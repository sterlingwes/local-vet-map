# local-vet-map

**Who owns your vet?** — a map of every accredited companion-animal veterinary
practice in Ontario, separating clinics owned by corporate consolidators
(ultimately private-equity groups or conglomerates) from local multi-location
groups, institutional clinics, and independent practices.

**Map:** `docs/index.html` (a static page — enable GitHub Pages on the `docs/`
folder, or open it locally with any static file server).

## Data model

The clinic universe is the **CVO (College of Veterinarians of Ontario) public
register** — every Active accredited practice, with street addresses — filtered
to companion-animal and specialty practices. Ownership is layered on top:

| Category | How it's determined | Status |
|---|---|---|
| VetStrategy → IVC Evidensia (EQT / Silver Lake PE, Nestlé minority) | CBC News research (Jan 2025) | verified |
| NVA → JAB Holding (PE) | CBC News research (Jan 2025) | verified |
| VCA → Mars Inc. | CBC list **or** registered under the `VCA` brand in the CVO register | verified |
| Local group (3+ locations) | Same registrant is director/owner of 3+ practices (CVO register) | verified |
| Institutional / non-profit | SPCA, humane societies, municipal animal services, college/university clinics | verified |
| Independent | No corporate signal found | **assumed** |

Every record carries `source`, `status`, and `precision`
(`address` / `postal` / `city`) so nothing is presented as more certain than
it is. Popups show the registered directors/owners and how long the practice
has been active.

Note: "Local group" is not the same as corporate — most are successful local
vets who own 2–3 clinics in neighbouring towns. But some are structured chains
(e.g. Juno Veterinary, 7 locations, venture-capital-backed), so they're worth
distinguishing from single-location independents.

## Pipeline

```
data/cvo-ontario-vet-practices.json        # CVO organization/search dump (all Active practices)
        │
scripts/fetch_cvo_details.py               # per-practice details from the CVO register API
        │                                  #   -> data/cvo-practices.json (extracted)
        │                                  #   -> data/cvo-director-correlation.json
        │                                  #   (raw cache data/cvo-details-raw.json is
        │                                  #    gitignored; the script resumes from it)
data/source/cbc-corporate-clinics-2025-01.csv   # CBC research (full Canada, 639 clinics)
        │
scripts/build_dataset.py                   # filter to companion/specialty, classify
        │                                  #   ownership, geocode addresses via Nominatim
        │                                  #   (cached in data/geocode-cache.json)
docs/data/clinics.geojson                  # the map's dataset
```

Rebuild: `python3 scripts/fetch_cvo_details.py && python3 scripts/build_dataset.py`
(Python 3 stdlib only. Both scripts cache aggressively; a fresh rebuild with a
warm cache takes seconds, a cold one ~45 minutes of polite rate-limited
fetching.)

`scripts/fetch_osm.py` (the original OpenStreetMap baseline) is kept for
cross-checking but is no longer part of the main pipeline.

## Director correlation

`data/cvo-director-correlation.json` lists every registrant tied to 2+
practices (175 as of the first pull). Useful signals found this way:

- CVO registers some VCA clinics under their rebranded names ("VCA Scarborough
  Animal Hospital") that name-matching against the CBC list would miss.
- A registrant who is plain "Director" (not "Director & Owner") of several
  practices tends to be a consolidator's nominal director — e.g. one vet
  directs 5 VCA clinics, another directs 4 CBC-listed corporate clinics.
- Owner-operated chains (Juno Veterinary, East Village Animal Hospital,
  Heartland, etc.) show up as "Director & Owner" across locations.

## Known limitations

- The CBC list is a January 2025 snapshot; consolidators keep acquiring.
  VetStrategy/NVA clinics acquired since — or never reported — are still
  shown as independent (hence "assumed").
- Only consolidators in the CBC data (NVA, VetStrategy, VCA) are flagged.
  P3 Veterinary Partners, Vetcare Canada and other roll-ups are not yet
  tracked.
- Some addresses geocode only to postal-code or city level (drawn faded).
- The CVO register's employment records can lag reality (directors change).

## Roadmap / maintenance ideas

1. **Refresh loop**: re-run the two scripts periodically; the CVO register is
   the ground truth for openings/closures, the consolidator clinic finders
   (e.g. `https://vetstrategy.com/wp-json/wp/v2/clinics`) for ownership drift.
2. **Director-graph expansion**: flag practices sharing a plain-"Director"
   registrant with known-corporate practices as "suspected corporate".
3. **Job postings / press releases**: consolidator career sites and
   acquisition announcements catch newly-acquired clinics early.
4. **Other consolidators**: add P3 Veterinary Partners, Vetcare, etc.
5. **All of Canada**: every provincial college has an equivalent register
   (many also on Thentia Cloud) — the same pipeline generalizes.

## Sources

- CVO public register (practice universe, addresses, directors) —
  `https://cvo.ca.thentiacloud.net/webs/cvo/register/`
- CBC News research on corporate-owned veterinary clinics (January 2025) —
  `data/source/cbc-corporate-clinics-2025-01.csv`
- Nominatim / OpenStreetMap (geocoding)
- Basemap tiles: CARTO / OpenStreetMap
