# Tower Scan data

Map-ready tiles of public FCC data for the **Tower Scan** iOS app by Fresh Coast RF.
The app code lives elsewhere. This repo holds only the build script and the weekly data releases.

## What's in a release

| File | What it is |
|---|---|
| `manifest.json` | Data version, tile list, sizes, SHA-256s, download base URL |
| `tile_<lat>_<lon>.sqlz` | One 2°×2° square of data: raw-DEFLATE-compressed SQLite |
| `index.sqlz` | Nationwide call sign / ASR number → coordinates, for search |

The app reads `releases/latest/download/manifest.json` and downloads only the tiles for the areas the user searches.

Tile key = `floor(lat / 2)_floor(lon / 2)`. For example, Grand Rapids, MI (42.96, −85.67) is `21_-43`.

## Sources

All public FCC data:

- **ASR**: `r_tower.zip` from <https://data.fcc.gov/download/pub/uls/complete/>.
  RA (registration), CO (coordinates), EN (owner). Constructed and granted structures only.
- **ULS**: `l_LMpriv`, `l_LMcomm`, `l_micro`, `l_cell`, `l_paging`, `l_LMbcast` and `l_coast`, from the same place.
  HD, EN, LO, FR, EM and PA records. Active licenses only. Fixed and temporary-fixed locations only, because mobile and itinerant areas aren't sites.
- **Broadcast**: FCC FM / TV / AM query (`transition.fcc.gov/fcc-bin/{fmq,tvq,amq}`), pipe-delimited output, LIC and STA records only.

Market-area licenses (AWS, PCS, 700 MHz blocks) have no transmitter coordinates in ULS, so they aren't included.

## Field positions (0-based, checked against real files 2026-10-15)

- **LO**:
  - 1 usi
  - 4 call sign
  - 6 location type (F/6/T kept)
  - 7 location class
  - 8 location number
  - 11 address
  - 12 city
  - 14 state
  - 18 ground elevation
  - 19–22 latitude (d, m, s, hemisphere)
  - 23–26 longitude
  - 37 ASR number
  - 38 support structure height
  - 39 overall height
  - 40 structure type
  - 42 location name
- **HD**:
  - 1 usi
  - 4 call sign
  - 5 status (A = active)
  - 6 radio service
  - 8 expiration date
- **EN** (ULS):
  - 1 usi
  - 5 entity type (L = licensee)
  - 7 name
  - 22 FRN
- **FR**:
  - 1 usi
  - 6 location number
  - 8 station class
  - 10 frequency
  - 11 upper frequency
  - 15 output power
  - 16 ERP
  - 20 EIRP
- **EM**:
  - 1 usi
  - 5 location number
  - 7 frequency
  - 9 emission designator
- **PA**:
  - 1 usi
  - 6 path number
  - 7 transmit location
  - 9 receive location
- **RA** (ASR):
  - 1 REG
  - 3 registration number
  - 4 usi
  - 8 status (C/G kept)
  - 12 date constructed
  - 23–25 address, city, state
  - 28 structure height
  - 29 ground elevation
  - 30 overall height AGL
  - 31 overall height AMSL
  - 32 structure type
  - 34 FAA study number
- **CO** (ASR):
  - 4 usi
  - 5 coordinate type (T)
  - 6–9 latitude
  - 11–14 longitude
- **EN** (ASR):
  - 4 usi
  - 5 entity type (O = owner)
  - 9 name
  - 24 FRN
- **fmq / tvq**:
  - 1 call sign
  - 2 frequency
  - 3 service
  - 4 channel
  - 9 status
  - 14 ERP
  - 16 HAAT
  - 18 facility ID
  - 19–26 coordinates
  - 27 licensee
  - 35 ASR number

## Carrier layer (OpenCellID)

`cells.py` adds an **optional** layer saying which cellular carriers are probably on each ASR tower.
It is built only when the build has an OpenCellID API token (or a local file), so the FCC data
is unaffected without one.

- Source: OpenCellID's full export (`cell_towers.csv.gz`, CC BY-SA 4.0, towers seen in the last 18 months).
  Each US cell has an *estimated* position. Each cell is matched to the nearest ASR structure within
  200 m, then rolled up per tower and carrier. Carrier names come from MCC/MNC (`carriers.json`).
- **Confidence:** `likely` means a cell within 60 m, at least 10 measurements, and no other tower within
  30 m of it. Everything else that matched is `possible`. Towers within 30 m of each other can't be told
  apart given the position noise, so each of them gets the cell, as `possible`.
- Most cell sites sit on structures too short to need an ASR, so most cells won't match any tower. That's expected.
- Output is separate files (`cells_<tile>.sqlz`, a few KB each) plus a `cells` block in `manifest.json`.
  Rows carry `source` and `source_date`. See [DATA-LICENSES.md](DATA-LICENSES.md).

**Setup (once):**
1. Register at <https://my.opencellid.org/register> and copy the API access token.
2. `gh secret set OPENCELLID_TOKEN --repo markoverhuel-ios/tower-scan-data`, then paste it when prompted.
3. Confirm with Unwired Labs (OpenCellID) that your use, including a paid app, is covered by your plan.
   The docs list commercial plans, and downloads are limited to 2 per file per day.

**Locally** (uses the same build, reusing an existing `work/stage.sqlite` and `out/manifest.json`):

```bash
python3 build_db.py --cells-only --raw raw --work work --out out \
  --base-url "https://github.com/markoverhuel-ios/tower-scan-data/releases/download/<tag>/" \
  --opencellid-file path/to/cell_towers.csv.gz
```

**Tuning the carrier match.** The distance and sample thresholds at the top of `cells.py`
(`MAX_DIST_M`, `LIKELY_DIST_M`, `LIKELY_MIN_SAMPLES`, `AMBIGUOUS_GAP_M`) were set without real data.
After the first real run, spot-check a few towers you know and adjust them. `python3 -m unittest test_cells`
covers the matching rules.

`carriers.json` is generated by `tools/make_carriers.py` from a community MCC/MNC list that hasn't been
updated since 2023, so a very new network code shows as "Carrier 310-xxx".

## Running it

```bash
python3 build_db.py --raw raw --work work --out out \
  --base-url "https://github.com/markoverhuel-ios/tower-scan-data/releases/download/<tag>/"
python3 check_output.py out
python3 -m unittest test_cells
```

It needs Python 3.10+ and nothing else, and takes about 10 minutes plus about 800 MB of downloads.
The GitHub Action in `.github/workflows/build.yml` runs it every Tuesday and publishes a release.
It keeps the newest 3 releases.

## Notes

Data is as filed with the FCC and may be out of date or inaccurate. Not affiliated with the FCC.
