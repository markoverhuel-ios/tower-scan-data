#!/usr/bin/env python3
"""
FAA Digital Obstacle File (DOF): known aviation obstacles (towers, wind turbines,
buildings, stacks, poles, ...) and, for ASR towers, the FAA aeronautical study
number that ties an obstacle to its FCC registration.

Source: FAA Aeronautical Information Services, https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/dof/
U.S. government data, public domain. Updated every 56 days.

FAA's caution applies to everything built from this file: it holds only man-made
objects that affect aeronautical charting and does not purport to show every
obstruction. "Unverified" records have unverified positions and heights.

Record layout (columns are 1-based in the FAA README; slices below are 0-based):
  1-9 OAS number | 11 verified O/U | 13-14 country | 16-17 state | 19-34 city
  36-47 latitude | 49-61 longitude | 63-80 type | 82 qty | 84-88 AGL ft | 90-94 AMSL ft
  96 lighting | 98 horizontal accuracy | 100 vertical accuracy | 102 marking
  104-117 FAA study number | 119 action (A/C) | 121-127 Julian date of action

Standard library only.
"""

from __future__ import annotations

import datetime as dt
import io
import math
import re
import urllib.request
import zipfile
from collections import defaultdict

SOURCE = "FAA Digital Obstacle File"
PAGE_URL = "https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/dof/"
ZIP_URL = "https://aeronav.faa.gov/Obst_Data/DOF_{yymmdd}.zip"
# One cycle every 56 days. Used only if the FAA page can't be read.
CYCLE_ANCHOR = dt.date(2026, 9, 27)
CYCLE_DAYS = 56

# DOF country codes we keep: the US and its territories.
KEEP_COUNTRIES = {"US", "PR", "VI", "GU", "AS", "MP"}

FT_TO_M = 0.3048
STUDY_LINK_MAX_M = 1000.0   # a study # shared by structures farther apart than this isn't a link
NEAR_LINK_MAX_M = 30.0      # no study match: an ASR tower this close is treated as the same structure
GRID_DEG = 0.001            # ~111 m; 3x3 neighbors cover NEAR_LINK_MAX_M comfortably

# Browser-like header: the FAA site refuses some default client strings.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TowerScanDataBuilder/1.0; +https://github.com/markoverhuel-ios/tower-scan-data)"}

OBSTACLE_COLUMNS = (
    "id, ver, city, state, lat, lon, type, qty, agl, amsl, light, hacc, vacc, mark, "
    "study, action, updated, asr, match, tile"
)


# ---------------------------------------------------------------------------
# Finding the current file


def cycle_urls(today: dt.date, count: int = 3) -> list[str]:
    """Candidate file URLs for the most recent cycles, newest first."""
    n = (today - CYCLE_ANCHOR).days // CYCLE_DAYS
    out = []
    for k in range(n, n - count, -1):
        d = CYCLE_ANCHOR + dt.timedelta(days=CYCLE_DAYS * k)
        out.append(ZIP_URL.format(yymmdd=d.strftime("%y%m%d")))
    return out


def find_zip_url(fetch_text=None, today: dt.date | None = None, head=None) -> str:
    """The newest DOF zip. Reads the FAA page for its links; if that fails, falls
    back to the 56-day cycle arithmetic and checks which file exists."""
    today = today or dt.date.today()
    try:
        text = fetch_text(PAGE_URL) if fetch_text else _get_text(PAGE_URL)
        links = re.findall(r"https://aeronav\.faa\.gov/Obst_Data/DOF_(\d{6})\.zip", text)
        if links:
            newest = max(set(links), key=lambda y: dt.datetime.strptime(y, "%y%m%d"))
            return ZIP_URL.format(yymmdd=newest)
    except Exception:  # noqa: BLE001 - fall through to the arithmetic guess
        pass
    check = head or _exists
    for url in cycle_urls(today):
        if check(url):
            return url
    raise RuntimeError("couldn't find a current DOF file")


def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def _exists(url: str) -> bool:
    req = urllib.request.Request(url, headers=HEADERS, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Parsing


def study_key(raw: str | None) -> str | None:
    """Canonical key for an FAA study number, so FCC ASR and DOF spellings meet:
         ASR: '2013-AGL-9135-OE', '96-AGL-1964-OE'   DOF: '2013AGL09135OE'
       -> '2013AGL09135OE'. Returns None for blanks and 'N/A'."""
    if not raw:
        return None
    s = raw.strip().upper()
    m = re.match(r"^(\d{2,4})-([A-Z]{2,3})-(\d+)-([A-Z]{2,3})$", s)
    if not m:
        m = re.match(r"^(\d{4})([A-Z]{2,3})(\d{5})([A-Z]{2,3})$", s)
    if not m:
        return None
    year, region, seq, kind = m.groups()
    y = int(year)
    if len(year) == 2:  # same rule the FAA site's own picker uses
        y = 2000 + y if y <= 26 else 1900 + y
    return f"{y}{region}{int(seq):05d}{kind}"


def study_display(key: str | None) -> str | None:
    """'2013AGL09135OE' -> '2013-AGL-9135-OE' (the form the FAA site and FCC use)."""
    if not key:
        return None
    m = re.match(r"^(\d{4})([A-Z]{2,3})(\d{5})([A-Z]{2,3})$", key)
    return f"{m.group(1)}-{m.group(2)}-{int(m.group(3))}-{m.group(4)}" if m else None


def julian_to_iso(jd: str) -> str | None:
    """'2014138' (year + day of year) -> '2014-05-18'."""
    jd = jd.strip()
    if not re.match(r"^\d{7}$", jd):
        return None
    try:
        return (dt.date(int(jd[:4]), 1, 1) + dt.timedelta(days=int(jd[4:]) - 1)).isoformat()
    except ValueError:
        return None


def _dms(deg: str, mins: str, secs: str, hemi: str) -> float:
    v = int(deg) + int(mins) / 60 + float(secs) / 3600
    return round(-v if hemi in "SW" else v, 6)


def parse_line(l: str) -> dict | None:
    """One DOF.DAT detail line -> dict, or None if it isn't one."""
    if len(l) < 110 or l[2] != "-":
        return None
    try:
        return {
            "id": l[0:9],
            "ver": l[10],
            "co": l[12:14],
            "state": l[15:17],
            "city": l[18:34].strip(),
            "lat": _dms(l[35:37], l[38:40], l[41:46], l[46]),
            "lon": _dms(l[48:51], l[52:54], l[55:60], l[60]),
            "type": l[62:80].strip(),
            "qty": int(l[81]) if l[81].isdigit() else 1,
            "agl_ft": int(l[83:88]),
            "amsl_ft": int(l[89:94]),
            "light": l[95],
            "hacc": l[97],
            "vacc": l[99],
            "mark": l[101],
            "study": study_key(l[103:117]),
            "action": l[118],
            "updated": julian_to_iso(l[120:127]),
        }
    except (ValueError, IndexError):
        return None


def currency_date(first_line: str) -> str | None:
    """'  CURRENCY DATE = 09/27/26' -> '2026-09-27'."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{2})", first_line)
    if not m:
        return None
    mm, dd, yy = (int(x) for x in m.groups())
    return dt.date(2000 + yy, mm, dd).isoformat()


def read_obstacles(zip_path: str):
    """Yields (currency_date, obstacle dicts) from DOF.DAT inside the zip."""
    with zipfile.ZipFile(zip_path) as z:
        with z.open("DOF.DAT") as raw:
            lines = io.TextIOWrapper(raw, encoding="latin-1", newline="")
            first = lines.readline()
            cur = currency_date(first)
            return cur, [o for o in (parse_line(l.rstrip("\r\n")) for l in lines) if o and o["co"] in KEEP_COUNTRIES]


# ---------------------------------------------------------------------------
# Linking to ASR


def _metres(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((lat2 - lat1) * 111_320.0,
                      (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2)))


def link_to_asr(obstacles: list[dict], asr_rows: list[tuple]) -> None:
    """Sets o['asr'] and o['match'] in place.

    asr_rows: (asr id, lat, lon, faa_study string as the FCC wrote it).
      match 'study'  the obstacle's FAA study number is the tower's, and they sit within
                     STUDY_LINK_MAX_M (one study can cover several structures; the nearest wins)
      match 'near'   no study match, but an ASR tower is within NEAR_LINK_MAX_M
      (none)         not linked: asr = None, match = None
    """
    by_study: dict[str, list[tuple]] = defaultdict(list)
    grid: dict[tuple[int, int], list[tuple]] = defaultdict(list)
    for a in asr_rows:
        k = study_key(a[3])
        if k:
            by_study[k].append(a)
        grid[(math.floor(a[1] / GRID_DEG), math.floor(a[2] / GRID_DEG))].append(a)

    for o in obstacles:
        o["asr"], o["match"] = None, None
        best = None
        for a in by_study.get(o["study"] or "", ()):
            d = _metres(o["lat"], o["lon"], a[1], a[2])
            if d <= STUDY_LINK_MAX_M and (best is None or d < best[0]):
                best = (d, a[0])
        if best:
            o["asr"], o["match"] = best[1], "study"
            continue
        ky, kx = math.floor(o["lat"] / GRID_DEG), math.floor(o["lon"] / GRID_DEG)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for a in grid.get((ky + dy, kx + dx), ()):
                    d = _metres(o["lat"], o["lon"], a[1], a[2])
                    if d <= NEAR_LINK_MAX_M and (best is None or d < best[0]):
                        best = (d, a[0])
        if best:
            o["asr"], o["match"] = best[1], "near"


def tile_key(lat: float, lon: float, tile_deg: int) -> str:
    return f"{math.floor(lat / tile_deg)}_{math.floor(lon / tile_deg)}"


def to_rows(obstacles: list[dict], tile_deg: int) -> list[tuple]:
    return [(
        o["id"], o["ver"], o["city"] or None, o["state"] or None, o["lat"], o["lon"],
        o["type"], o["qty"], round(o["agl_ft"] * FT_TO_M, 1), round(o["amsl_ft"] * FT_TO_M, 1),
        o["light"], o["hacc"], o["vacc"], o["mark"], study_display(o["study"]),
        o["action"], o["updated"], o["asr"], o["match"], tile_key(o["lat"], o["lon"], tile_deg),
    ) for o in obstacles]
