#!/usr/bin/env python3
"""Scrape business websites from Google Maps using the results feed only.

The slow way is to open every business's own Maps page to read its website.
Measured on "pest control in Manchester": that yields ~20 businesses in two
minutes. But the website is already on the result card as a "Visit ..." link,
along with name, rating, category, address and phone - so the place-page visit
buys nothing we need. Reading the feed instead: 121 businesses in 30 seconds,
93% of them with a website.

Fewer page loads is also far less conspicuous than hammering hundreds of place
pages, so the same politeness budget covers many more businesses.

    python scrape_maps_fast.py --category "pest control" \
        --locations-file uk_cities.txt --output uk_pest_raw.csv

Resumable: locations already in --output are skipped, so stop and restart
freely. If Google starts refusing (CAPTCHA), it stops rather than grinding on.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import random
import re
import sys
import time
from urllib.parse import quote_plus, urlsplit

from playwright.async_api import async_playwright

FIELDS = ["name", "website", "category", "rating", "reviews", "address",
          "phone", "hours", "location", "query", "card_text"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

PHONE_RE = re.compile(r"(\+?\d[\d\s()‑-]{7,}\d)")

# Google salts card text with icon glyphs from the Unicode Private Use Area
# (U+E000-U+F8FF). They carry no meaning and wreck naive text parsing.
def _clean(text: str) -> str:
    return "".join(ch for ch in text if not ("\ue000" <= ch <= "\uf8ff")).strip()
RATING_RE = re.compile(r"^(\d[.,]\d)\s*(?:\(([\d,]+)\))?$")
HOURS_RE = re.compile(r"^(open|clos|temporarily|permanently|24\s*hours|opens|reopens)", re.I)
# Card chrome, not business data
NOISE_LINES = {"no reviews", "no rating", "new", "sponsored",
               "website", "directions", "online estimates", "on-site services",
               "online appointments", "in-store shopping", "in-store pick-up",
               "delivery", "wheelchair accessible entrance", "identifies as women-owned",
               "call", "share", "save", "book online", "order online"}

# Pulls every card out of the feed in one evaluate - one round trip per scroll
# rather than one per element, which matters when a city has 120 of them.
EXTRACT_JS = """() => {
  const feed = document.querySelector('div[role="feed"]');
  if (!feed) return [];
  const cards = [...feed.querySelectorAll('div[jsaction]')].filter(
      c => c.querySelector('a[href*="/maps/place/"]'));
  return cards.map(c => {
    const place = c.querySelector('a[href*="/maps/place/"]');
    // the website is a plain anchor labelled "Visit <business>" - the only
    // external link on the card
    const site = [...c.querySelectorAll('a')].find(
        a => (a.getAttribute('aria-label') || '').startsWith('Visit '));
    return {
      name: place ? (place.getAttribute('aria-label') || '') : '',
      website: site ? site.href : '',
      maps_url: place ? place.href : '',
      lines: c.innerText.split('\\n').map(s => s.trim()).filter(Boolean),
    };
  }).filter(r => r.name);
}"""


def parse_card(raw: dict, location: str, query: str) -> dict:
    """Best-effort structure from the card's text.

    Only `website` and `name` come from real selectors; everything else is read
    off the rendered text, so the raw lines are kept in `card_text`. If Google
    reshuffles the card layout the extra columns degrade, the URLs do not.

    The category is on its own line with no separator - an earlier version only
    looked at lines containing "·" and so filed 533 businesses under "Open 24
    hours", which then read as the wrong trade and got them discarded.
    """
    lines = [c for c in (_clean(l) for l in raw["lines"]) if c]
    name = raw["name"].strip()

    body, seen = [], {name.lower()}
    for l in lines:                      # the name is rendered twice per card
        if l.lower() in seen:
            continue
        seen.add(l.lower()); body.append(l)

    rating = reviews = hours = phone = ""
    leftover = []
    for l in body:
        m = RATING_RE.match(l)
        if m and not rating:
            rating = m.group(1).replace(",", ".")
            if m.group(2):
                reviews = m.group(2).replace(",", "")
            continue
        if HOURS_RE.match(l):
            hours = hours or l.split("·")[0].strip()
            if not phone:
                pm = PHONE_RE.search(l)
                if pm:
                    phone = pm.group(1).strip()
            continue
        if l.lower() in NOISE_LINES:
            continue
        pm = PHONE_RE.search(l)
        if pm and len(re.sub(r"\D", "", l)) >= 9 and len(l) < 24:
            phone = phone or pm.group(1).strip()
            continue
        leftover.append(l)

    category = address = ""
    if leftover:
        parts = [x.strip() for x in leftover[0].split("·") if x.strip()]
        category = parts[0] if parts else ""
        if len(parts) > 1:
            address = parts[-1]
    if not address and len(leftover) > 1:
        parts = [x.strip() for x in leftover[1].split("·") if x.strip()]
        address = parts[-1] if parts else ""

    return {"name": name, "website": raw["website"].strip(), "category": category,
            "rating": rating, "reviews": reviews, "address": address, "phone": phone,
            "hours": hours, "location": location, "query": query,
            "card_text": " | ".join(lines)}


def host_of(url: str) -> str:
    h = urlsplit(url).netloc.split("@")[-1].split(":")[0].lower()
    return h[4:] if h.startswith("www.") else h


def load_existing(path):
    if not os.path.exists(path):
        return [], set(), set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return (rows,
            {r.get("location", "") for r in rows},
            {host_of(r.get("website", "")) for r in rows if r.get("website")})


def save(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


async def scrape_location(page, category, location, per_location):
    query = f"{category} in {location}"
    await page.goto("https://www.google.com/maps/search/" + quote_plus(query),
                    wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_selector('div[role="feed"]', timeout=25000)
    except Exception:
        return None                      # no feed: consent wall, CAPTCHA, or no results

    seen_names, prev, stagnant = set(), 0, 0
    for _ in range(60):
        cards = await page.evaluate(EXTRACT_JS)
        seen_names = {c["name"] for c in cards}
        if len(seen_names) >= per_location:
            break
        if len(seen_names) == prev:
            stagnant += 1
            if stagnant >= 3:
                break                    # feed exhausted
        else:
            stagnant = 0
        prev = len(seen_names)
        await page.evaluate("""() => { const f = document.querySelector('div[role="feed"]');
                                       if (f) f.scrollTop = f.scrollHeight; }""")
        await page.wait_for_timeout(random.randint(1100, 1900))

    return [parse_card(c, location, query)
            for c in await page.evaluate(EXTRACT_JS)][:per_location]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", required=True, help='e.g. "pest control"')
    ap.add_argument("--output", required=True)
    ap.add_argument("--locations-file", help="one location per line, e.g. 'Manchester, UK'")
    ap.add_argument("--locations",
                    help="semicolon-separated, as an alternative to the file. "
                         "Semicolons, not commas, because a location is usually "
                         "written 'Manchester, UK' and splitting on commas turns "
                         "that into two useless searches.")
    ap.add_argument("--per-location", type=int, default=200,
                    help="Maps itself caps a search around 120; the default just "
                         "means 'take everything the feed will give'")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--websites-only", action="store_true",
                    help="skip businesses Maps has no website for - they cannot be "
                         "approached through a contact form")
    args = ap.parse_args()

    if args.locations_file:
        locations = [l.strip() for l in open(args.locations_file, encoding="utf-8") if l.strip()]
    elif args.locations:
        locations = [l.strip() for l in args.locations.split(";") if l.strip()]
    else:
        print("give --locations-file or --locations", file=sys.stderr); return 2

    rows, done_locations, seen_hosts = load_existing(args.output)
    if done_locations:
        print(f"[resume] {len(rows)} rows already in {args.output}, "
              f"{len(done_locations)} location(s) done")

    empty_streak = 0
    t0 = time.time()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headful)
        ctx = await browser.new_context(user_agent=UA, locale="en-GB",
                                        viewport={"width": 1400, "height": 950})
        page = await ctx.new_page()

        for i, loc in enumerate(locations, 1):
            if loc in done_locations:
                print(f"[{i}/{len(locations)}] {loc} - already done, skipping"); continue

            found = await scrape_location(page, args.category, loc, args.per_location)
            if found is None:
                empty_streak += 1
                print(f"[{i}/{len(locations)}] {loc} - NO FEED "
                      f"(consent wall or CAPTCHA) [{empty_streak}]")
                # Grinding through the rest while blocked produces nothing and
                # makes the block worse. Stop and let the operator wait it out.
                if empty_streak >= 3:
                    print("\nthree locations in a row returned nothing - Google is "
                          "almost certainly blocking. Stopping; rerun later and it "
                          "resumes from here.")
                    break
                await asyncio.sleep(random.uniform(20, 40))
                continue
            empty_streak = 0

            new = 0
            for r in found:
                if args.websites_only and not r["website"]:
                    continue
                h = host_of(r["website"]) if r["website"] else ""
                if h and h in seen_hosts:
                    continue             # same company, another branch
                if h:
                    seen_hosts.add(h)
                rows.append(r); new += 1

            done_locations.add(loc)
            save(args.output, rows)
            with_site = sum(1 for r in found if r["website"])
            print(f"[{i}/{len(locations)}] {loc} - {len(found)} found, "
                  f"{with_site} with website, {new} new -> {len(rows)} total")
            await asyncio.sleep(random.uniform(6, 14))

        await browser.close()

    save(args.output, rows)
    mins = (time.time() - t0) / 60
    print(f"\n{len(rows)} unique businesses in {args.output} ({mins:.1f} min)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
