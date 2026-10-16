#!/usr/bin/env python3
"""
Regenerates carriers.json (MCC-MNC -> carrier name) for US networks from the
community list https://github.com/pbakondy/mcc-mnc-list (MIT).

    python3 tools/make_carriers.py > carriers.json
    python3 tools/make_carriers.py mcc-mnc-list.json > carriers.json   # offline copy

Brand names are merged where one carrier appears under several brands, and
mergers are noted in the name. Edit ALIASES, not carriers.json, to change names.
"""
import json
import re
import sys
import urllib.request

URL = "https://raw.githubusercontent.com/pbakondy/mcc-mnc-list/master/mcc-mnc-list.json"
US_COUNTRY_CODES = {"US", "PR", "VI", "GU", "AS", "MP"}

# exact brand -> display name
ALIASES = {
    "Verizon Wireless": "Verizon",
    "Verizon": "Verizon",
    "Cellco Partnership": "Verizon",
    "AT&T Mobility": "AT&T",
    "AT&T": "AT&T",
    "Cingular": "AT&T",
    "FirstNet": "FirstNet (AT&T)",
    "Sprint": "Sprint (now T-Mobile)",
    "Nextel": "Sprint (now T-Mobile)",
    "Dish": "Dish Wireless",
    "Boost Mobile": "Dish Wireless",
    "U.S. Cellular": "UScellular",
    "US Cellular": "UScellular",
}


def main() -> None:
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)
    else:
        with urllib.request.urlopen(URL, timeout=60) as r:
            data = json.load(r)
    # The list repeats a code once per territory that shares it (310-410 is
    # AT&T on the mainland but "Liberty" in the USVI). Mainland US wins; a
    # territory entry is only used when no mainland entry exists.
    out: dict[str, str] = {}
    from_us: set[str] = set()
    for x in data:
        if x.get("countryCode") not in US_COUNTRY_CODES:
            continue
        name = (x.get("brand") or x.get("operator") or "").strip()
        name = ALIASES.get(name, name)
        name = re.sub(r"\s+", " ", name)
        if not name:
            continue
        key = f"{x['mcc']}-{x['mnc']}"
        if x["countryCode"] == "US":
            out[key] = name
            from_us.add(key)
        elif key not in from_us and key not in out:
            out[key] = name
    json.dump(dict(sorted(out.items())), sys.stdout, indent=0, ensure_ascii=False)
    print()


main()
