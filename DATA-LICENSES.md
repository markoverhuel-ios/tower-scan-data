# Data licenses

This repo publishes data built from several sources. (The app also looks up FAA aeronautical studies live from the FAA's OE/AAA site; nothing from that is published or stored here.) They are **not** all under the same terms.

| Files | Source | License |
|---|---|---|
| `tile_*.sqlz` (FCC data plus the `obstacle` table), `index.sqlz` | FCC ASR, ULS and broadcast databases, and the FAA Digital Obstacle File (the `obstacle` table) | US government public data; no restrictions. FCC data is as filed and may be out of date. The FAA says the DOF covers only objects that affect aeronautical charting, not every obstruction, and that "unverified" records have unverified positions and heights. |
| `cells.sqlz` | **OpenCellID** (<https://opencellid.org>), (c) OpenCellID contributors | **CC BY-SA 4.0** (<https://creativecommons.org/licenses/by-sa/4.0/>) |
| `carriers.json` (code only: MCC-MNC to carrier name) | [pbakondy/mcc-mnc-list](https://github.com/pbakondy/mcc-mnc-list) | MIT |
| `*.py`, workflow | this repo | (add a license before accepting outside contributions) |

## OpenCellID layer (`cells.sqlz`)

- Cell positions are crowd-sourced estimates. Each row says which tower we *assumed* a carrier is on,
  with a `confidence` of `likely` or `possible`. It is not confirmation that a carrier is on that tower.
- These files are an adaptation of the OpenCellID database. They are published under **CC BY-SA 4.0**,
  so anyone who reuses them must credit OpenCellID, link the license, say what was changed, and share
  derivatives under the same license.
- **Attribution to show wherever the data is displayed:**
  "Cell tower data from OpenCellID, licensed under CC BY-SA 4.0." with links to <https://opencellid.org>
  and the license. State the change: "Cell positions matched to the nearest FCC antenna structure (ASR) and
  grouped by carrier; carrier names derived from MCC/MNC."
- Every row carries `source` and `source_date`; every file carries a `meta` table with the same text;
  `manifest.json` has the wording the app displays (`cells.attribution`, `cells.changes`).
- The FCC tiles are kept in separate files on purpose, so the ShareAlike terms apply only to `cells.sqlz`.

This is a description of how the project intends to comply, not legal advice.
