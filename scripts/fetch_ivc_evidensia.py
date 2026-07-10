#!/usr/bin/env python3
"""Fetch every IVC Evidensia-owned clinic worldwide from their referral-guide tool.

https://ivcevidensia.com/referral-guide embeds a Next.js app
(external-referral-ui.azurewebsites.net) whose search API takes a lat/lon and
returns every clinic (IVC-owned or not — it's a shared referral network) within
`range` miles, capped at 621 (the tool's own max radius). There's no "list all"
endpoint, so this script walks a grid of points spaced so their 621-mile search
circles cover the whole world, and keeps only clinics flagged `ivcClinic: true`.

The site also filters by animalType (1=small animal, 2=equine, 3=farm, plus
4-6 seen but rare); a clinic can appear under one type and not another (e.g.
equine-only referral centres), so each grid point is queried once per type.

Cache:  data/ivc-search-raw-cache.json   ("lat,lon,animalType" -> raw clinics
        list; gitignored, resumable — rerun to continue after an interruption)
Output: data/ivc-evidensia-clinics-world.json  (deduped, ivcClinic-only)

Run: python3 scripts/fetch_ivc_evidensia.py [--limit N] [--step-km 800]
"""
import argparse
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
OUT_CLINICS = ROOT / "data" / "ivc-evidensia-clinics-world.json"

SEARCH_PAGE_URL = "https://external-referral-ui.azurewebsites.net/en/search"
DATA_URL_TMPL = "https://external-referral-ui.azurewebsites.net/_next/data/{build_id}/en/search.json"
RANGE_MILES = 621
ANIMAL_TYPES = ["1", "2", "3", "4", "5", "6"]
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


def build_grid(step_km: float, lat_min: float = -75.0, lat_max: float = 78.0):
    """Grid of (lat, lon) points spaced so 621-mile search circles cover the globe.

    Row spacing is constant in km; column spacing widens near the poles (by
    1/cos(lat)) to keep it roughly constant in km too, so we don't over-sample
    near the equator or under-sample near the poles.
    """
    points = []
    lat = lat_min
    lat_step_deg = step_km / 111.32
    while lat <= lat_max:
        lon_step_deg = min(step_km / (111.32 * max(math.cos(math.radians(lat)), 0.01)), 360.0)
        lon = -180.0
        while lon < 180.0:
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


def extract(clinic: dict) -> dict:
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
        "source": "IVC Evidensia referral guide",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="fetch at most N uncached grid queries")
    ap.add_argument("--step-km", type=float, default=800.0, help="grid spacing in km (default 800, radius is 999km/621mi)")
    args = ap.parse_args()

    build_id = get_build_id()
    print(f"build id: {build_id}")

    grid = build_grid(args.step_km)
    queries = [(lat, lon, at) for (lat, lon) in grid for at in ANIMAL_TYPES]
    cache = json.loads(RAW_CACHE.read_text()) if RAW_CACHE.exists() else {}
    todo = [q for q in queries if f"{q[0]},{q[1]},{q[2]}" not in cache]
    if args.limit is not None:
        todo = todo[: args.limit]
    print(f"{len(grid)} grid points x {len(ANIMAL_TYPES)} animal types = {len(queries)} queries, "
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

    by_site_id = {}
    for clinics in cache.values():
        for c in clinics:
            if not c.get("ivcClinic"):
                continue
            by_site_id[c["siteId"]] = extract(c)
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
