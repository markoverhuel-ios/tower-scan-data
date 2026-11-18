#!/usr/bin/env python3
"""
Per-license frequency + location detail for the FRN Dashboard (iOS and Android).

The apps used to scrape wireless2.fcc.gov (licenseFreqSum.jsp / licenseLocSum.jsp) on every
device, and the FCC's bot protection answers that with HTTP 403. This publishes the same
information from the FCC's bulk ULS files instead, as static files on data.freshcoastrf.com.

Output: out_dir/<usi // 1000>.json  (gzip, ~5,000 small files)
  { "<usi>": { "f": [[freq, loc#, ant#, freqId, class, units, pagingRx, outPower, maxERP], ...],
               "l": [[label, address, "DD-MM-SS.s N, DDD-MM-SS.s W", status], ...] } }

<usi> is the ULS unique system identifier, which is the `licKey` the apps already store as
licenseID. Shards are by usi // 1000 because one FRN's licenses are usually numbered close
together, so a whole FRN's detail is a handful of files, not one per license.

The row shape matches FCCLicenseFrequency / FCCLicenseLocation in the apps so they decode it
directly. Licenses missing from the files (aircraft, amateur, area-based market licenses, ...)
return 404 and the apps fall back to the live FCC page.

Usage: python lic_detail.py --raw raw --work work --out out_lic
Reads the same raw/<name>.zip files build_db.py downloads; run it after build_db.py.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys

from build_db import ULS_SOURCES, clean, dat_rows, f_or_none, log

SHARD = 1000

# LO.location_type_code -> the label the FCC's Locations page shows after "N - ".
LOCATION_KIND = {"F": "Fixed", "M": "Mobile", "I": "Itinerant", "T": "Temporary Fixed", "6": "Fixed", "": "Fixed"}


def dms_text(deg: str, mins: str, secs: str, hemi: str, deg_width: int) -> str | None:
    """'42','20','51.3','N' -> '42-20-51.3 N', the same text the ULS page prints."""
    deg, mins, secs, hemi = deg.strip(), mins.strip(), secs.strip(), hemi.strip().upper()
    if not deg or not hemi or f_or_none(deg) is None:
        return None
    return f"{deg.zfill(deg_width)}-{(mins or '0').zfill(2)}-{secs or '0'} {hemi}"


def lat_long(p: list[str]) -> str:
    lat = dms_text(p[19], p[20], p[21], p[22], 2)
    lon = dms_text(p[23], p[24], p[25], p[26], 3)
    if lat is None or lon is None or lat.startswith("00-00-0 ") and lon.startswith("000-00-0 "):
        return ""
    return f"{lat}, {lon}"


def address(p: list[str]) -> str:
    street, city, county, state = clean(p[11]), clean(p[12]), clean(p[13]), clean(p[14])
    if street or (city and not county):
        return ", ".join(x for x in (street, city, state) if x)
    # Area-of-operation style rows have only a county, e.g. "KENT County, MI".
    parts = [f"{county} County" if county else None, state]
    return ", ".join(x for x in parts if x)


def location_row(p: list[str]) -> list[str] | None:
    loc = p[8].strip()
    if not loc.isdigit():
        return None
    label = f"{int(loc)} - {LOCATION_KIND.get(p[6].strip().upper(), 'Fixed')}"
    return [label, address(p), lat_long(p), ""]


def frequency_row(p: list[str]) -> list[str] | None:
    freq = p[10].strip()
    value = f_or_none(freq)
    if value is None or value == 0:
        return None
    return [freq, p[6].strip(), p[7].strip(), p[26].strip(), p[8].strip(), "MHz", "", p[15].strip(), p[16].strip()]


def stage(db: sqlite3.Connection, zip_path: str) -> None:
    name = os.path.basename(zip_path)
    log(f"{name}: locations")
    batch = []
    for p in dat_rows(zip_path, "LO.dat"):
        if len(p) < 27 or not p[1].isdigit():
            continue
        row = location_row(p)
        if row:
            batch.append((int(p[1]), "l", int(p[8]), json.dumps(row, separators=(",", ":"))))
        if len(batch) >= 100000:
            db.executemany("INSERT INTO d VALUES (?,?,?,?)", batch)
            batch.clear()
    db.executemany("INSERT INTO d VALUES (?,?,?,?)", batch)
    batch.clear()

    log(f"{name}: frequencies")
    seq = 0
    for p in dat_rows(zip_path, "FR.dat"):
        if len(p) < 27 or not p[1].isdigit():
            continue
        row = frequency_row(p)
        if row:
            seq += 1
            batch.append((int(p[1]), "f", seq, json.dumps(row, separators=(",", ":"))))
        if len(batch) >= 100000:
            db.executemany("INSERT INTO d VALUES (?,?,?,?)", batch)
            batch.clear()
    db.executemany("INSERT INTO d VALUES (?,?,?,?)", batch)
    db.commit()


def write_shards(db: sqlite3.Connection, out_dir: str) -> tuple[int, int]:
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        os.remove(os.path.join(out_dir, f))
    shard_id, shard, files, licenses = None, {}, 0, 0

    def flush():
        nonlocal shard, files
        if shard_id is None or not shard:
            return
        raw = json.dumps(shard, separators=(",", ":")).encode()
        with open(os.path.join(out_dir, f"{shard_id}.json"), "wb") as fh:
            fh.write(gzip.compress(raw, 9, mtime=0))
        files += 1
        shard = {}

    # One license's rows are contiguous; drop repeated locations (a site shared by paths).
    cur, seen = None, set()
    for usi, kind, _, payload in db.execute("SELECT usi, kind, seq, row FROM d ORDER BY usi, kind, seq"):
        sid = usi // SHARD
        if sid != shard_id:
            flush()
            shard_id = sid
        if usi != cur:
            cur, seen = usi, set()
            shard[str(usi)] = {"f": [], "l": []}
            licenses += 1
        if payload in seen:
            continue
        seen.add(payload)
        shard[str(usi)][kind].append(json.loads(payload))
    flush()
    return files, licenses


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default="raw")
    ap.add_argument("--work", default="work")
    ap.add_argument("--out", default="out_lic")
    ap.add_argument("--min-licenses", type=int, default=100000, help="fail if fewer licenses than this were found")
    args = ap.parse_args()
    os.makedirs(args.work, exist_ok=True)

    path = os.path.join(args.work, "lic_detail.sqlite")
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE d(usi INTEGER, kind TEXT, seq INTEGER, row TEXT)")

    for src in ULS_SOURCES:
        z = os.path.join(args.raw, src + ".zip")
        if not os.path.exists(z):
            log(f"{src}: missing, skipped")
            continue
        stage(db, z)
    db.execute("CREATE INDEX d_usi ON d(usi, kind, seq)")
    files, licenses = write_shards(db, args.out)
    db.close()
    log(f"license detail: {licenses:,} licenses in {files:,} files")
    if licenses < args.min_licenses:
        log("error: suspiciously few licenses; not publishing")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
