#!/usr/bin/env python3
"""Fetch every IVC Evidensia-owned clinic worldwide from their referral-guide tool.

https://ivcevidensia.com/referral-guide embeds a Next.js app
(external-referral-ui.azurewebsites.net) whose search API takes a lat/lon and
returns every clinic (IVC-owned or not — it's a shared referral network) within
`range` miles, capped at 621 (the tool's own max radius). There's no "list all"
endpoint, so this script walks a grid of points spaced so their 621-mile search
circles cover the whole world, and keeps only the clinics IVC actually owns.

Ownership detection: the API's own `ivcClinic` flag turns out to be unreliable
— it's `true` for the UK/Ireland (verified against ~all of that data) but
`false` for *every* Canadian clinic returned, including ones this repo already
knows are VetStrategy (=IVC-owned) from data/source/cbc-corporate-clinics-2025-01.csv
(e.g. "Sherbourne Animal Hospital", Toronto). `phcStatus == "National PHC (in
EDD)"` is a second signal that catches ~96% precision but only ~40% recall on
its own for Canada. So this script combines three signals: the ivcClinic flag,
the phcStatus signal, and — for Canada specifically, where we have a trusted
independent source — a normalized name match against the CBC VetStrategy list.
Every output record carries which signal(s) matched it, in the spirit of this
repo's source/status/precision transparency (see README).

Known gap: the referral network also returns hundreds of clinics in Germany,
Sweden, Spain, Finland, Norway, Portugal, Switzerland, Netherlands, Belgium,
and Denmark, but *none* of them carry the ivcClinic flag, the phcStatus
signal, or (we have no equivalent source list to cross-check against, unlike
Canada's CBC data) any other ownership marker in this dataset. IVC Evidensia
does own clinics in continental Europe (mostly under the AniCura brand), so
this script currently undercounts there — it only has good signal for the
UK, Ireland, and Canada. Extending coverage would need another verified
source list (e.g. AniCura's own clinic finder) to cross-reference against,
the way we do for Canada.

The site also filters by animalType (1=small animal, 2=equine, 3=farm, plus
4-6 seen but rare); a clinic can appear under one type and not another (e.g.
equine-only referral centres). We only care about small-animal (companion)
clinics here, so we only query animalType=1 — pass --animal-types to widen
that if needed.

To avoid burning requests on open ocean, the grid is clipped to a handful of
coarse continental bounding boxes (see CONTINENT_BBOXES) rather than sweeping
the full globe. These are deliberately generous rectangles, not real
coastlines, so they still include some empty sea/desert — but they cut out
the Pacific/Atlantic/Southern/Arctic oceans and Antarctica, which is most of
Earth's surface and all of it clinic-free.

Cache:  data/ivc-search-raw-cache.json   ("lat,lon,animalType" -> raw clinics
        list; gitignored, resumable — rerun to continue after an interruption)
Input:  data/source/cbc-corporate-clinics-2025-01.csv  (for the Canada name match)
Output: data/ivc-evidensia-clinics-world.json  (deduped, IVC-owned only)

Run: python3 scripts/fetch_ivc_evidensia.py [--limit N] [--step-km 800]
"""
import argparse
import csv
import json
import math
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW_CACHE = ROOT / "data" / "ivc-search-raw-cache.json"
CBC_SOURCE = ROOT / "data" / "source" / "cbc-corporate-clinics-2025-01.csv"
OUT_CLINICS = ROOT / "data" / "ivc-evidensia-clinics-world.json"
PHC_STATUS_SIGNAL = "National PHC (in EDD)"

SEARCH_PAGE_URL = "https://external-referral-ui.azurewebsites.net/en/search"
DATA_URL_TMPL = "https://external-referral-ui.azurewebsites.net/_next/data/{build_id}/en/search.json"
RANGE_MILES = 621
ANIMAL_TYPES = ["1"]  # small animal / companion only — see module docstring
# Rough (lat_min, lat_max, lon_min, lon_max) rectangles covering inhabited
# landmasses, used to skip grid points that fall in open ocean or Antarctica.
CONTINENT_BBOXES = [
    (7, 72, -168, -52),      # North America
    (-56, 13, -82, -34),     # South America
    (34, 71, -25, 45),       # Europe
    (-35, 38, -18, 52),      # Africa
    (-11, 78, 26, 180),      # Asia
    (50, 72, -180, -168),    # far-eastern Russia (crosses the antimeridian)
    (-48, 0, 110, 180),      # Australia / Oceania
]
HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-CA,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://external-referral-ui.azurewebsites.net/en/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "x-nextjs-data": "1",
}
DELAY_S = 0.3
SAVE_EVERY = 25


def get_build_id() -> str:
    """The Next.js data URL is versioned by build id, which rotates on deploys."""
    req = urllib.request.Request(SEARCH_PAGE_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "ignore")
    m = re.search(r'"buildId":"([^"]+)"', html)
    if not m:
        raise RuntimeError("couldn't find buildId in search page HTML — site markup may have changed")
    return m.group(1)


def in_land_bbox(lat: float, lon: float) -> bool:
    return any(
        lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
        for lat_min, lat_max, lon_min, lon_max in CONTINENT_BBOXES
    )


def build_grid(step_km: float, lat_min: float = -56.0, lat_max: float = 78.0):
    """Grid of (lat, lon) points spaced so 621-mile search circles cover land.

    Row spacing is constant in km; column spacing widens near the poles (by
    1/cos(lat)) to keep it roughly constant in km too, so we don't over-sample
    near the equator or under-sample near the poles. Points outside
    CONTINENT_BBOXES (open ocean, Antarctica) are dropped.
    """
    points = []
    lat = lat_min
    lat_step_deg = step_km / 111.32
    while lat <= lat_max:
        lon_step_deg = min(step_km / (111.32 * max(math.cos(math.radians(lat)), 0.01)), 360.0)
        lon = -180.0
        while lon < 180.0:
            if in_land_bbox(lat, lon):
                points.append((round(lat, 4), round(lon, 4)))
            lon += lon_step_deg
        lat += lat_step_deg
    return points


def fetch_point(build_id: str, lat: float, lon: float, animal_type: str):
    params = {
        "animalType": animal_type,
        "range": RANGE_MILES,
        "latitude": lat,
        "longitude": lon,
    }
    url = DATA_URL_TMPL.format(build_id=build_id) + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
            data = json.loads(body)
            return data["pageProps"]["searchResult"]["clinics"]
        except json.JSONDecodeError:
            print(f"  non-JSON response for {lat},{lon},{animal_type}, attempt {attempt + 1}")
        except Exception as e:
            print(f"  error for {lat},{lon},{animal_type}: {e}, attempt {attempt + 1}")
        time.sleep(2 ** attempt * 2)
    return None


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def load_cbc_vetstrategy_names() -> set:
    """Normalized names of clinics the CBC Jan-2025 research attributes to VetStrategy
    (IVC Evidensia's Canadian brand) — used as a Canada-specific ownership signal
    since the referral-guide API's own flags are unreliable there (see docstring)."""
    names = set()
    if not CBC_SOURCE.exists():
        return names
    with CBC_SOURCE.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            name, owner = row[0], row[1]
            if "vet strategy" in owner.lower() or "vetstrategy" in owner.lower():
                for part in re.split(r"\|", name):
                    names.add(normalize_name(part))
    return names


def ownership_signals(clinic: dict, cbc_vetstrategy_names: set) -> list:
    """Which signal(s) indicate this clinic is IVC Evidensia-owned, if any."""
    signals = []
    if clinic.get("ivcClinic"):
        signals.append("ivcClinic flag")
    if clinic.get("phcStatus") == PHC_STATUS_SIGNAL:
        signals.append("phcStatus")
    if clinic.get("country") == "Canada" and normalize_name(clinic.get("name")) in cbc_vetstrategy_names:
        signals.append("CBC VetStrategy name match")
    return signals


def extract(clinic: dict, signals: list) -> dict:
    coords = (clinic.get("location") or {}).get("coordinates") or {}
    return {
        "site_id": clinic.get("siteId"),
        "name": clinic.get("name"),
        "address_line1": clinic.get("addressLine1"),
        "address_line2": clinic.get("addressLine2"),
        "city": clinic.get("city"),
        "post_code": clinic.get("postCode"),
        "country": clinic.get("country"),
        "latitude": coords.get("latitude"),
        "longitude": coords.get("longitude"),
        "phone": clinic.get("phone"),
        "webpage": clinic.get("webpage"),
        "referral_clinic": clinic.get("referralClinic"),
        "clinic_types": [t.get("name") for t in clinic.get("clinicTypes") or []],
        "animal_types": clinic.get("animalTypes"),
        "phc_status": clinic.get("phcStatus"),
        "google_rate": clinic.get("googleRate"),
        "google_places_id": clinic.get("googlePlacesId"),
        "ownership_signals": signals,
        "source": "IVC Evidensia referral guide",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="fetch at most N uncached grid queries")
    ap.add_argument("--step-km", type=float, default=800.0, help="grid spacing in km (default 800, radius is 999km/621mi)")
    ap.add_argument("--animal-types", default=",".join(ANIMAL_TYPES),
                     help="comma-separated animalType ids to query (default: 1, small animal only)")
    args = ap.parse_args()
    animal_types = args.animal_types.split(",")

    build_id = get_build_id()
    print(f"build id: {build_id}")

    grid = build_grid(args.step_km)
    queries = [(lat, lon, at) for (lat, lon) in grid for at in animal_types]
    cache = json.loads(RAW_CACHE.read_text()) if RAW_CACHE.exists() else {}
    todo = [q for q in queries if f"{q[0]},{q[1]},{q[2]}" not in cache]
    if args.limit is not None:
        todo = todo[: args.limit]
    print(f"{len(grid)} grid points x {len(animal_types)} animal types = {len(queries)} queries, "
          f"{len(cache)} cached, fetching {len(todo)}")

    failures = 0
    for n, (lat, lon, at) in enumerate(todo, 1):
        clinics = fetch_point(build_id, lat, lon, at)
        key = f"{lat},{lon},{at}"
        if clinics is None:
            failures += 1
            if failures >= 5:
                print("Too many consecutive failures — likely blocked. Saving progress and stopping.")
                break
            continue
        failures = 0
        cache[key] = clinics
        if n % SAVE_EVERY == 0:
            RAW_CACHE.write_text(json.dumps(cache))
            print(f"  {n}/{len(todo)} fetched ({len(cache)}/{len(queries)} total cached)")
        time.sleep(DELAY_S)
    RAW_CACHE.write_text(json.dumps(cache))

    cbc_vetstrategy_names = load_cbc_vetstrategy_names()
    by_site_id = {}
    for clinics in cache.values():
        for c in clinics:
            signals = ownership_signals(c, cbc_vetstrategy_names)
            if not signals:
                continue
            by_site_id[c["siteId"]] = extract(c, signals)
    clinics_out = sorted(by_site_id.values(), key=lambda c: (c["country"] or "", c["name"] or ""))
    OUT_CLINICS.write_text(json.dumps(clinics_out, indent=1, ensure_ascii=False))

    by_country = {}
    for c in clinics_out:
        by_country[c["country"]] = by_country.get(c["country"], 0) + 1
    print(f"\n{len(clinics_out)} IVC Evidensia clinics -> {OUT_CLINICS}")
    for country, n in sorted(by_country.items(), key=lambda kv: -kv[1]):
        print(f"  {country}: {n}")
    if len(cache) < len(queries):
        print(f"\nNOTE: {len(queries) - len(cache)} grid queries still uncached — rerun to resume.")
        sys.exit(1)


if __name__ == "__main__":
    main()
