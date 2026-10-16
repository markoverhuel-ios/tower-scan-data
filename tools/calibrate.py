#!/usr/bin/env python3
"""
Re-checks how trustworthy the carrier-on-tower matches are, using OpenCellID's
own data as the yardstick. Run it after changing the thresholds in cells.py, or
when a new OpenCellID export looks different.

    python3 tools/calibrate.py work/stage.sqlite raw/cell_towers.csv.gz

Idea: a match is "real" if a carrier is genuinely on the tower, "chance" if some
unrelated cell site just happens to be nearby. Chance matches are spread evenly
over the ground, so the number per square metre in a ring far from the tower
(160-200 m, where real matches have faded out) tells us how many chance matches to
expect closer in. Whatever is left over in the inner rings is real.

Prints, per measurement-count bucket, the estimated % real within 30/45/60/90/120 m.
Pick KEEP_*/LIKELY_* so "likely" is ~95% real and "possible" is honest about odds.
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cells  # noqa: E402

BUCKETS = [(2, 6), (6, 20), (20, 60), (60, 200), (200, 10**9)]
RADII = (30, 45, 60, 90, 120)


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    stage, csv_path = sys.argv[1:3]
    carriers = cells.load_carriers(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "carriers.json"))
    towers = cells.load_towers(stage)
    index = cells.TowerIndex([(a, la, lo) for a, (la, lo) in towers.items()])
    agg, stats = cells.match_cells(cells.read_rows(csv_path), index, carriers)
    rows = [(a["dist"], a["samples"]) for a in agg.values() if not a["radios"] <= cells.LEGACY_RADIOS]
    print(f"{len(rows):,} tower/carrier candidates ({stats['us']:,} US cells)\n")
    print(f"{'samples':>9} | " + " | ".join(f"<= {d:3d} m" for d in RADII))
    for lo, hi in BUCKETS:
        sub = [r for r in rows if lo <= r[1] < hi]
        ring = sum(1 for r in sub if 160 <= r[0] < 200)
        bg = ring / (math.pi * (200 ** 2 - 160 ** 2))
        cols = []
        for d in RADII:
            n = sum(1 for r in sub if r[0] <= d)
            exp = bg * math.pi * d * d
            cols.append(f"{(max(0, 1 - exp / n) * 100 if n else 0):4.0f}% (n={n:5d})")
        print(f"{lo:>4}-{(str(hi) if hi < 10**9 else '+'):<4} | " + " | ".join(cols))


main()
