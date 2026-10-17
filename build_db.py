#!/usr/bin/env python3
"""
Tower Scan data builder.

Turns the FCC's public bulk data into small, map-ready SQLite tiles that the
Tower Scan iOS app downloads on demand.

Sources (all public FCC data):
  - ASR:        data.fcc.gov  r_tower.zip            (antenna structures)
  - ULS:        data.fcc.gov  l_*.zip                (site-based licenses)
  - Broadcast:  transition.fcc.gov fmq / tvq / amq   (FM, TV, AM stations)

Output (in --out):
  tile_<lat>_<lon>.sqlz   one raw-DEFLATE-compressed SQLite DB per TILE_DEG square
  index.sqlz              call sign / ASR number -> coordinates, for search
                          (tiles also carry the FAA Digital Obstacle File: see dof.py)
  manifest.json           what exists, sizes, and where to download it

Field positions were checked against the real files (see README.md).
Standard library only, so it runs anywhere with Python 3.10+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.request
import zipfile
import zlib

SCHEMA_VERSION = 1
TILE_DEG = 2  # must match TileMath.tileDegrees in the app

ULS_BASE = "https://data.fcc.gov/download/pub/uls/complete/"
ASR_ZIP = "r_tower"

# ULS bulk file -> app category. Only site-based services; market-area
# licenses (AWS, PCS, 700 MHz blocks) have no transmitter coordinates.
ULS_SOURCES = {
    "l_LMpriv": "landMobile",
    "l_LMcomm": "commercial",
    "l_micro": "microwave",
    "l_cell": "cellular",
    "l_paging": "paging",
    "l_LMbcast": "broadcastAux",
    "l_coast": "coastal",
}

# LO.location_type_code values that are real, fixed transmitter/receiver
# sites. M (mobile) and I (itinerant) are areas of operation, not sites.
FIXED_LOCATION_TYPES = {"F", "6", "T", ""}

BROADCAST_QUERIES = {
    "fm": "https://transition.fcc.gov/fcc-bin/fmq?state=&call=&city=&arn=&serv=&vac=&freq=0.0&fre2=108.0&facid=&class=&dkt=&list=4&dist=&dlat2=&mlat2=&slat2=&NS=N&dlon2=&mlon2=&slon2=&EW=W&size=9",
    "tv": "https://transition.fcc.gov/fcc-bin/tvq?state=&call=&city=&chan=0&cha2=69&serv=&vac=&facid=&list=4&dist=&dlat2=&mlat2=&slat2=&NS=N&dlon2=&mlon2=&slon2=&EW=W&size=9",
    "am": "https://transition.fcc.gov/fcc-bin/amq?state=&call=&city=&freq=530&fre2=1700&type=0&facid=&class=&list=4&dist=&dlat2=&mlat2=&slat2=&NS=N&dlon2=&mlon2=&slon2=&EW=W&size=9",
}
BROADCAST_STATUSES = {"LIC", "STA"}

USER_AGENT = "TowerScanDataBuilder/1.0 (+https://github.com/markoverhuel-ios/tower-scan-data)"

TILE_SCHEMA = """
CREATE TABLE asr(
  id INTEGER PRIMARY KEY,        -- ASR registration number
  lat REAL NOT NULL, lon REAL NOT NULL,
  height REAL, overall_agl REAL, overall_amsl REAL, ground REAL,
  type TEXT, status TEXT, owner TEXT, frn TEXT,
  address TEXT, city TEXT, state TEXT, faa_study TEXT, constructed TEXT,
  tile TEXT NOT NULL
);
CREATE TABLE site(
  id INTEGER PRIMARY KEY,        -- ULS: usi*1000+location#; broadcast: hashed
  kind TEXT NOT NULL,            -- 'uls' | 'fm' | 'tv' | 'am'
  cat TEXT NOT NULL,             -- landMobile, microwave, cellular, ..., fm, tv, am
  call TEXT, svc TEXT, licensee TEXT, frn TEXT,
  lat REAL NOT NULL, lon REAL NOT NULL,
  asr INTEGER,                   -- ASR registration number, when the license names one
  name TEXT, address TEXT, city TEXT, state TEXT,
  ground REAL, height REAL, struct TEXT, cls TEXT,
  expires TEXT, usi INTEGER, loc INTEGER,
  fmin REAL, fmax REAL,          -- MHz, for quick band filtering
  extra TEXT,                    -- JSON with source-specific details
  tile TEXT NOT NULL
);
CREATE TABLE obstacle(
  id TEXT PRIMARY KEY,           -- FAA OAS number, e.g. 01-063487
  ver TEXT,                      -- 'O' verified, 'U' unverified
  city TEXT, state TEXT,
  lat REAL NOT NULL, lon REAL NOT NULL,
  type TEXT, qty INTEGER,
  agl REAL, amsl REAL,           -- metres
  light TEXT, hacc TEXT, vacc TEXT, mark TEXT,   -- FAA DOF codes
  study TEXT,                    -- FAA aeronautical study number, e.g. 2017-AGL-16359-OE
  action TEXT, updated TEXT,     -- last action (A add / C change) and its date
  asr INTEGER,                   -- linked ASR registration, when there is one
  match TEXT,                    -- 'study' | 'near' | NULL
  tile TEXT NOT NULL
);
CREATE TABLE freq(
  site INTEGER NOT NULL,
  mhz REAL, upper REAL, power REAL, erp REAL, cls TEXT, emis TEXT
);
CREATE TABLE asr_key(
  asr INTEGER PRIMARY KEY,       -- ASR registration number
  regkey INTEGER NOT NULL        -- the FCC's internal key, used in asrRegistration.jsp?regKey=
);
CREATE TABLE contact(
  kind TEXT NOT NULL,            -- 'owner' (ASR owner) | 'control' (ULS control point) | 'licensee'
  ref INTEGER NOT NULL,          -- ASR number for 'owner', ULS usi for the others
  label TEXT,                    -- owner / licensee name, or the control point's "City, ST"
  phone TEXT NOT NULL            -- 10 digits, no punctuation
);
CREATE TABLE path(
  id INTEGER PRIMARY KEY,        -- usi*1000+path#
  call TEXT, licensee TEXT,
  tx_site INTEGER, rx_site INTEGER,
  tx_lat REAL, tx_lon REAL, rx_lat REAL, rx_lon REAL,
  fmin REAL, fmax REAL,
  tile TEXT NOT NULL
);
"""

TILE_INDEXES = """
CREATE INDEX asr_ll ON asr(lat, lon);
CREATE INDEX site_ll ON site(lat, lon);
CREATE INDEX site_asr ON site(asr);
CREATE INDEX freq_site ON freq(site);
CREATE INDEX path_tx ON path(tx_lat, tx_lon);
CREATE INDEX obstacle_ll ON obstacle(lat, lon);
CREATE INDEX obstacle_asr ON obstacle(asr);
CREATE INDEX contact_ref ON contact(kind, ref);
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Download helpers


def download(url: str, dest: str, retries: int = 4, headers: dict | None = None) -> str:
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        log(f"cached {os.path.basename(dest)}")
        return dest
    for attempt in range(1, retries + 1):
        try:
            log(f"downloading {url}")
            req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=600) as r, open(dest + ".part", "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            os.replace(dest + ".part", dest)
            return dest
        except Exception as e:  # noqa: BLE001 - retry anything network-shaped
            log(f"  attempt {attempt} failed: {e}")
            if attempt == retries:
                raise
            time.sleep(10 * attempt)
    return dest


def dat_rows(zip_path: str, member: str):
    """Yields split rows from one .dat file inside an FCC zip, streaming."""
    with zipfile.ZipFile(zip_path) as z:
        if member not in z.namelist():
            return
        with z.open(member) as raw:
            for line in io.TextIOWrapper(raw, encoding="latin-1", newline=""):
                line = line.rstrip("\r\n")
                if line:
                    yield line.split("|")


# ---------------------------------------------------------------------------
# Parsing helpers


def f_or_none(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip()
    m = re.match(r"^-?\d*\.?\d+", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def dms(deg: str, mins: str, secs: str, hemi: str) -> float | None:
    d = f_or_none(deg)
    if d is None:
        return None
    v = d + (f_or_none(mins) or 0) / 60 + (f_or_none(secs) or 0) / 3600
    if hemi.strip().upper() in ("S", "W"):
        v = -v
    return round(v, 6)


def asr_num(s: str | None) -> int | None:
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) < 6 or len(digits) > 8:
        return None
    return int(digits)


def tile_key(lat: float, lon: float) -> str:
    return f"{math.floor(lat / TILE_DEG)}_{math.floor(lon / TILE_DEG)}"


def clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = " ".join(s.split())
    return s or None


def clean_phone(s: str | None) -> str | None:
    """10 digits or None. Rejects blanks and obvious placeholders (0000000000, 1111111111...)."""
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or len(set(digits)) == 1:
        return None
    return digits


ORG_WORDS = re.compile(
    r"\b(INC|INCORPORATED|LLC|L L C|CORP|CORPORATION|CO|COMPANY|LTD|LP|LLP|COUNTY|CITY|TOWN|TOWNSHIP|"
    r"VILLAGE|STATE|DEPARTMENT|DEPT|DISTRICT|AUTHORITY|SCHOOLS?|UNIVERSITY|COLLEGE|HOSPITAL|"
    r"COOPERATIVE|COOP|ASSOCIATION|ASSN|COMMISSION|BOARD|SERVICES?|TOWERS?|COMMUNICATIONS|WIRELESS|"
    r"ELECTRIC|POWER|RAILROAD|UTILITY|UTILITIES|FIRE|POLICE|SHERIFF|PARISH|BOROUGH|TRIBE|TRIBAL)\b",
    re.I)


def is_organization(type_code: str | None, first: str | None, last: str | None, name: str | None) -> bool:
    """Publish phone numbers for businesses and agencies only, never individuals.

    Individual = applicant type 'I', or a first/last name on the record. A blank type code
    is only trusted when the name reads like a business or agency.
    """
    if (first or "").strip() or (last or "").strip():
        return False
    code = (type_code or "").strip().upper()
    if code == "I":
        return False
    if code:
        return True
    return bool(ORG_WORDS.search(name or ""))


def tv_channel_mhz(ch: int) -> float | None:
    if 2 <= ch <= 4:
        return 57.0 + (ch - 2) * 6
    if 5 <= ch <= 6:
        return 79.0 + (ch - 5) * 6
    if 7 <= ch <= 13:
        return 177.0 + (ch - 7) * 6
    if 14 <= ch <= 69:
        return 473.0 + (ch - 14) * 6
    return None


def stable_id(*parts) -> int:
    """Deterministic 52-bit id for rows that have no FCC numeric key."""
    h = hashlib.sha1("|".join(str(p) for p in parts).encode()).digest()
    return (1 << 52) + (int.from_bytes(h[:6], "big") & ((1 << 48) - 1))


# ---------------------------------------------------------------------------
# Staging database (nationwide, on disk)


def open_stage(path: str) -> sqlite3.Connection:
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-400000")
    db.executescript(TILE_SCHEMA)
    db.execute("CREATE TABLE emis(site INTEGER, mhz REAL, emis TEXT)")
    db.execute("CREATE TABLE rawpath(usi INTEGER, path INTEGER, txloc INTEGER, rxloc INTEGER)")
    return db


def load_asr(db: sqlite3.Connection, zip_path: str) -> int:
    log("ASR: registrations")
    reg: dict[int, list] = {}  # reg# -> [usi, row]
    for p in dat_rows(zip_path, "RA.dat"):
        if len(p) < 35 or p[1] != "REG" or p[8] not in ("C", "G"):
            continue
        n = asr_num(p[3])
        usi = int(p[4]) if p[4].isdigit() else 0
        if n is None:
            continue
        if n not in reg or usi > reg[n][0]:
            reg[n] = [usi, p]
    usi_to_reg = {v[0]: k for k, v in reg.items()}

    log("ASR: coordinates")
    coords: dict[int, tuple[float, float]] = {}
    for p in dat_rows(zip_path, "CO.dat"):
        if len(p) < 15 or p[1] != "REG":
            continue
        usi = int(p[4]) if p[4].isdigit() else 0
        n = usi_to_reg.get(usi)
        if n is None or (p[5] and p[5] != "T"):
            continue
        lat = dms(p[6], p[7], p[8], p[9])
        lon = dms(p[11], p[12], p[13], p[14])
        if lat is not None and lon is not None:
            coords[n] = (lat, lon)

    log("ASR: owners")
    owners: dict[int, tuple[str | None, str | None]] = {}
    owner_phone: dict[int, tuple[str | None, str]] = {}
    for p in dat_rows(zip_path, "EN.dat"):
        if len(p) < 25 or p[1] != "REG" or p[5] != "O":
            continue
        usi = int(p[4]) if p[4].isdigit() else 0
        n = usi_to_reg.get(usi)
        if n is not None:
            owners[n] = (clean(p[9]), clean(p[24]))
            phone = clean_phone(p[14])
            if phone and is_organization(p[6], p[10], p[12], p[9]):
                owner_phone[n] = (clean(p[9]), phone)
            else:
                owner_phone.pop(n, None)

    rows = []
    for n, (_, p) in reg.items():
        if n not in coords:
            continue
        lat, lon = coords[n]
        owner, frn = owners.get(n, (None, None))
        rows.append((
            n, lat, lon,
            f_or_none(p[28]), f_or_none(p[30]), f_or_none(p[31]), f_or_none(p[29]),
            clean(p[32]), "Constructed" if p[8] == "C" else "Granted",
            owner, frn, clean(p[23]), clean(p[24]), clean(p[25]),
            clean(p[34]), clean(p[12]), tile_key(lat, lon),
        ))
    db.executemany("INSERT OR REPLACE INTO asr VALUES (" + ",".join("?" * 17) + ")", rows)
    db.executemany("INSERT OR REPLACE INTO asr_key VALUES (?, ?)",
                   [(n, v[0]) for n, v in reg.items() if n in coords and v[0] > 0])
    db.executemany("INSERT INTO contact VALUES ('owner', ?, ?, ?)",
                   [(n, name, ph) for n, (name, ph) in owner_phone.items() if n in coords])
    db.commit()
    log(f"ASR: {len(rows):,} structures, {sum(1 for n in owner_phone if n in coords):,} owner phones")
    return len(rows)


def load_uls(db: sqlite3.Connection, zip_path: str, cat: str) -> int:
    name = os.path.basename(zip_path)
    log(f"{name}: headers")
    active: dict[int, tuple[str, str, str]] = {}
    for p in dat_rows(zip_path, "HD.dat"):
        if len(p) > 8 and p[5] == "A" and p[1].isdigit():
            active[int(p[1])] = (p[4], p[6], p[8])

    log(f"{name}: licensees ({len(active):,} active licenses)")
    ent: dict[int, tuple[str | None, str | None]] = {}
    lic_phone: dict[int, tuple[str | None, str]] = {}  # organizations only
    for p in dat_rows(zip_path, "EN.dat"):
        if len(p) < 24 or p[5] != "L" or not p[1].isdigit():
            continue
        usi = int(p[1])
        if usi in active:
            nm = clean(p[7]) or clean(" ".join(x for x in (p[8], p[9], p[10]) if x))
            ent[usi] = (nm, clean(p[22]))
            phone = clean_phone(p[12])
            if phone and is_organization(p[23], p[8], p[10], p[7]):
                lic_phone[usi] = (clean(p[7]), phone)
            else:
                lic_phone.pop(usi, None)

    log(f"{name}: locations")
    keys: set[int] = set()
    batch = []
    for p in dat_rows(zip_path, "LO.dat"):
        if len(p) < 43 or not p[1].isdigit():
            continue
        usi = int(p[1])
        hd = active.get(usi)
        if hd is None or p[6] not in FIXED_LOCATION_TYPES:
            continue
        if cat != "microwave" and p[6] == "" and p[7] != "T":
            continue
        lat = dms(p[19], p[20], p[21], p[22])
        lon = dms(p[23], p[24], p[25], p[26])
        loc = int(p[8]) if p[8].isdigit() else 0
        if lat is None or lon is None or (lat == 0 and lon == 0) or loc >= 1000:
            continue
        sid = usi * 1000 + loc
        if sid in keys:
            continue
        keys.add(sid)
        call, svc, expires = hd
        licensee, frn = ent.get(usi, (None, None))
        batch.append((
            sid, "uls", cat, call, svc, licensee, frn, lat, lon, asr_num(p[37]),
            clean(p[42]), clean(p[11]), clean(p[12]), clean(p[14]),
            f_or_none(p[18]), f_or_none(p[39]) or f_or_none(p[38]), clean(p[40]), clean(p[7]),
            clean(expires), usi, loc, None, None, None, tile_key(lat, lon),
        ))
        if len(batch) >= 50000:
            db.executemany("INSERT INTO site VALUES (" + ",".join("?" * 25) + ")", batch)
            batch.clear()
    db.executemany("INSERT INTO site VALUES (" + ",".join("?" * 25) + ")", batch)
    batch.clear()

    log(f"{name}: frequencies ({len(keys):,} sites)")
    for p in dat_rows(zip_path, "FR.dat"):
        if len(p) < 21 or not p[1].isdigit() or not p[6].isdigit():
            continue
        sid = int(p[1]) * 1000 + int(p[6])
        if sid not in keys:
            continue
        mhz = f_or_none(p[10])
        if mhz is None:
            continue
        batch.append((sid, mhz, f_or_none(p[11]), f_or_none(p[15]),
                      f_or_none(p[16]) or f_or_none(p[20]), clean(p[8]), None))
        if len(batch) >= 100000:
            db.executemany("INSERT INTO freq VALUES (?,?,?,?,?,?,?)", batch)
            batch.clear()
    db.executemany("INSERT INTO freq VALUES (?,?,?,?,?,?,?)", batch)
    batch.clear()

    log(f"{name}: emissions")
    for p in dat_rows(zip_path, "EM.dat"):
        if len(p) < 10 or not p[1].isdigit() or not p[5].isdigit():
            continue
        sid = int(p[1]) * 1000 + int(p[5])
        if sid in keys and p[9]:
            batch.append((sid, f_or_none(p[7]), p[9].strip()))
        if len(batch) >= 100000:
            db.executemany("INSERT INTO emis VALUES (?,?,?)", batch)
            batch.clear()
    db.executemany("INSERT INTO emis VALUES (?,?,?)", batch)
    batch.clear()

    if cat == "microwave":
        log(f"{name}: paths")
        for p in dat_rows(zip_path, "PA.dat"):
            if len(p) < 11 or not p[1].isdigit():
                continue
            usi = int(p[1])
            if usi in active and p[6].isdigit() and p[7].isdigit() and p[9].isdigit():
                batch.append((usi, int(p[6]), int(p[7]), int(p[9])))
        db.executemany("INSERT INTO rawpath VALUES (?,?,?,?)", batch)
        batch.clear()

    # Contacts: the licensee's phone, and each control point's phone (where someone can
    # shut the transmitter off). Organizations only, and only for licenses with a site.
    with_site = {k // 1000 for k in keys}
    rows = [("licensee", usi, nm, ph) for usi, (nm, ph) in lic_phone.items() if usi in with_site]
    seen: set[tuple[int, str]] = set()
    per_license: dict[int, int] = {}
    for p in dat_rows(zip_path, "CP.dat"):
        if len(p) < 11 or p[0] != "CP" or not p[1].isdigit():
            continue
        usi = int(p[1])
        if usi not in with_site or usi not in lic_phone:
            continue
        phone = clean_phone(p[10])
        if not phone or (usi, phone) in seen or per_license.get(usi, 0) >= 3:
            continue
        seen.add((usi, phone))
        per_license[usi] = per_license.get(usi, 0) + 1
        place = ", ".join(x for x in (clean(p[8]), clean(p[9])) if x) or None
        rows.append(("control", usi, place, phone))
    db.executemany("INSERT INTO contact VALUES (?,?,?,?)", rows)
    log(f"{name}: {len(rows):,} contact phones ({sum(1 for r in rows if r[0] == 'control'):,} control points)")

    db.commit()
    return len(keys)


def load_broadcast(db: sqlite3.Connection, raw_dir: str) -> int:
    total = 0
    for kind, url in BROADCAST_QUERIES.items():
        path = download(url, os.path.join(raw_dir, f"{kind}_all.txt"))
        seen: set[int] = set()
        sites, freqs = [], []
        with open(path, encoding="latin-1") as f:
            for line in f:
                if not line.startswith("|"):
                    continue
                r = line.rstrip("\r\n").split("|")
                if len(r) < 33 or r[9].strip() not in BROADCAST_STATUSES:
                    continue
                lat = dms(r[20], r[21], r[22], r[19])
                lon = dms(r[24], r[25], r[26], r[23])
                if lat is None or lon is None:
                    continue
                call = clean(r[1])
                svc = clean(r[3])
                facid = clean(r[18])
                if kind == "fm":
                    mhz = f_or_none(r[2])
                    asr = asr_num(r[35]) if len(r) > 35 else None
                    extra = {"facility": facid, "class": clean(r[7]), "erpKW": f_or_none(r[14]),
                             "haatM": f_or_none(r[16]), "channel": clean(r[4]), "status": r[9].strip()}
                    height = None
                elif kind == "tv":
                    ch = f_or_none(r[4])
                    mhz = tv_channel_mhz(int(ch)) if ch else None
                    asr = asr_num(r[35]) if len(r) > 35 else None
                    extra = {"facility": facid, "rfChannel": clean(r[4]), "erpKW": f_or_none(r[14]),
                             "haatM": f_or_none(r[16]), "status": r[9].strip()}
                    height = None
                else:  # am
                    khz = f_or_none(r[2])
                    mhz = khz / 1000 if khz else None
                    asr = None
                    extra = {"facility": facid, "class": clean(r[7]), "powerKW": f_or_none(r[14]),
                             "hours": clean(r[6]), "status": r[9].strip()}
                    height = None
                sid = stable_id(kind, facid, call, lat, lon, mhz)
                if sid in seen:
                    continue
                seen.add(sid)
                erp = extra.get("erpKW") or extra.get("powerKW")
                sites.append((
                    sid, kind, kind, call, svc, clean(r[27]), None, lat, lon, asr,
                    None, None, clean(r[10]), clean(r[11]), None, height, None, None,
                    None, None, None, mhz, mhz, json.dumps({k: v for k, v in extra.items() if v is not None}),
                    tile_key(lat, lon),
                ))
                freqs.append((sid, mhz, None, None, erp, svc, None))
        db.executemany("INSERT OR IGNORE INTO site VALUES (" + ",".join("?" * 25) + ")", sites)
        db.executemany("INSERT INTO freq VALUES (?,?,?,?,?,?,?)", freqs)
        db.commit()
        log(f"broadcast {kind}: {len(sites):,} stations")
        total += len(sites)
    return total


def load_dof(db: sqlite3.Connection, raw_dir: str) -> dict | None:
    """FAA Digital Obstacle File (see dof.py). Optional: on any failure the build
    carries on without obstacles rather than losing the FCC data."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import dof

    try:
        url = os.environ.get("DOF_URL") or dof.find_zip_url()
        dest = os.path.join(raw_dir, os.path.basename(url))
        download(url, dest, headers=dof.HEADERS)
        currency, obstacles = dof.read_obstacles(dest)
        log(f"DOF: {len(obstacles):,} US obstacles, currency {currency}")
        asr_rows = db.execute("SELECT id, lat, lon, faa_study FROM asr").fetchall()
        dof.link_to_asr(obstacles, asr_rows)
        rows = dof.to_rows(obstacles, TILE_DEG)
        db.executemany("INSERT OR REPLACE INTO obstacle VALUES (" + ",".join("?" * 20) + ")", rows)
        db.commit()
        by_study = sum(1 for o in obstacles if o["match"] == "study")
        near = sum(1 for o in obstacles if o["match"] == "near")
        log(f"DOF: linked to ASR {by_study:,} by study number, {near:,} by proximity")
        return {"source": dof.SOURCE, "currency": currency, "url": url, "rows": len(rows),
                "linkedByStudy": by_study, "linkedNear": near}
    except Exception as e:  # noqa: BLE001
        print(f"::warning title=FAA obstacle layer skipped::{type(e).__name__}: {e}", flush=True)
        return None


def finalize_stage(db: sqlite3.Connection) -> None:
    log("stage: attaching emissions to frequencies")
    db.execute("CREATE INDEX emis_k ON emis(site, mhz)")
    db.execute("CREATE INDEX freq_k ON freq(site, mhz)")
    db.execute("""
        UPDATE freq SET emis = (
          SELECT group_concat(DISTINCT emis) FROM emis
          WHERE emis.site = freq.site AND abs(emis.mhz - freq.mhz) < 0.000001)
        WHERE freq.site < 4503599627370496
    """)
    db.execute("CREATE INDEX contact_k ON contact(kind, ref)")
    log("stage: de-duplicating frequencies")
    db.execute("""
        CREATE TABLE freq2 AS
        SELECT site, mhz, max(upper) upper, max(power) power, max(erp) erp,
               group_concat(DISTINCT cls) cls, max(emis) emis
        FROM freq GROUP BY site, mhz
    """)
    db.execute("DROP TABLE freq")
    db.execute("ALTER TABLE freq2 RENAME TO freq")
    db.execute("CREATE INDEX freq_site ON freq(site)")
    log("stage: site frequency ranges")
    db.execute("""
        UPDATE site SET
          fmin = (SELECT min(mhz) FROM freq WHERE freq.site = site.id),
          fmax = (SELECT max(coalesce(upper, mhz)) FROM freq WHERE freq.site = site.id)
        WHERE kind = 'uls'
    """)
    log("stage: microwave paths")
    rows = db.execute("""
        SELECT rp.usi*1000 + rp.path, t.call, t.licensee, t.id, r.id,
               t.lat, t.lon, r.lat, r.lon, t.fmin, t.fmax, t.tile, r.tile
        FROM rawpath rp
        JOIN site t ON t.id = rp.usi*1000 + rp.txloc
        JOIN site r ON r.id = rp.usi*1000 + rp.rxloc
        WHERE t.id != r.id
    """).fetchall()
    out = []
    for row in rows:
        *vals, ttile, rtile = row
        out.append((*vals, ttile))
        if rtile != ttile:
            # Second copy so the path shows up from either end's tile.
            # Same id: the app's INSERT OR REPLACE keeps just one.
            out.append((*vals, rtile))
    db.execute("CREATE TABLE path_out AS SELECT * FROM path WHERE 0")
    db.executemany("INSERT INTO path_out VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", out)
    db.execute("CREATE INDEX path_out_tile ON path_out(tile)")
    db.execute("CREATE INDEX site_tile ON site(tile)")
    db.execute("CREATE INDEX asr_tile ON asr(tile)")
    db.commit()
    log(f"stage: {len(rows):,} microwave paths")


# ---------------------------------------------------------------------------
# Output


def compress_file(src: str, dest: str) -> int:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw DEFLATE == Apple's COMPRESSION_ZLIB
    with open(src, "rb") as fi, open(dest, "wb") as fo:
        while chunk := fi.read(1 << 20):
            fo.write(c.compress(chunk))
        fo.write(c.flush())
    return os.path.getsize(dest)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def write_tiles(stage_path: str, out_dir: str, work_dir: str) -> dict:
    stage = sqlite3.connect(stage_path)
    keys = sorted({r[0] for r in stage.execute(
        "SELECT tile FROM site UNION SELECT tile FROM asr UNION SELECT tile FROM obstacle")})
    stage.close()
    tiles = {}
    for i, key in enumerate(keys, 1):
        tmp = os.path.join(work_dir, f"tile_{key}.sqlite")
        if os.path.exists(tmp):
            os.remove(tmp)
        t = sqlite3.connect(tmp)
        t.execute("PRAGMA journal_mode=OFF")
        t.executescript(TILE_SCHEMA)
        t.execute("ATTACH DATABASE ? AS s", (stage_path,))
        t.execute("INSERT INTO asr SELECT * FROM s.asr WHERE tile = ?", (key,))
        t.execute("INSERT INTO site SELECT * FROM s.site WHERE tile = ?", (key,))
        t.execute("INSERT INTO freq SELECT f.* FROM s.freq f JOIN site ON site.id = f.site")
        t.execute("INSERT OR REPLACE INTO path SELECT * FROM s.path_out WHERE tile = ?", (key,))
        t.execute("INSERT INTO obstacle SELECT * FROM s.obstacle WHERE tile = ?", (key,))
        t.execute("INSERT INTO asr_key SELECT k.* FROM s.asr_key k WHERE k.asr IN (SELECT id FROM asr)")
        t.execute("INSERT INTO contact SELECT DISTINCT c.* FROM s.contact c "
                  "WHERE c.kind = 'owner' AND c.ref IN (SELECT id FROM asr)")
        t.execute("INSERT INTO contact SELECT DISTINCT c.* FROM s.contact c "
                  "WHERE c.kind IN ('control', 'licensee') AND c.ref IN (SELECT usi FROM site WHERE usi IS NOT NULL)")
        counts = {
            "asr": t.execute("SELECT count(*) FROM asr").fetchone()[0],
            "sites": t.execute("SELECT count(*) FROM site").fetchone()[0],
            "paths": t.execute("SELECT count(*) FROM path").fetchone()[0],
            "obstacles": t.execute("SELECT count(*) FROM obstacle").fetchone()[0],
            "contacts": t.execute("SELECT count(*) FROM contact").fetchone()[0],
            "asrKeys": t.execute("SELECT count(*) FROM asr_key").fetchone()[0],
        }
        t.commit()
        t.execute("DETACH DATABASE s")
        t.executescript(TILE_INDEXES)
        t.execute("VACUUM")
        t.close()
        name = f"tile_{key}.sqlz"
        dest = os.path.join(out_dir, name)
        size = compress_file(tmp, dest)
        os.remove(tmp)
        tiles[key] = {"file": name, "bytes": size, "sha256": sha256(dest), **counts}
        if i % 25 == 0 or i == len(keys):
            log(f"tiles: {i}/{len(keys)}")
    return tiles


def write_index(stage_path: str, out_dir: str, work_dir: str) -> dict:
    tmp = os.path.join(work_dir, "index.sqlite")
    if os.path.exists(tmp):
        os.remove(tmp)
    t = sqlite3.connect(tmp)
    t.execute("PRAGMA journal_mode=OFF")
    t.execute("""CREATE TABLE lookup(
        key TEXT NOT NULL, kind TEXT NOT NULL, label TEXT,
        lat REAL NOT NULL, lon REAL NOT NULL, place TEXT)""")
    t.execute("ATTACH DATABASE ? AS s", (stage_path,))
    # One row per call sign + category, at its primary site: ULS location
    # #1, or the strongest broadcast facility (so a main station wins over
    # its own translators/boosters). Never average locations together.
    t.execute("""
        INSERT INTO lookup
        SELECT upper(call), cat, coalesce(licensee, ''), lat, lon,
               trim(coalesce(city, '') || ', ' || coalesce(state, ''), ', ')
        FROM (
          SELECT call, cat, licensee, lat, lon, city, state,
                 row_number() OVER (
                   PARTITION BY upper(call), cat
                   ORDER BY coalesce(loc, 0),
                            coalesce(json_extract(extra, '$.erpKW'), json_extract(extra, '$.powerKW'), 0) DESC
                 ) AS rn
          FROM s.site WHERE call IS NOT NULL
        ) WHERE rn = 1
    """)
    t.execute("""
        INSERT INTO lookup
        SELECT CAST(id AS TEXT), 'asr', coalesce(owner, ''), lat, lon,
               trim(coalesce(city, '') || ', ' || coalesce(state, ''), ', ')
        FROM s.asr
    """)
    t.commit()
    t.execute("DETACH DATABASE s")
    t.execute("CREATE INDEX lookup_key ON lookup(key)")
    n = t.execute("SELECT count(*) FROM lookup").fetchone()[0]
    t.execute("VACUUM")
    t.close()
    dest = os.path.join(out_dir, "index.sqlz")
    size = compress_file(tmp, dest)
    os.remove(tmp)
    log(f"index: {n:,} keys, {size / 1e6:.1f} MB")
    return {"file": "index.sqlz", "bytes": size, "sha256": sha256(dest), "rows": n}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default="raw", help="where FCC downloads are cached")
    ap.add_argument("--work", default="work", help="scratch space for the staging DB")
    ap.add_argument("--out", default="out", help="where tiles + manifest are written")
    ap.add_argument("--base-url", required=True,
                    help="URL prefix the app downloads tile files from (ends with /)")
    ap.add_argument("--version", default=dt.date.today().isoformat())
    ap.add_argument("--skip-broadcast", action="store_true")
    args = ap.parse_args()

    for d in (args.raw, args.work, args.out):
        os.makedirs(d, exist_ok=True)

    for f in os.listdir(args.out):
        os.remove(os.path.join(args.out, f))

    stage_path = os.path.join(args.work, "stage.sqlite")
    db = open_stage(stage_path)
    stats = {}

    asr_zip = download(ULS_BASE + ASR_ZIP + ".zip", os.path.join(args.raw, ASR_ZIP + ".zip"))
    stats["asr"] = load_asr(db, asr_zip)
    for src, cat in ULS_SOURCES.items():
        z = download(ULS_BASE + src + ".zip", os.path.join(args.raw, src + ".zip"))
        stats[cat] = load_uls(db, z, cat)
    if not args.skip_broadcast:
        stats["broadcast"] = load_broadcast(db, args.raw)
    dof_block = load_dof(db, args.raw)
    finalize_stage(db)
    db.close()

    tiles = write_tiles(stage_path, args.out, args.work)
    index = write_index(stage_path, args.out, args.work)
    areas_info = None
    try:
        import areas
        areas_info = areas.build(args.raw, args.work, args.out)
    except Exception as e:  # noqa: BLE001 - area licenses are an extra; never block the weekly release
        log(f"areas: skipped ({e})")

    manifest = {
        "schema": SCHEMA_VERSION,
        "version": args.version,
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "baseURL": args.base_url,
        "tileDegrees": TILE_DEG,
        "counts": stats,
        "index": index,
        "tiles": tiles,
    }
    if dof_block:
        manifest["dof"] = dof_block
    if areas_info:
        manifest["areas"] = areas_info
    with open(os.path.join(args.out, "manifest.json"), "w") as f:
        json.dump(manifest, f, separators=(",", ":"))
    total = sum(t["bytes"] for t in tiles.values())
    log(f"done: {len(tiles)} tiles, {total / 1e6:.1f} MB compressed; counts {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
