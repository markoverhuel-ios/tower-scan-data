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
# Phone contacts (organizations only). Zero means the parsing broke, not that the FCC has none.
contacts = sum(t.get("contacts", 0) for t in m["tiles"].values())
if contacts < 100_000:
    problems.append(f"contacts: {contacts:,} < 100,000")
if len(m["tiles"]) < MIN_TILES:
    problems.append(f"tiles: {len(m['tiles'])} < {MIN_TILES}")
for t in list(m["tiles"].values()) + [m["index"]] + ([m["areas"]] if "areas" in m else []):
    if not os.path.exists(os.path.join(out, t["file"])):
        problems.append(f"missing file {t['file']}")

# Area licenses are optional (the build skips them if the FCC boundary files are unreachable),
# but a half-built set isn't.
areas = m.get("areas")
if areas and areas.get("rows", 0) < 80_000:
    problems.append(f"areas: only {areas.get('rows', 0):,} licenses, expected ~105,000")

# The FAA obstacle layer is optional too, but a half-built one isn't.
dof = m.get("dof")
if dof and dof.get("rows", 0) < 400_000:
    problems.append(f"dof: only {dof.get('rows', 0):,} obstacles, expected ~630,000")

if problems:
    print("Build looks wrong, not publishing:\n  " + "\n  ".join(problems))
    sys.exit(1)
print(f"OK: {len(m['tiles'])} tiles, {contacts:,} phone contacts, counts {m['counts']}"
      + (f", {dof['rows']:,} FAA obstacles" if dof else ", no FAA obstacle layer"))
