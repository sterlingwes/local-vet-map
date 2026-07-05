#!/usr/bin/env python3
"""Build the Ontario clinic dataset for the map.

Universe: the CVO public register (every accredited practice, with addresses)
filtered to companion-animal-relevant practices. Ownership is layered on top:

  corporate     - matched to the CBC consolidator list (NVA/VetStrategy/VCA),
                  or registered under the consolidator's own brand (VCA prefix)
  institutional - SPCA / humane societies / municipal / university clinics
  group         - same person directs-or-owns 3+ practices (local multi-location
                  operations, e.g. Juno Veterinary)
  independent   - everything else (assumed independent until verified)

Inputs:
  data/cvo-practices.json                       (see fetch_cvo_details.py)
  data/source/cbc-corporate-clinics-2025-01.csv (CBC research, Jan 2025)
  data/geocode-cache.json                       (Nominatim cache, grows as needed)

Output:
  docs/data/clinics.geojson
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
CVO_IN = ROOT / "data" / "cvo-practices.json"
CSV_IN = ROOT / "data" / "source" / "cbc-corporate-clinics-2025-01.csv"
GEOCODE_CACHE = ROOT / "data" / "geocode-cache.json"
OUT = ROOT / "docs" / "data" / "clinics.geojson"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "local-vet-map/0.2 (ownership research; github.com/sterlingwes/local-vet-map)"

PARENTS = {
    "NVA": "National Veterinary Associates — JAB Holding (private equity)",
    "VetStrategy": "VetStrategy — IVC Evidensia (EQT / Silver Lake private equity, Nestlé minority)",
    "VCA": "VCA Canada — Mars Inc.",
}
CBC_COMPANY = {"NVA": "NVA", "Vet Strategy": "VetStrategy", "VCA": "VCA"}

ONTARIO_BARE_CITIES = {"toronto", "ottawa", "hamilton"}
INSTITUTIONAL_RE = re.compile(
    r"\b(spca|humane society|animal services|ovc"
    r"|(?:university|college)\b(?!\s+(?:st\b|street|ave\b|avenue|rd\b|road|blvd|drive|dr\b)))",
    re.I)
GROUP_MIN_PRACTICES = 3
FUZZY_ACCEPT = 0.72

GENERIC_TOKENS = {"animal", "hospital", "veterinary", "vet", "clinic", "pet",
                  "services", "service", "centre", "center", "the", "of", "and",
                  "cat", "dog", "hopital", "veterinaire", "24", "hour", "emergency"}


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_city(s: str) -> str:
    return norm_name((s or "").split(",")[0])


def token_sim(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    w = lambda toks: sum(1 if t in GENERIC_TOKENS else 2 for t in toks)
    return w(ta & tb) / w(ta | tb)


class Geocoder:
    def __init__(self):
        self.cache = json.loads(GEOCODE_CACHE.read_text()) if GEOCODE_CACHE.exists() else {}
        self.last_call = 0.0
        self.n_new = 0

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
        self.n_new += 1
        if self.n_new % 100 == 0:
            self.save()
            print(f"  ...geocoded {self.n_new} new queries")
        return hit

    def save(self):
        GEOCODE_CACHE.write_text(json.dumps(self.cache, indent=1, ensure_ascii=False))


def load_cbc_ontario():
    """CBC rows for Ontario, indexed by normalized name."""
    by_name = {}
    with CSV_IN.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            city = row["City/Region"].strip()
            if not (city.endswith(", Ont.") or city.lower() in ONTARIO_BARE_CITIES):
                continue
            rec = {
                "name": row["Name"].strip(),
                "group": CBC_COMPANY[row["Company"].strip()],
                "city": norm_city(re.sub(r", Ont\.$", "", city)),
            }
            by_name.setdefault(norm_name(rec["name"]), []).append(rec)
    return by_name


def match_cbc(practice, cbc_by_name):
    """Return the CBC group for a CVO practice, or None."""
    name = practice["name"] or ""
    city = norm_city(practice["city"])
    for candidate_name in (name, re.sub(r"^VCA\s+", "", name, flags=re.I)):
        cands = cbc_by_name.get(norm_name(candidate_name), [])
        if len(cands) == 1:
            return cands[0]["group"]
        if cands:
            same_city = [c for c in cands if c["city"] == city]
            if same_city:
                return same_city[0]["group"]
    # fuzzy, same city only
    target = norm_name(name)
    best, best_sim = None, 0.0
    for cands in cbc_by_name.values():
        for c in cands:
            if c["city"] != city:
                continue
            sim = token_sim(target, norm_name(c["name"]))
            if sim > best_sim:
                best, best_sim = c, sim
    return best["group"] if best_sim >= FUZZY_ACCEPT else None


def classify(practices, cbc_by_name):
    """Attach ownership fields to each practice dict."""
    for p in practices:
        group = match_cbc(p, cbc_by_name)
        if group is None and re.match(r"^VCA\b", p["name"] or "", re.I):
            group = "VCA"
        if group:
            p["ownership"] = "corporate"
            p["owner_group"] = group
            p["parent"] = PARENTS[group]
            p["status"] = "verified"
        elif INSTITUTIONAL_RE.search(p["name"] or ""):
            p["ownership"] = "institutional"
            p["owner_group"] = None
            p["parent"] = None
            p["status"] = "verified"
        else:
            p["ownership"] = None  # decided by the group pass below

    # group pass: registrant -> not-yet-classified practices they direct/own
    by_reg = {}
    for p in practices:
        if p["ownership"] is not None:
            continue
        for person in p["people"]:
            if person.get("registrant_id") and person.get("active") is not False:
                by_reg.setdefault(person["registrant_id"], {"name": person["name"], "ids": set()})
                by_reg[person["registrant_id"]]["ids"].add(p["id"])
    group_of = {}
    for reg in by_reg.values():
        if len(reg["ids"]) >= GROUP_MIN_PRACTICES:
            for pid in reg["ids"]:
                # a practice keeps the largest group it belongs to
                if pid not in group_of or len(reg["ids"]) > group_of[pid][1]:
                    group_of[pid] = (reg["name"], len(reg["ids"]))
    for p in practices:
        if p["ownership"] is not None:
            continue
        if p["id"] in group_of:
            name, n = group_of[p["id"]]
            p["ownership"] = "group"
            p["owner_group"] = f"{name} ({n} locations)"
            p["parent"] = None
            p["status"] = "verified"
        else:
            p["ownership"] = "independent"
            p["owner_group"] = None
            p["parent"] = None
            p["status"] = "assumed"


def locate(p, geo):
    """Geocode a practice: address -> postal code -> city."""
    city = (p["city"] or "").split(",")[0].strip()
    if p["street1"]:
        street = re.sub(r"\s*(Unit|Suite|Ste\.?|#)\s*\S+$", "", p["street1"], flags=re.I)
        pt = geo.lookup(f"{street}, {city}, Ontario, Canada")
        if pt:
            return pt, "address"
    if p["postal_code"]:
        pt = geo.lookup(f"{p['postal_code']}, Ontario, Canada")
        if pt:
            return pt, "postal"
    if city:
        pt = geo.lookup(f"{city}, Ontario, Canada")
        if pt:
            return pt, "city"
    return None, None


def main():
    practices = json.loads(CVO_IN.read_text())
    keep = [p for p in practices
            if p["companion"] or any("specialty" in c.lower() for c in p["categories_all"])]
    print(f"{len(practices)} CVO practices, {len(keep)} companion/specialty")

    cbc_by_name = load_cbc_ontario()
    classify(keep, cbc_by_name)

    geo = Geocoder()
    features, stats = [], {}
    for p in keep:
        pt, precision = locate(p, geo)
        if pt is None:
            print(f"  DROPPED (no location): {p['name']} — {p['city']}")
            stats["dropped"] = stats.get("dropped", 0) + 1
            continue
        stats[precision] = stats.get(precision, 0) + 1
        addr = ", ".join(filter(None, [p["street1"], p["street2"], (p["city"] or "").split(",")[0],
                                       p["postal_code"]]))
        directors = "; ".join(f"{x['name']} ({x['position']})" for x in p["people"] if x.get("name"))
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(pt[1], 6), round(pt[0], 6)]},
            "properties": {
                "name": p["name"],
                "city": (p["city"] or "").split(",")[0],
                "address": addr,
                "ownership": p["ownership"],
                "owner_group": p["owner_group"],
                "parent": p["parent"],
                "status": p["status"],
                "source": "CVO public register" + (" + CBC News research (Jan 2025)"
                                                   if p["ownership"] == "corporate" else ""),
                "precision": precision,
                "established": p["established"],
                "directors": directors or None,
                "cvo_id": p["id"],
            },
        })
    geo.save()

    # spread stacked points (shared postal/city fallbacks) into a small ring
    by_pt = {}
    for f in features:
        by_pt.setdefault(tuple(f["geometry"]["coordinates"]), []).append(f)
    for pt, grp in by_pt.items():
        if len(grp) < 2:
            continue
        for i, f in enumerate(grp):
            ang = 2 * math.pi * i / len(grp)
            f["geometry"]["coordinates"] = [round(pt[0] + 0.004 * math.cos(ang), 6),
                                            round(pt[1] + 0.003 * math.sin(ang), 6)]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features},
                              ensure_ascii=False))
    counts = {}
    for f in features:
        p = f["properties"]
        key = p["owner_group"] if p["ownership"] == "corporate" else p["ownership"]
        counts[key] = counts.get(key, 0) + 1
    print(f"Wrote {len(features)} clinics -> {OUT}")
    print(f"Ownership: {counts}")
    print(f"Location precision: {stats}")


if __name__ == "__main__":
    main()
