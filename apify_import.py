#!/usr/bin/env python3
"""Pull central London places from Apify's Google Maps scraper into the
scrape_london_hotels.py files, so they go through the same website dedupe and
contact-form check.

Needs APIFY_TOKEN in .env.local. Only the base per-place charge applies - every
paid add-on (contact enrichment, full details, photos, reviews) stays off, and
--max-usd is passed to Apify as a hard spending cap.

    python apify_import.py --queries "spa;event venue" --per-search 38 --max-usd 4.5
    python apify_import.py --run-id <id>      # re-import a finished run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

import scrape_london_hotels as m

ACTOR = "vortex_data~google-maps"
API = "https://api.apify.com/v2"

AREAS = [
    "Mayfair", "Soho", "Marylebone", "Fitzrovia", "Bloomsbury", "Covent Garden",
    "Holborn", "Westminster", "Victoria", "Knightsbridge", "South Kensington",
    "Kensington", "Paddington", "King's Cross", "Camden Town", "Islington",
    "Clerkenwell", "Shoreditch", "City of London", "London Bridge", "Waterloo",
]


def token() -> str:
    for line in open(".env.local", encoding="utf-8"):
        if line.startswith("APIFY_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("APIFY_TOKEN missing from .env.local")


def call(path: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(API + path, method="POST" if data is not None else "GET",
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Authorization": f"Bearer {token()}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def import_items(items: list[dict]) -> None:
    known = {r["key"] for r in m.read_csv(m.PLACES)}
    places, sites = [], []
    for it in items:
        key = it.get("fid") or it.get("placeId") or it.get("title", "").lower()
        if not key or key in known or it.get("permanentlyClosed"):
            continue
        known.add(key)
        q = it.get("searchString", "")
        places.append({"key": key, "name": it.get("title", ""), "maps_url": it.get("url", ""),
                       "card_website": it.get("website", ""), "card_text": "apify",
                       "query": q.split(" in ")[0] if " in " in q else q, "area": "apify"})
        sites.append({"key": key, "name": it.get("title", ""), "website": it.get("website") or "",
                      "phone": it.get("phone", ""), "address": it.get("address", ""),
                      "category": it.get("categoryName", ""), "rating": it.get("totalScore", ""),
                      "reviews": it.get("reviewsCount", "")})
    m.append_csv(m.PLACES, places, m.PLACE_FIELDS)
    m.append_csv(m.SITES, sites, m.SITE_FIELDS)
    print(f"imported {len(places)} new places "
          f"({sum(1 for s in sites if s['website'])} with a website)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", help="semicolon-separated search terms")
    ap.add_argument("--per-search", type=int, default=38)
    ap.add_argument("--max-usd", type=float, required=False, default=4.5)
    ap.add_argument("--run-id", help="import an existing run instead of starting one")
    args = ap.parse_args()

    run_id = args.run_id
    if not run_id:
        queries = [q.strip() for q in args.queries.split(";") if q.strip()]
        body = {"searchStringsArray": queries,
                "locationQueries": [f"{a}, London, UK" for a in AREAS],
                "maxCrawledPlacesPerSearch": args.per_search,
                "skipClosedPlaces": True}
        run = call(f"/acts/{ACTOR}/runs?maxTotalChargeUsd={args.max_usd}", body)["data"]
        run_id = run["id"]
        print(f"started Apify run {run_id} ({len(queries)} terms x {len(AREAS)} areas, "
              f"<= {args.per_search} each, cap ${args.max_usd})", flush=True)

    while True:
        run = call(f"/actor-runs/{run_id}")["data"]
        if run["status"] not in ("READY", "RUNNING"):
            break
        print(f"  {run['status']}  ${run.get('usageTotalUsd') or 0:.2f}", flush=True)
        time.sleep(30)
    print(f"run {run['status']}, charged ${run.get('usageTotalUsd') or 0:.2f}")

    items, offset = [], 0
    while True:
        chunk = call(f"/datasets/{run['defaultDatasetId']}/items?clean=1&offset={offset}&limit=1000")
        chunk = chunk if isinstance(chunk, list) else chunk.get("data", [])
        items += chunk
        if len(chunk) < 1000:
            break
        offset += 1000
    print(f"{len(items)} places from Apify")
    import_items(items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
