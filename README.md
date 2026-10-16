# Tower Scan data

Map-ready tiles of public FCC data for the **Tower Scan** iOS app by Fresh Coast RF.
The app code lives elsewhere. This repo holds only the build script and the weekly data releases.

## What's in a release

| File | What it is |
|---|---|
| `manifest.json` | Data version, tile list, sizes, SHA-256s, download base URL |
| `tile_<lat>_<lon>.sqlz` | One 2°×2° square of data: raw-DEFLATE-compressed SQLite |
| `index.sqlz` | Nationwide call sign / ASR number → coordinates, for search |
| `areas.sqlz` | Market-area licenses, market → county lists and county outlines |

The app reads `releases/latest/download/manifest.json` and downloads only the tiles for the areas the user searches.

Tile key = `floor(lat / 2)_floor(lon / 2)`. For example, Grand Rapids, MI (42.96, −85.67) is `21_-43`.

## Sources

All public FCC data:

- **ASR**: `r_tower.zip` from <https://data.fcc.gov/download/pub/uls/complete/>.
  RA (registration), CO (coordinates), EN (owner). Constructed and granted structures only.
- **ULS**: `l_LMpriv`, `l_LMcomm`, `l_micro`, `l_cell`, `l_paging`, `l_LMbcast` and `l_coast`, from the same place.
  HD, EN, LO, FR, EM and PA records. Active licenses only. Fixed and temporary-fixed locations only, because mobile and itinerant areas aren't sites.
- **Broadcast**: FCC FM / TV / AM query (`transition.fcc.gov/fcc-bin/{fmq,tvq,amq}`), pipe-delimited output, LIC and STA records only.

Market-area licenses (PCS, AWS, 700 MHz, 3.5 GHz and others) have no transmitter coordinates, so they go into a separate `areas.sqlz` (see `areas.py`): the licenses from `l_market.zip`, which counties make up each market (FCC county cross-reference, PEA workbook), and Census 2017 county outlines. A market is drawn as the union of its counties.

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

## FAA obstacle layer (Digital Obstacle File)

`dof.py` adds the FAA's [Digital Obstacle File](https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/dof/)
(public domain; updated every 56 days) as an `obstacle` table inside each tile: ~638,000 US obstacles
(towers, wind turbines, transmission towers, poles, buildings, stacks and more) with height, lighting, marking,
position/height accuracy, verified or not, and the FAA **aeronautical study number**.

- **Finding the file:** the build reads the FAA's DOF page for the newest `DOF_YYMMDD.zip`; if the page can't be read it
  falls back to the 56-day cycle (anchor 2026-09-27) and checks which file exists. `DOF_URL` overrides.
- **Linking to ASR towers** (`asr` + `match` columns): `study` when the obstacle's study number is the tower's and they're within
  1 km (a study can cover several structures; the nearest wins), otherwise `near` when an ASR tower is within 30 m. On the first
  run that was ~71,000 by study number and ~38,000 by position. Obstacles with no link are shown on the map as their own layer.
- It's optional: if the FAA site can't be reached the build warns and carries on without obstacles.
- The `manifest.json` `dof` block records the file's currency date and the link counts.
- Format and codes are in the FAA's DOF README. `python3 -m unittest test_dof` covers parsing, study-number normalization and linking.

The FAA's **aeronautical studies** themselves (case status, dates, determination letters) are *not* in this repo: the app looks them up
live from the FAA's OE/AAA site when you open a study. That response includes the sponsor's personal contact details, so the app reads
only structured, non-personal fields and stores nothing.

## Running it

```bash
python3 build_db.py --raw raw --work work --out out \
  --base-url "https://github.com/markoverhuel-ios/tower-scan-data/releases/download/<tag>/"
python3 check_output.py out
python3 -m unittest test_dof
```

It needs Python 3.10+ and nothing else, and takes about 10 minutes plus about 800 MB of downloads.
The GitHub Action in `.github/workflows/build.yml` runs it every Tuesday and publishes a release.
It keeps the newest 3 releases.

## Notes

Data is as filed with the FCC and may be out of date or inaccurate. Not affiliated with the FCC.
