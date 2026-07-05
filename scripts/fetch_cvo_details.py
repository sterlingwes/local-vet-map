#!/usr/bin/env python3
"""Fetch per-practice details from the CVO public register API.

Input:  data/cvo-ontario-vet-practices.json  (organization/search response, all
        Active practices — see README for the curl)
Cache:  data/cvo-details-raw.json            (org id -> raw API response; the
        script is resumable and only fetches ids not yet cached)
Output: data/cvo-practices.json              (extracted, per practice)
        data/cvo-director-correlation.json   (registrants directing 2+ practices)

Run: python3 scripts/fetch_cvo_details.py [--limit N]
"""
import argparse
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIST_IN = ROOT / "data" / "cvo-ontario-vet-practices.json"
RAW_CACHE = ROOT / "data" / "cvo-details-raw.json"
OUT_PRACTICES = ROOT / "data" / "cvo-practices.json"
OUT_DIRECTORS = ROOT / "data" / "cvo-director-correlation.json"

DETAIL_URL = "https://cvo.ca.thentiacloud.net/rest/public/organization/get/?id="
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-CA,en;q=0.9",
    "referer": "https://cvo.ca.thentiacloud.net/webs/cvo/register/",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
}
DELAY_S = 0.35
SAVE_EVERY = 50


def fetch_detail(org_id: str):
    req = urllib.request.Request(DETAIL_URL + urllib.parse.quote(org_id), headers=HEADERS)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
            return json.loads(body)
        except json.JSONDecodeError:
            # Non-JSON means the WAF challenge page — backing off won't fix a block,
            # but give it a couple of chances in case it's transient.
            print(f"  non-JSON response for {org_id} (WAF?), attempt {attempt + 1}")
        except Exception as e:
            print(f"  error for {org_id}: {e}, attempt {attempt + 1}")
        time.sleep(2 ** attempt * 2)
    return None


def extract(raw: dict) -> dict:
    facility = [
        {"category": f.get("category"), "status": f.get("status"), "expiry": f.get("expiryDate")}
        for f in raw.get("facilityDetails") or []
    ]
    active_cats = sorted({f["category"] for f in facility if f["category"] and f["status"] == "Active"})
    all_cats = sorted({f["category"] for f in facility if f["category"]})
    pa = raw.get("professionalActivity") or {}
    people = []
    for e in raw.get("employmentDetails") or []:
        reg = e.get("registrant") or {}
        if not reg.get("id") and not reg.get("name"):
            continue
        people.append({
            "registrant_id": reg.get("id"),
            "name": reg.get("name"),
            "position": e.get("position"),
            "primary": e.get("primary"),
            "active": e.get("active"),
            "start_date": e.get("startDate"),
            "registration_status": (reg.get("registrationStatus") or {}).get("name"),
        })
    history = raw.get("historyDetails") or []
    text = " ".join(filter(None, active_cats + all_cats + [pa.get("patientGroups") or ""])).lower()
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "telephone": raw.get("telephone"),
        "email": raw.get("emailAddress"),
        "street1": raw.get("street1"),
        "street2": raw.get("street2"),
        "city": raw.get("city"),
        "province": raw.get("province"),
        "postal_code": raw.get("postalCode"),
        "organization_type": raw.get("organizationType"),
        "categories_active": active_cats,
        "categories_all": all_cats,
        "patient_groups": pa.get("patientGroups"),
        "patient_types": pa.get("patientTypes"),
        "companion": "companion" in text,
        "established": history[0].get("startDate") if history else None,
        "people": people,
        "source": "CVO public register",
    }


def build_director_correlation(practices: list) -> list:
    by_reg = {}
    for p in practices:
        for person in p["people"]:
            rid = person.get("registrant_id")
            if not rid:
                continue
            entry = by_reg.setdefault(rid, {"registrant_id": rid, "name": person.get("name"), "practices": []})
            entry["practices"].append({
                "id": p["id"], "name": p["name"], "city": p["city"],
                "position": person.get("position"), "active": person.get("active"),
                "companion": p["companion"],
            })
    multi = [e for e in by_reg.values() if len({pr["id"] for pr in e["practices"]}) > 1]
    multi.sort(key=lambda e: -len(e["practices"]))
    return multi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="fetch at most N uncached records")
    args = ap.parse_args()

    org_ids = [r["id"] for r in json.loads(LIST_IN.read_text())["result"]]
    cache = json.loads(RAW_CACHE.read_text()) if RAW_CACHE.exists() else {}
    todo = [i for i in org_ids if i not in cache]
    if args.limit is not None:
        todo = todo[: args.limit]
    print(f"{len(org_ids)} practices, {len(cache)} cached, fetching {len(todo)}")

    failures = 0
    for n, org_id in enumerate(todo, 1):
        raw = fetch_detail(org_id)
        if raw is None:
            failures += 1
            if failures >= 5:
                print("Too many consecutive failures — likely blocked. Saving progress and stopping.")
                break
            continue
        failures = 0
        cache[org_id] = raw
        if n % SAVE_EVERY == 0:
            RAW_CACHE.write_text(json.dumps(cache))
            print(f"  {n}/{len(todo)} fetched ({len(cache)} total cached)")
        time.sleep(DELAY_S)
    RAW_CACHE.write_text(json.dumps(cache))

    practices = [extract(cache[i]) for i in org_ids if i in cache]
    OUT_PRACTICES.write_text(json.dumps(practices, indent=1, ensure_ascii=False))
    directors = build_director_correlation(practices)
    OUT_DIRECTORS.write_text(json.dumps(directors, indent=1, ensure_ascii=False))

    n_comp = sum(1 for p in practices if p["companion"])
    print(f"\nExtracted {len(practices)} practices -> {OUT_PRACTICES}")
    print(f"  companion-animal: {n_comp}, other (large animal/poultry/etc.): {len(practices) - n_comp}")
    print(f"  registrants tied to 2+ practices: {len(directors)} -> {OUT_DIRECTORS}")
    if len(cache) < len(org_ids):
        print(f"  NOTE: {len(org_ids) - len(cache)} still uncached — rerun to resume.")
        sys.exit(1)


if __name__ == "__main__":
    main()
