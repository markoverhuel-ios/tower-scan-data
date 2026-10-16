#!/usr/bin/env python3
"""
Refuses to publish an obviously broken build (e.g. an FCC download that
came back truncated, or the broadcast query being blocked), so a bad
week never replaces the last good release.
"""

import json
import os
import sys

MINIMUMS = {
    "asr": 120_000,
    "landMobile": 300_000,
    "microwave": 250_000,
    "broadcast": 30_000,
}
MIN_TILES = 400

out = sys.argv[1] if len(sys.argv) > 1 else "out"
m = json.load(open(os.path.join(out, "manifest.json")))
problems = []
for key, minimum in MINIMUMS.items():
    got = m["counts"].get(key, 0)
    if got < minimum:
        problems.append(f"{key}: {got:,} < {minimum:,}")
if len(m["tiles"]) < MIN_TILES:
    problems.append(f"tiles: {len(m['tiles'])} < {MIN_TILES}")
for t in list(m["tiles"].values()) + [m["index"]]:
    if not os.path.exists(os.path.join(out, t["file"])):
        problems.append(f"missing file {t['file']}")

# The carrier layer is optional, but if it's there it must be sane.
cells = m.get("cells")
if cells:
    if cells.get("license") != "CC BY-SA 4.0" or not cells.get("attribution"):
        problems.append("cells: missing license/attribution in manifest")
    if not cells["tiles"]:
        problems.append("cells: layer present but has no tiles")
    for t in cells["tiles"].values():
        if not os.path.exists(os.path.join(out, t["file"])):
            problems.append(f"missing file {t['file']}")

if problems:
    print("Build looks wrong, not publishing:\n  " + "\n  ".join(problems))
    sys.exit(1)
print(f"OK: {len(m['tiles'])} tiles, counts {m['counts']}"
      + (f", carrier rows {cells['stats']['rowsOut']:,}" if cells else ", no carrier layer"))
