# local-vet-map

**Who owns your vet?** — a map of Ontario veterinary clinics that separates
clinics owned by corporate consolidators (ultimately private-equity groups or
conglomerates) from smaller, usually locally-owned independent practices.

**Map:** `docs/index.html` (a static page — enable GitHub Pages on the `docs/`
folder, or open it locally with any static file server).

## Current data (MVP)

| Category | Count | Ultimate owner |
|---|---|---|
| VetStrategy | 132 | IVC Evidensia (EQT / Silver Lake private equity, Nestlé minority) |
| NVA | 77 | JAB Holding (private equity) |
| VCA Canada | 53 | Mars Inc. |
| Independent (assumed) | 562 | — |

Every clinic record carries `source`, `status` (`verified` / `assumed`) and
`precision` (`exact` / `geocoded` / `city`) so nothing is presented as more
certain than it is:

- **Corporate** clinics come from CBC News research (January 2025) and are
  `status: verified` (as of that date).
- **Independent** clinics are OpenStreetMap veterinary locations that did not
  match any consolidator list — `status: assumed` until verified otherwise.
- ~130 corporate clinics aren't in OpenStreetMap yet; they're placed near
  their city centre (`precision: city`) and drawn faded on the map.

## Pipeline

```
data/source/cbc-corporate-clinics-2025-01.csv   # CBC research (full Canada, 639 clinics)
        │
scripts/fetch_osm.py                            # Overpass: amenity=veterinary in Ontario
        │                                       #   -> data/osm-vets-on.json
scripts/build_dataset.py                        # filter CBC to Ontario, match to OSM
        │                                       #   (exact + fuzzy name match near stated city),
        │                                       #   geocode leftovers via Nominatim (cached in
        │                                       #   data/geocode-cache.json)
docs/data/clinics.geojson                       # the map's dataset
```

Rebuild: `python3 scripts/fetch_osm.py && python3 scripts/build_dataset.py`
(no dependencies beyond Python 3 stdlib; Nominatim calls are rate-limited to
1/s and cached, so re-runs are fast).

## Known limitations

- **OSM under-covers Ontario**: the College of Veterinarians of Ontario (CVO)
  accredits ~2,400 facilities; OSM knows ~690 named ones. The independent
  count is therefore a floor, and 128 corporate clinics lack exact addresses.
- The CBC list is a January 2025 snapshot; consolidators keep acquiring.
- A handful of same-named clinics in the same region could mis-match despite
  the 50 km city-proximity guard.

## Roadmap / maintenance ideas

1. **CVO public register** (best next step): the authoritative list of every
   accredited facility in Ontario, with addresses —
   `https://cvo.ca.thentiacloud.net/webs/cvo/register/#/`. It sits behind an
   AWS WAF browser challenge, so it needs either a real-browser scrape (from a
   normal machine) or a data request to CVO. This would replace OSM as the
   baseline universe and fix all city-level locations. Other provinces'
   colleges have equivalent registers (many also on Thentia), which is the
   path to all-of-Canada coverage.
2. **Consolidator clinic finders**: VetStrategy exposes its clinic list at
   `https://vetstrategy.com/wp-json/wp/v2/clinics` (names/slugs only;
   addresses render client-side). VCA Canada and NVA have location pages.
   Scraping these periodically would keep the corporate list current without
   waiting for journalism.
3. **Job postings correlation**: consolidator career sites (NVA, VCA, IVC/
   VetStrategy) list clinic names + cities in postings — a signal for
   newly-acquired clinics not yet in any list.
4. **Press releases / news**: acquisition announcements name clinics; a
   periodic search per consolidator brand would catch changes.
5. **Independent verification**: cross-check "assumed independent" clinics
   against consolidator brand pages before upgrading their status; a business
   association of independent clinics could confirm members in bulk.
6. **Other consolidators**: the CBC list covers NVA, VetStrategy and VCA. P3
   Veterinary Partners, Vetcare Canada and smaller roll-ups are absent —
   worth adding.

## Sources

- CBC News research on corporate-owned veterinary clinics (January 2025) —
  `data/source/cbc-corporate-clinics-2025-01.csv`
- OpenStreetMap contributors (clinic locations, ODbL) via Overpass API
- Nominatim (geocoding, city centroids)
- Basemap tiles: CARTO / OpenStreetMap
