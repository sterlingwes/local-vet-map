#!/usr/bin/env python3
"""Fetch all veterinary amenities in Ontario from OpenStreetMap (Overpass API).

Output: data/osm-vets-on.json (raw Overpass response)
"""
import json
import pathlib
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "osm-vets-on.json"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Ontario admin area, all nodes/ways/relations tagged amenity=veterinary
QUERY = """
[out:json][timeout:180];
area["ISO3166-2"="CA-ON"][admin_level=4]->.on;
nwr["amenity"="veterinary"](area.on);
out center tags;
"""


def main():
    req = urllib.request.Request(
        OVERPASS_URL,
        data=("data=" + urllib.parse.quote(QUERY)).encode(),
        headers={"User-Agent": "local-vet-map/0.1 (ownership research; github.com/sterlingwes/local-vet-map)"},
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        payload = json.load(resp)
    elements = payload.get("elements", [])
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    named = sum(1 for e in elements if e.get("tags", {}).get("name"))
    print(f"Fetched {len(elements)} veterinary features in Ontario ({named} with names) -> {OUT}")


if __name__ == "__main__":
    main()
