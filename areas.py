#!/usr/bin/env python3
"""
Area-license dataset for the app: the FCC's market-based licenses (PCS, AWS,
700 MHz, 3.5 GHz, ...) which have a licensed *area* instead of a transmitter site.

Output: one raw-DEFLATE SQLite file (areas.sqlz) with
  market(code, kind, name)             e.g. BEA172 / "Grand Rapids-Muskegon-Holland, MI"
  county_market(fips, code)            which counties make up a market
  county(fips, name, state, minlat, minlon, maxlat, maxlon, geom)
                                       simplified Census county outline, float32 BLOB:
                                       [ring count u32] then per ring [n u32][lat f32, lon f32] * n
  license(usi, call, licensee, frn, svc, svc_name, band, block, market, expires)

A market's outline is the union of its counties, so the app draws a market by
drawing its counties together. No geometry library is needed.

Sources (public): FCC ULS l_market.zip; FCC county cross-references
(transition.fcc.gov/bureaus/oet/info/maps/areas); Census 2017 cartographic county
boundaries (1:20m). Standard library only.
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
import struct
import zipfile

import build_db as b

FCC_AREAS = "https://transition.fcc.gov/bureaus/oet/info/maps/areas/"
CENSUS_COUNTIES = "https://www2.census.gov/geo/tiger/GENZ2017/shp/cb_2017_us_county_20m.zip"

# Cross-reference columns -> market code prefix.
XREF_KINDS = ["cma", "bta", "mta", "rpc", "bea", "mea", "rea", "eag", "vpc"]
NAME_FILES = {"CMA": "cmanames", "BTA": "btanames", "MTA": "mtanames", "RPC": "rpcnames",
              "BEA": "beanames", "MEA": "meanames", "REA": "reanames", "EAG": "eagnames", "VPC": "vpcnames"}

# ULS radio service codes seen in market licenses. Only codes whose meaning is certain are
# named here; anything else keeps the generic name the app falls back to.
SERVICE_NAMES = {
    "CW": "Broadband PCS", "CN": "Narrowband PCS", "AW": "AWS-1", "PL": "3.5 GHz Priority Access (CBRS)",
    "WY": "700 MHz Lower (A, B, E)", "WZ": "700 MHz Lower (C, D)",
}

# Band plans for the services above, by channel block. Wording: paired bands as "up / down".
BANDS = {
    "CW": {"A": "1850–1865 / 1930–1945 MHz", "B": "1870–1885 / 1950–1965 MHz", "C": "1895–1910 / 1975–1990 MHz",
           "D": "1865–1870 / 1945–1950 MHz", "E": "1885–1890 / 1965–1970 MHz", "F": "1890–1895 / 1970–1975 MHz"},
    "AW": {"A": "1710–1720 / 2110–2120 MHz", "B": "1720–1730 / 2120–2130 MHz", "C": "1730–1735 / 2130–2135 MHz",
           "D": "1735–1740 / 2135–2140 MHz", "E": "1740–1745 / 2140–2145 MHz", "F": "1745–1755 / 2145–2155 MHz"},
    "WY": {"A": "698–704 / 728–734 MHz", "B": "704–710 / 734–740 MHz", "E": "722–728 MHz"},
    "WZ": {"C": "710–716 / 740–746 MHz", "D": "716–722 MHz"},
}
CBRS_BAND = "3550–3650 MHz"


def read_dbf(data: bytes):
    n, hlen, rlen = struct.unpack("<xxxxIHH", data[:12])
    fields, pos = [], 32
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\0")[0].decode("ascii")
        fields.append((name, data[pos + 16]))
        pos += 32
    for i in range(n):
        rec = data[hlen + i * rlen: hlen + (i + 1) * rlen]
        off, row = 1, {}
        for name, ln in fields:
            row[name] = rec[off:off + ln].decode("latin-1").strip()
            off += ln
        yield row


def read_polygons(shp: bytes):
    """Yields, per record, a list of rings [(lat, lon), ...] from a polygon .shp."""
    pos = 100
    while pos + 8 <= len(shp):
        _, words = struct.unpack(">ii", shp[pos:pos + 8])
        body = shp[pos + 8: pos + 8 + words * 2]
        pos += 8 + words * 2
        if struct.unpack("<i", body[:4])[0] != 5:
            yield []
            continue
        nparts, npts = struct.unpack("<ii", body[36:44])
        parts = list(struct.unpack(f"<{nparts}i", body[44:44 + 4 * nparts])) + [npts]
        pts = struct.unpack(f"<{npts * 2}d", body[44 + 4 * nparts: 44 + 4 * nparts + 16 * npts])
        rings = []
        for a, z in zip(parts, parts[1:]):
            rings.append([(pts[2 * i + 1], pts[2 * i]) for i in range(a, z)])
        yield rings


def encode_rings(rings) -> bytes:
    out = [struct.pack("<I", len(rings))]
    for r in rings:
        out.append(struct.pack("<I", len(r)))
        out.append(struct.pack(f"<{len(r) * 2}f", *[v for pt in r for v in pt]))
    return b"".join(out)


def read_xlsx_pairs(path: str) -> dict[str, int]:
    """FIPS -> PEA number from the FCC's PEA workbook (read with the standard library)."""
    z = zipfile.ZipFile(path)
    ss = [re.sub(r"<[^>]+>", "", m) for m in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode(), re.S)]
    out: dict[str, int] = {}
    for name in z.namelist():
        if not re.match(r"xl/worksheets/sheet\d+\.xml", name):
            continue
        for row in re.findall(r"<row[^>]*>(.*?)</row>", z.read(name).decode(), re.S):
            vals = []
            for attrs, v in re.findall(r"<c ([^>]*)>\s*<v>([^<]*)</v>", row):
                vals.append(ss[int(v)] if 't="s"' in attrs else v)
            if len(vals) >= 2 and re.fullmatch(r"\d{1,3}", vals[0]) and re.fullmatch(r"\d{4,5}", vals[1]):
                out[vals[1].zfill(5)] = int(vals[0])
    return out


def build(raw_dir: str, work_dir: str, out_dir: str) -> dict:
    b.log("areas: county shapes")
    cz = b.download(CENSUS_COUNTIES, os.path.join(raw_dir, "cb_county_20m.zip"))
    with zipfile.ZipFile(cz) as z:
        base = next(n for n in z.namelist() if n.endswith(".shp"))[:-4]
        shapes = list(read_polygons(z.read(base + ".shp")))
        dbf = list(read_dbf(z.read(base + ".dbf")))

    b.log("areas: cross-references")
    xref_path = b.download(FCC_AREAS + "xref/xrefcnty1999.txt", os.path.join(raw_dir, "xrefcnty1999.txt"))
    xref: dict[str, dict] = {}
    import csv
    with open(xref_path, newline="", encoding="latin-1") as f:
        for row in csv.DictReader(f):
            xref[row["FIPS"].zfill(5)] = row
    pea_path = b.download(FCC_AREAS + "data/FCC_PEA_website.xlsx", os.path.join(raw_dir, "FCC_PEA_website.xlsx"))
    pea = read_xlsx_pairs(pea_path)

    names: dict[str, str] = {}
    for kind, fn in NAME_FILES.items():
        p = b.download(FCC_AREAS + f"names/{fn}.txt", os.path.join(raw_dir, fn + ".txt"))
        for line in open(p, encoding="latin-1"):
            m = re.match(r'\s*(\d+)[\s,]+"?(.*?)"?\s*$', line)
            if m:
                names[f"{kind}{int(m.group(1)):03d}"] = m.group(2)

    path = os.path.join(work_dir, "areas.sqlite")
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE market(code TEXT PRIMARY KEY, kind TEXT, name TEXT);
        CREATE TABLE county_market(fips TEXT, code TEXT);
        CREATE TABLE county(fips TEXT PRIMARY KEY, name TEXT, state TEXT,
            minlat REAL, minlon REAL, maxlat REAL, maxlon REAL, geom BLOB);
        CREATE TABLE license(usi INTEGER, call TEXT, licensee TEXT, frn TEXT, svc TEXT, svc_name TEXT,
            band TEXT, block TEXT, market TEXT, expires TEXT);
    """)

    # Counties + which markets each belongs to.
    nc = 0
    for rings, rec in zip(shapes, dbf):
        fips = rec.get("GEOID", "").zfill(5)
        if not rings or not fips:
            continue
        lats = [p[0] for r in rings for p in r]
        lons = [p[1] for r in rings for p in r]
        x = xref.get(fips, {})
        db.execute("INSERT INTO county VALUES (?,?,?,?,?,?,?,?)",
                   (fips, rec.get("NAME"), x.get("State"), min(lats), min(lons), max(lats), max(lons), encode_rings(rings)))
        nc += 1
        codes = [f"{k.upper()}{int(x[k]):03d}" for k in XREF_KINDS if x.get(k, "").strip().isdigit()]
        if fips in pea:
            codes.append(f"PEA{pea[fips]:03d}")
        codes.append(f"D{fips}")  # county-level licenses
        db.executemany("INSERT INTO county_market VALUES (?,?)", [(fips, c) for c in codes])

    # Licenses.
    mz = b.download(b.ULS_BASE + "l_market.zip", os.path.join(raw_dir, "l_market.zip"))
    hd = {}
    for p in b.dat_rows(mz, "HD.dat"):
        if len(p) > 8 and p[5] == "A":
            hd[p[1]] = (p[4], p[6], p[8])           # call, service, expiry
    owners = {}
    for p in b.dat_rows(mz, "EN.dat"):
        if len(p) > 22 and p[5] == "L" and p[1] in hd:
            owners[p[1]] = (p[7], p[22])
    known = {r[0] for r in db.execute("SELECT code FROM county_market")}
    mk_names: dict[str, str] = {}
    rows, nat = [], 0
    for p in b.dat_rows(mz, "MK.dat"):
        if len(p) < 9 or p[1] not in hd:
            continue
        usi, code, block = p[1], p[5], p[6]
        mk_names.setdefault(code, p[8])
        if code not in known and not code.startswith("NWA"):
            continue                                  # market types we can't draw (yet)
        call, svc, exp = hd[usi]
        o = owners.get(usi, (None, None))
        band = BANDS.get(svc, {}).get(block) or (CBRS_BAND if svc == "PL" else None)
        rows.append((int(usi), call, o[0], o[1], svc, SERVICE_NAMES.get(svc), band, block, code, exp))
        nat += code.startswith("NWA")
    db.executemany("INSERT INTO license VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    used = {r[8] for r in rows}
    for code in used:
        kind = re.match(r"[A-Z]+", code).group(0) if not re.match(r"D\d", code) else "COUNTY"
        db.execute("INSERT INTO market VALUES (?,?,?)",
                   (code, kind, names.get(code) or mk_names.get(code) or code))
    db.execute("CREATE INDEX lic_market ON license(market)")
    db.execute("CREATE INDEX cm_fips ON county_market(fips)")
    db.execute("CREATE INDEX cm_code ON county_market(code)")
    db.commit()
    db.execute("VACUUM")
    db.close()

    dest = os.path.join(out_dir, "areas.sqlz")
    size = b.compress_file(path, dest)
    info = {"file": "areas.sqlz", "bytes": size, "sha256": b.sha256(dest), "rows": len(rows)}
    b.log(f"areas: {len(rows)} licenses ({nat} nationwide), {nc} counties, {size / 1e6:.1f} MB")
    return info
