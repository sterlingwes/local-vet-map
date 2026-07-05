#!/usr/bin/env python3
"""Build the merged Ontario clinic dataset.

Inputs:
  data/source/cbc-corporate-clinics-2025-01.csv  (CBC research, Jan 2025)
  data/osm-vets-on.json                          (Overpass output, see fetch_osm.py)

Output:
  docs/data/clinics.geojson

Every feature gets:
  name, city, ownership (corporate|independent), owner_group, parent,
  status (verified|assumed), source, precision (exact|geocoded|city), osm_id
"""
import csv
import json
import math
import pathlib
import re
import time
import unicodedata
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSV_IN = ROOT / "data" / "source" / "cbc-corporate-clinics-2025-01.csv"
OSM_IN = ROOT / "data" / "osm-vets-on.json"
GEOCODE_CACHE = ROOT / "data" / "geocode-cache.json"
OUT = ROOT / "docs" / "data" / "clinics.geojson"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "local-vet-map/0.1 (ownership research; github.com/sterlingwes/local-vet-map)"

CBC_SOURCE = "CBC News research (Jan 2025)"
OSM_SOURCE = "OpenStreetMap"

# Ultimate owners of the consolidator groups in the CBC data (as of Jan 2025)
PARENTS = {
    "NVA": "National Veterinary Associates — JAB Holding (private equity)",
    "Vet Strategy": "VetStrategy — IVC Evidensia (EQT / Silver Lake private equity, Nestlé minority)",
    "VCA": "VCA Canada — Mars Inc.",
}
GROUP_LABEL = {"NVA": "NVA", "Vet Strategy": "VetStrategy", "VCA": "VCA"}

# Cities that appear in the CBC sheet without a province and are in Ontario
ONTARIO_BARE_CITIES = {"toronto", "ottawa", "hamilton"}

MATCH_RADIUS_KM = 50  # OSM name match must be within this distance of the stated city
DEDUPE_RADIUS_KM = 1.0


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)          # drop parentheticals e.g. (PetFocus)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def base_name(s: str) -> str:
    """Name with any ' – Branch' suffix removed."""
    return norm_name(re.split(r"[–—-]", s, maxsplit=1)[0]) if re.search(r"[–—]", s) else norm_name(s)


GENERIC_TOKENS = {"animal", "hospital", "veterinary", "vet", "clinic", "pet",
                  "services", "service", "centre", "center", "the", "of", "and",
                  "cat", "dog", "hopital", "veterinaire", "24", "hour", "emergency"}


def token_sim(a: str, b: str) -> float:
    """Jaccard similarity of token sets, weighting distinctive tokens double."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    w = lambda toks: sum(1 if t in GENERIC_TOKENS else 2 for t in toks)
    return w(inter) / w(union)


def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


class Geocoder:
    def __init__(self):
        self.cache = json.loads(GEOCODE_CACHE.read_text()) if GEOCODE_CACHE.exists() else {}
        self.last_call = 0.0

    def lookup(self, query: str):
        if query in self.cache:
            return self.cache[query]
        wait = 1.1 - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        url = NOMINATIM + "?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "limit": 1, "countrycodes": "ca"})
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                results = json.load(resp)
        except Exception as e:
            print(f"  geocode error for {query!r}: {e}")
            results = []
        self.last_call = time.monotonic()
        hit = [float(results[0]["lat"]), float(results[0]["lon"])] if results else None
        self.cache[query] = hit
        return hit

    def save(self):
        GEOCODE_CACHE.write_text(json.dumps(self.cache, indent=1, ensure_ascii=False))


def load_corporate():
    rows = []
    with CSV_IN.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            city = row["City/Region"].strip()
            is_on = city.endswith(", Ont.") or city.lower() in ONTARIO_BARE_CITIES
            if not is_on:
                continue
            rows.append({
                "name": row["Name"].strip().replace("’", "'"),
                "company": row["Company"].strip(),
                "city": re.sub(r", Ont\.$", "", city),
            })
    return rows


def load_osm():
    payload = json.loads(OSM_IN.read_text())
    feats = []
    for el in payload["elements"]:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        if el["type"] == "node":
            lat, lon = el["lat"], el["lon"]
        else:
            c = el.get("center")
            if not c:
                continue
            lat, lon = c["lat"], c["lon"]
        feats.append({
            "name": name.strip(),
            "lat": lat,
            "lon": lon,
            "osm_id": f"{el['type']}/{el['id']}",
            "city": tags.get("addr:city", ""),
            "brand": tags.get("brand", ""),
            "operator": tags.get("operator", ""),
        })
    # Dedupe: same normalized name within DEDUPE_RADIUS_KM
    deduped = []
    for f in feats:
        dup = next((d for d in deduped
                    if norm_name(d["name"]) == norm_name(f["name"])
                    and haversine_km((d["lat"], d["lon"]), (f["lat"], f["lon"])) < DEDUPE_RADIUS_KM), None)
        if not dup:
            deduped.append(f)
    return deduped


def main():
    corporate = load_corporate()
    osm = load_osm()
    geo = Geocoder()
    print(f"{len(corporate)} Ontario corporate clinics (CBC), {len(osm)} named OSM vets after dedupe")

    # City centroids for disambiguating name matches
    city_pt = {}
    for c in sorted({r["city"] for r in corporate}):
        city_pt[c] = geo.lookup(f"{c}, Ontario, Canada")
    geo.save()

    osm_by_name = {}
    for f in osm:
        osm_by_name.setdefault(norm_name(f["name"]), []).append(f)

    features = []
    claimed_osm = set()
    stats = {"osm_match": 0, "geocoded": 0, "city_centroid": 0, "dropped": 0}

    for row in corporate:
        cpt = city_pt.get(row["city"])
        cand = osm_by_name.get(norm_name(row["name"]), [])
        if not cand:
            # try matching CBC branch-suffixed names against OSM base names
            cand = osm_by_name.get(base_name(row["name"]), [])
        match = None
        for f in cand:
            if f["osm_id"] in claimed_osm:
                continue
            if cpt is None or haversine_km((f["lat"], f["lon"]), cpt) <= MATCH_RADIUS_KM:
                match = f
                break
        if match is None and cpt is not None:
            # fuzzy fallback: best token-set match near the stated city
            best, best_sim = None, 0.0
            target = norm_name(row["name"])
            for f in osm:
                if f["osm_id"] in claimed_osm:
                    continue
                if haversine_km((f["lat"], f["lon"]), cpt) > MATCH_RADIUS_KM:
                    continue
                sim = token_sim(target, norm_name(f["name"]))
                if sim > best_sim:
                    best, best_sim = f, sim
            if best_sim >= 0.65:
                match = best

        if match:
            claimed_osm.add(match["osm_id"])
            lat, lon, precision, osm_id = match["lat"], match["lon"], "exact", match["osm_id"]
            stats["osm_match"] += 1
        else:
            pt = geo.lookup(f"{row['name']}, {row['city']}, Ontario, Canada")
            if pt and cpt and haversine_km(pt, cpt) > MATCH_RADIUS_KM:
                pt = None  # geocoder wandered off; distrust it
            if pt:
                lat, lon, precision, osm_id = pt[0], pt[1], "geocoded", None
                stats["geocoded"] += 1
            elif cpt:
                lat, lon, precision, osm_id = cpt[0], cpt[1], "city", None
                stats["city_centroid"] += 1
            else:
                print(f"  DROPPED (no location): {row['name']} — {row['city']}")
                stats["dropped"] += 1
                continue

        features.append(feature(
            lat, lon,
            name=row["name"], city=row["city"], ownership="corporate",
            owner_group=GROUP_LABEL[row["company"]], parent=PARENTS[row["company"]],
            status="verified", source=CBC_SOURCE, precision=precision, osm_id=osm_id,
        ))

    geo.save()

    for f in osm:
        if f["osm_id"] in claimed_osm:
            continue
        features.append(feature(
            f["lat"], f["lon"],
            name=f["name"], city=f["city"], ownership="independent",
            owner_group=None, parent=None,
            status="assumed", source=OSM_SOURCE, precision="exact", osm_id=f["osm_id"],
        ))

    # Spread features that share the exact same point (city centroids) into a
    # small ring (~300 m) so every marker stays clickable. precision stays "city".
    by_pt = {}
    for f in features:
        by_pt.setdefault(tuple(f["geometry"]["coordinates"]), []).append(f)
    for pt, group in by_pt.items():
        if len(group) < 2:
            continue
        for i, f in enumerate(group):
            ang = 2 * math.pi * i / len(group)
            lat = pt[1] + 0.003 * math.sin(ang)
            lon = pt[0] + 0.004 * math.cos(ang)
            f["geometry"]["coordinates"] = [round(lon, 6), round(lat, 6)]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"type": "FeatureCollection", "features": features}, ensure_ascii=False))
    n_corp = sum(1 for f in features if f["properties"]["ownership"] == "corporate")
    print(f"Wrote {len(features)} clinics ({n_corp} corporate, {len(features) - n_corp} independent) -> {OUT}")
    print(f"Corporate location quality: {stats}")


def feature(lat, lon, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
        "properties": props,
    }


if __name__ == "__main__":
    main()
