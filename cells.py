#!/usr/bin/env python3
"""
Carrier layer: which cellular carriers are (probably) on each ASR tower.

Source: OpenCellID (https://opencellid.org), (c) OpenCellID contributors,
licensed CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/).

This is NOT FCC data and it is kept apart from the FCC tiles on purpose:
CC BY-SA is ShareAlike, so anything derived from OpenCellID has to stay
under CC BY-SA, while the FCC tiles are public-domain US government data.
Output goes to a separate `cells.sqlz` file, each row marked with its
source, and the manifest carries the attribution text the app must show.

How a carrier gets "assumed" onto a tower:
  OpenCellID only knows crowd-sourced *estimated* positions for individual
  radio cells (sectors). For each US cell we find the nearest ASR structure
  within MAX_DIST_M, then roll cells up per (tower, carrier). Confidence:
    likely    within LIKELY_DIST_M, enough samples, and no other tower within
              AMBIGUOUS_GAP_M (towers that close are indistinguishable given
              OpenCellID's position noise, so they all get "possible")
    possible  anything else that still matched
  Most cell sites are on structures too short to need an ASR, so most cells
  will not match any tower. That's expected.

Standard library only.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import math
import os
import sqlite3
import statistics
import sys
import time
import urllib.parse
import urllib.request
import zlib
from collections import defaultdict

SOURCE = "OpenCellID"
LICENSE = "CC BY-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
SOURCE_URL = "https://opencellid.org"
ATTRIBUTION = "Cell tower data from OpenCellID, licensed under CC BY-SA 4.0."
CHANGES = (
    "Cell positions matched to the nearest FCC antenna structure (ASR) and "
    "grouped by carrier; carrier names derived from MCC/MNC."
)

# Tunables. Calibrated on the first real run (2026-10-16): of ~12k tower/carrier
# matches within 200 m, only ~15% were real; the rest were an unrelated cell site
# that happened to be nearby. Share that's real, by total measurements behind a row:
#   <20 samples: 5-20%   |   20-59: ~40%   |   60+: ~95% (within 90 m)
# so rows are kept/labelled by evidence, not just distance. Re-check with
# tools/calibrate.py after changing anything or when OpenCellID's data shifts.
MAX_DIST_M = 200.0          # candidate search radius (also used to detect near-twin towers)
KEEP_DIST_M = 90.0          # farther than this: dropped
KEEP_MIN_SAMPLES = 20       # fewer measurements than this: dropped (mostly chance)
LIKELY_MIN_SAMPLES = 60     # at least this many, and not ambiguous => "likely"
MIN_CELL_SAMPLES = 2        # single-measurement cells are noise before we even aggregate
AMBIGUOUS_GAP_M = 30.0      # runner-up tower this close behind nearest => ambiguous
LEGACY_RADIOS = {"GSM", "UMTS", "CDMA"}  # US 2G/3G networks are largely shut down

GRID_DEG = 0.005         # spatial index cell; 3x3 neighbors cover >= 280 m
TILE_DEG = 2             # must match build_db.TILE_DEG

# US + territories (CONUS/AK/HI 310-316, Puerto Rico 330, USVI 332).
US_MCCS = {str(m) for m in (310, 311, 312, 313, 314, 315, 316, 330, 332)}

RADIO_ORDER = {"GSM": 0, "CDMA": 1, "UMTS": 2, "LTE": 3, "NR": 4}

DEFAULT_URL = "https://opencellid.org/ocid/downloads?token={token}&type=full&file=cell_towers.csv.gz"

CELL_SCHEMA = """
CREATE TABLE cell_tenant(
  asr INTEGER NOT NULL,
  carrier TEXT NOT NULL,
  codes TEXT,            -- "310-410,310-150": MCC-MNC pairs seen
  radios TEXT,           -- "LTE,NR"
  cells INTEGER,         -- radio cells (sectors) matched to this tower
  samples INTEGER,       -- measurements behind them
  dist_m INTEGER,        -- closest cell's estimated distance from the tower
  range_m INTEGER,       -- smallest reported cell range, if any
  updated TEXT,          -- newest observation (YYYY-MM-DD)
  confidence TEXT NOT NULL,   -- likely | possible
  source TEXT NOT NULL,       -- always 'OpenCellID'
  source_date TEXT,           -- date of the OpenCellID export used
  tile TEXT NOT NULL,
  PRIMARY KEY(asr, carrier)
);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Carrier names


def load_carriers(path: str) -> dict[str, str]:
    """{"310-410": "AT&T", ...}"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def carrier_for(carriers: dict[str, str], mcc: str, mnc: str) -> str:
    mnc = mnc.strip()
    # The MNC is 2 or 3 digits and the export doesn't keep leading zeros.
    for cand in (mnc.zfill(3), mnc.zfill(2), mnc):
        name = carriers.get(f"{mcc}-{cand}")
        if name:
            return name
    return f"Carrier {mcc}-{mnc}"


# ---------------------------------------------------------------------------
# Download


def download_opencellid(token: str, dest: str, url_template: str = DEFAULT_URL) -> str:
    """Downloads the full export. The token is never printed.

    The service answers errors with HTTP 200 and a JSON body, so the result
    is checked for the gzip magic number rather than the status code."""
    if os.path.exists(dest) and os.path.getsize(dest) > 1_000_000:
        log(f"cached {os.path.basename(dest)}")
        return dest
    url = url_template.format(token=urllib.parse.quote(token))
    log("downloading OpenCellID export (token hidden)")
    req = urllib.request.Request(url, headers={"User-Agent": "TowerScanDataBuilder/1.0"})
    with urllib.request.urlopen(req, timeout=900) as r, open(dest + ".part", "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    with open(dest + ".part", "rb") as f:
        magic = f.read(2)
        f.seek(0)
        head = f.read(200)
    if magic != b"\x1f\x8b":
        os.remove(dest + ".part")
        raise RuntimeError(f"OpenCellID did not return a gzip file: {head[:120]!r}")
    os.replace(dest + ".part", dest)
    return dest


# ---------------------------------------------------------------------------
# Matching


def _metres_per_deg_lon(lat: float) -> float:
    return 111_320.0 * math.cos(math.radians(lat))


def dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat2 - lat1) * 111_320.0
    dx = (lon2 - lon1) * _metres_per_deg_lon((lat1 + lat2) / 2)
    return math.hypot(dx, dy)


class TowerIndex:
    """Grid index of ASR structures for nearest-neighbor lookups."""

    def __init__(self, towers: list[tuple[int, float, float]]):
        self.towers = {t[0]: (t[1], t[2]) for t in towers}
        self.grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for asr, lat, lon in towers:
            self.grid[self._key(lat, lon)].append(asr)

    @staticmethod
    def _key(lat: float, lon: float) -> tuple[int, int]:
        return (math.floor(lat / GRID_DEG), math.floor(lon / GRID_DEG))

    def nearby(self, lat: float, lon: float, limit_m: float):
        """Returns [(dist, asr), ...] for every tower within limit_m, nearest first."""
        ky, kx = self._key(lat, lon)
        found = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for asr in self.grid.get((ky + dy, kx + dx), ()):
                    tlat, tlon = self.towers[asr]
                    d = dist_m(lat, lon, tlat, tlon)
                    if d <= limit_m:
                        found.append((d, asr))
        found.sort()
        return found


def match_cells(rows, index: TowerIndex, carriers: dict[str, str],
                max_dist: float = MAX_DIST_M, min_samples: int = MIN_CELL_SAMPLES):
    """rows: iterable of OpenCellID CSV rows (lists of str).

    Returns ({(asr, carrier): aggregate}, stats)."""
    agg: dict[tuple[int, str], dict] = {}
    stats = {"rows": 0, "us": 0, "usable": 0, "matched": 0}
    for row in rows:
        stats["rows"] += 1
        if len(row) < 13 or row[1] not in US_MCCS:
            continue
        stats["us"] += 1
        try:
            samples = int(row[9])
            lon, lat = float(row[6]), float(row[7])
        except ValueError:
            continue
        if samples < min_samples or (lat == 0 and lon == 0):
            continue
        stats["usable"] += 1
        near = index.nearby(lat, lon, max_dist)
        if not near:
            continue
        stats["matched"] += 1
        # OpenCellID positions are noisy (tens of meters), so towers within
        # AMBIGUOUS_GAP_M of the nearest one are indistinguishable. The cell
        # goes to all of them, flagged ambiguous, rather than arbitrarily to one
        # (which would leave its neighbor with no carriers at all).
        candidates = [(d, asr) for d, asr in near if d - near[0][0] < AMBIGUOUS_GAP_M]
        ambiguous = len(candidates) > 1
        carrier = carrier_for(carriers, row[1], row[2])
        try:
            rng = int(float(row[8]))
        except ValueError:
            rng = 0
        try:
            updated = int(row[12])
        except ValueError:
            updated = 0
        for d, asr in candidates:
            a = agg.setdefault((asr, carrier), {
                "codes": set(), "radios": set(), "cells": 0, "samples": 0,
                "dist": d, "ranges": [], "updated": 0, "ambiguous": True,
            })
            a["codes"].add(f"{row[1]}-{row[2]}")
            a["radios"].add(row[0])
            a["cells"] += 1
            a["samples"] += samples
            a["dist"] = min(a["dist"], d)
            if rng > 0:
                a["ranges"].append(rng)
            a["updated"] = max(a["updated"], updated)
            # A tower stays "ambiguous" only if every cell behind it was.
            a["ambiguous"] = a["ambiguous"] and ambiguous
    return agg, stats


def confidence(a: dict) -> str | None:
    """'likely', 'possible', or None when the evidence is too weak to show."""
    if a["radios"] <= LEGACY_RADIOS:          # only 2G/3G: networks mostly sunset
        return None
    if a["dist"] > KEEP_DIST_M or a["samples"] < KEEP_MIN_SAMPLES:
        return None
    if a["samples"] >= LIKELY_MIN_SAMPLES and not a["ambiguous"]:
        return "likely"
    return "possible"


def to_rows(agg: dict, towers: dict[int, tuple[float, float]], source_date: str):
    out = []
    for (asr, carrier), a in agg.items():
        conf = confidence(a)
        if conf is None:
            continue
        lat, lon = towers[asr]
        radios = sorted(a["radios"], key=lambda r: RADIO_ORDER.get(r, 9))
        updated = (dt.datetime.fromtimestamp(a["updated"], dt.timezone.utc).strftime("%Y-%m-%d")
                   if a["updated"] else None)
        out.append((
            asr, carrier, ",".join(sorted(a["codes"])), ",".join(radios),
            a["cells"], a["samples"], int(round(a["dist"])),
            int(statistics.median(a["ranges"])) if a["ranges"] else None,
            updated, conf, SOURCE, source_date,
            f"{math.floor(lat / TILE_DEG)}_{math.floor(lon / TILE_DEG)}",
        ))
    return out


# ---------------------------------------------------------------------------
# Reading + writing


def read_rows(path: str):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i == 0 and row and row[0].lower() == "radio":
                continue
            yield row


def load_towers(stage_path: str) -> dict[int, tuple[float, float]]:
    db = sqlite3.connect(stage_path)
    towers = {r[0]: (r[1], r[2]) for r in db.execute("SELECT id, lat, lon FROM asr")}
    db.close()
    return towers


def _compress(src: str, dest: str) -> int:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    with open(src, "rb") as fi, open(dest, "wb") as fo:
        while chunk := fi.read(1 << 20):
            fo.write(c.compress(chunk))
        fo.write(c.flush())
    return os.path.getsize(dest)


def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def write_cells_file(rows: list[tuple], out_dir: str, work_dir: str, source_date: str) -> dict:
    """One small file for the whole country (~12k rows), not one per tile: it keeps
    the release's asset count down, and the app downloads it once."""
    tmp = os.path.join(work_dir, "cells.sqlite")
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.execute("PRAGMA journal_mode=OFF")
    db.executescript(CELL_SCHEMA)
    db.executemany("INSERT INTO cell_tenant VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO meta VALUES (?,?)", [
        ("source", SOURCE), ("license", LICENSE), ("license_url", LICENSE_URL),
        ("source_url", SOURCE_URL), ("attribution", ATTRIBUTION),
        ("changes", CHANGES), ("source_date", source_date),
    ])
    db.commit()
    db.execute("VACUUM")
    db.close()
    dest = os.path.join(out_dir, "cells.sqlz")
    size = _compress(tmp, dest)
    os.remove(tmp)
    return {"file": "cells.sqlz", "bytes": size, "sha256": _sha256(dest), "rows": len(rows)}


def manifest_block(file_info: dict, source_date: str, stats: dict) -> dict:
    return {
        "source": SOURCE,
        "license": LICENSE,
        "licenseURL": LICENSE_URL,
        "sourceURL": SOURCE_URL,
        "attribution": ATTRIBUTION,
        "changes": CHANGES,
        "sourceDate": source_date,
        "stats": stats,
        "file": file_info,
    }


def build_cells(stage_path: str, csv_path: str, carriers_path: str,
                out_dir: str, work_dir: str, source_date: str) -> dict:
    carriers = load_carriers(carriers_path)
    towers = load_towers(stage_path)
    log(f"cells: {len(towers):,} ASR structures indexed")
    index = TowerIndex([(a, la, lo) for a, (la, lo) in towers.items()])
    agg, stats = match_cells(read_rows(csv_path), index, carriers)
    rows = to_rows(agg, towers, source_date)
    stats["candidates"] = len(agg)
    stats["dropped"] = len(agg) - len(rows)
    log(f"cells: {stats['us']:,} US cells, {stats['usable']:,} usable, "
        f"{stats['matched']:,} matched -> {len(agg):,} candidates -> {len(rows):,} kept "
        f"({stats['dropped']:,} dropped as weak/legacy)")
    file_info = write_cells_file(rows, out_dir, work_dir, source_date)
    stats["rowsOut"] = len(rows)
    stats["towersWithCarriers"] = len({k[0] for k in agg})
    log(f"cells: cells.sqlz {file_info['bytes'] / 1e3:.0f} KB, {stats['towersWithCarriers']:,} towers with carriers")
    return manifest_block(file_info, source_date, stats)


if __name__ == "__main__":
    sys.exit("cells.py is used by build_db.py (see --opencellid-file)")
