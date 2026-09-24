#!/usr/bin/env python3
"""Website URLs of DTC brands and e-commerce companies in the Austin, Texas area - one file, built for volume.

SETUP (once)
    Mac:      python3 -m pip install playwright openpyxl
              python3 -m playwright install chromium
    Windows:  py -m pip install playwright openpyxl
              py -m playwright install chromium

RUN
    python3 scrape_austin_dtc.py              (Windows: py scrape_austin_dtc.py)

    Stop it with Ctrl+C whenever you like and run the same command again - it
    carries on from the search it had reached. Everything lands in the folder
    austin_dtc_output/ next to this file:

    austin_dtc.csv                 the list: one row per company, best first
    austin_dtc part 1 of N.xlsx    same list in 900-row pieces (Google Sheets
                                   refuses an import over 1,000 rows)
    austin_dtc_rejects.csv         everything dropped, with the reason - read it
    austin_dtc_raw.csv             every Maps card as scraped; keep it, the
                                   list can be rebuilt from it without re-scraping

HOW IT WORKS
    1. Google Maps caps one search at ~120 results, so "e-commerce company in
       Austin" would return 120 of many thousands. Instead it runs 20 search
       terms (online store, clothing brand, skincare, supplements, coffee
       roaster, home goods, pet store ...) once per named area AND once per ZIP
       code - about 2,000 small overlapping searches - and merges them. The
       website is read straight off the result card - no place page is opened
       - which is fast and rarely blocked. By default it stops once 10,000
       unique websites are in hand (--target, counted before cleaning - the
       final list is shorter; raise the target if you need more).
    2. Drops rows with no website, marketplaces and social pages, national
       chains (a known big-box name, or a domain with 5+ addresses on Maps),
       trades that are never a brand (restaurants, salons, dentists ...) and
       repeats of a domain already kept.
    3. "DTC / e-commerce": Maps has no such category, so each homepage is
       fetched once and checked for an online shop - a store platform
       (Shopify, WooCommerce, BigCommerce, Magento ...) or cart / checkout /
       product links. That goes in the sells_online column (yes / maybe / no /
       unknown); rows with no shop are kept but sorted to the bottom, so you
       filter them out in the sheet or check them by hand. Every row is also
       scored on what a growing brand leaves lying around - a hiring system,
       a careers page, "as seen in" press, a wholesale / stockist page, an
       Amazon store, a mobile app, subscriptions, funding news - and sorted by
       that score. A homepage that says the company acquires or holds a
       portfolio of brands is flagged "aggregator?". All of this is a guess
       from public signals, not revenue or headcount data.

TIME
    Full run: ~18 hours of searching at one tab (~9 h with --parallel 2), less
    when --target stops it early; then ~1-2 h reading homepages. Leave it
    running; Ctrl+C and rerun any time.
    --quick: 2 terms x 35 named areas = 70 searches, ~35 min.

USEFUL OPTIONS
    --target 10000       stop once this many unique websites are collected
                         (0 = search everything)
    --parallel 2         search in 2 tabs at once: half the time, more risk of
                         a Google block (it backs off and resumes by itself)
    --quick              first two search terms, named areas only (no ZIP codes)
    --rebuild            skip scraping; rebuild the list from the raw file
    --no-shop-check      skip step 3 (no sells_online column, no sorting)
    --headful            show the browser (use this if it reports a block)
    --queries "a;b"      your own search terms, semicolon-separated
    --locations "a;b"    your own areas, semicolon-separated
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import random
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus, urlsplit, urlunsplit

# ================================================================ REGION
# Everything specific to this region sits between here and END REGION.
# scrape_utah_ecommerce.py is the same file with a different block.

REGION = "Austin"                    # the city column in the output
STATE = "Texas"
OUT_NAME = "austin_dtc"              # file name stem; the output folder is <stem>_output
AREA_CODES = {"512", "737"}          # a local phone number is a small sign of a real local HQ

QUERIES = [
    "e-commerce company",            # --quick runs the first two only
    "online store",
    "clothing brand",
    "boutique",
    "skincare brand",
    "cosmetics store",
    "supplement store",
    "food and beverage brand",
    "coffee roaster",
    "candle store",
    "jewelry store",
    "home goods store",
    "furniture store",
    "pet store",
    "outdoor gear store",
    "gift shop",
    "toy store",
    "specialty food store",
    "leather goods store",
    "consumer products company",
]

# Areas rather than "Austin": each search is capped at ~120 cards, so the only
# way to see the whole metro is many small overlapping searches.
_AUSTIN = ["Downtown", "East Austin", "South Congress", "South Lamar", "Zilker",
           "Bouldin Creek", "Travis Heights", "Mueller", "Hyde Park", "North Loop",
           "The Domain", "Arboretum", "Northwest Hills", "Oak Hill", "Riverside",
           "Montopolis", "Wells Branch", "Tech Ridge", "Southpark Meadows", "Circle C"]
_SUBURBS = ["West Lake Hills", "Bee Cave", "Lakeway", "Dripping Springs", "Sunset Valley",
            "Round Rock", "Pflugerville", "Cedar Park", "Leander", "Georgetown", "Hutto",
            "Manor", "Buda", "Kyle", "San Marcos"]
NEIGHBOURHOODS = [f"{n}, Austin, TX" for n in _AUSTIN] + [f"{n}, TX" for n in _SUBURBS]
# ZIP codes tile the metro far more finely than area names, and Maps accepts
# "online store in 78704, TX". A few numbers may not be real ZIPs; Maps just
# returns nothing for those and the search is marked done.
ZIPS = ([f"787{n:02d}" for n in range(1, 60)
         if n not in (6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 18, 20, 40, 43, 55)]   # Austin proper
        + ["78610", "78613", "78620", "78626", "78628", "78633", "78634", "78640", "78641",
           "78645", "78653", "78660", "78664", "78665", "78666", "78681"])       # the suburbs
LOCATIONS = NEIGHBOURHOODS + [f"{z}, TX" for z in ZIPS]
# ================================================================ END REGION

# Maps pads every search with neighbours. A category containing one of these is
# a trade that is never a consumer brand, so the row is dropped unless the
# business calls itself a brand / shop / store in its own name.
DROP_CATEGORY = [
    "agency", "consultant", "marketing", "advertising", "web design", "restaurant", "cafe",
    "coffee shop", "bar", "pub", "brewpub", "salon", "barber", "spa", "gym", "fitness",
    "yoga", "dentist", "dental", "doctor", "physician", "medical", "clinic", "hospital",
    "pharmacy", "lawyer", "attorney", "law firm", "real estate", "apartment", "property",
    "school", "university", "college", "church", "insurance", "bank", "credit union",
    "hotel", "motel", "repair", "contractor", "plumb", "electrician", "roofing", "hvac",
    "car dealer", "used car", "auto", "gas station", "grocery", "supermarket", "government",
    "storage", "moving", "cleaning", "landscap", "photographer", "wedding", "event venue",
    "museum", "park", "thrift", "pawn", "convenience store", "shopping mall",
    "department store", "printing", "sign shop", "coworking", "co-working", "recruit",
    "staffing", "logistics", "freight", "warehouse", "fulfillment",
    "accountant", "tax", "financial", "mortgage", "veterinar",
    "child care", "day care", "tattoo", "laundry", "dry cleaner", "tutoring",
]
# ... unless the name says otherwise, whatever Maps filed it under
KEEP_NAME = re.compile(r"\b(brand|brands|shop|store|boutique|co\.|goods|supply|apparel|"
                       r"skincare|cosmetics|beauty|supplements?|nutrition|coffee|candles?|"
                       r"jewelry|leather|boots?|outfitters|provisions|foods?)\b", re.I)
CHAIN_LOCATIONS = 5                  # a domain with this many addresses on Maps is a chain

# ---------------------------------------------------------------- maps scraping

RAW_FIELDS = ["name", "website", "category", "rating", "reviews", "address",
              "phone", "hours", "location", "query", "maps_url", "card_text"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

PHONE_RE = re.compile(r"(\+?\d[\d\s()‑-]{7,}\d)")
RATING_RE = re.compile(r"^(\d[.,]\d)\s*(?:\(([\d,]+)\))?$")
HOURS_RE = re.compile(r"^(open|clos|temporarily|permanently|24\s*hours|opens|reopens)", re.I)
# Card chrome, not business data
NOISE_LINES = {"no reviews", "no rating", "new", "sponsored", "website", "directions",
               "online estimates", "on-site services", "online appointments",
               "in-store shopping", "in-store pick-up", "in-store pickup", "delivery",
               "curbside pickup", "wheelchair accessible entrance",
               "identifies as women-owned", "identifies as veteran-owned",
               "identifies as latino-owned", "identifies as black-owned",
               "call", "share", "save", "book online", "order online", "shop online"}


# Google salts card text with icon glyphs from the Unicode Private Use Area
# (U+E000-U+F8FF). They carry no meaning and wreck naive text parsing.
def _clean(text: str) -> str:
    return "".join(ch for ch in text if not ("" <= ch <= "")).strip()


# Pulls every card out of the feed in one evaluate - one round trip per scroll
# rather than one per element, which matters when an area has 120 of them.
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
        a => (a.getAttribute('aria-label') || '').startsWith('Visit ')
             || a.getAttribute('data-value') === 'Website');
    return {
      name: place ? (place.getAttribute('aria-label') || '') : '',
      website: site ? site.href : '',
      maps_url: place ? place.href : '',
      lines: c.innerText.split('\\n').map(s => s.trim()).filter(Boolean),
    };
  }).filter(r => r.name);
}"""

SCROLL_JS = """() => { const f = document.querySelector('div[role="feed"]');
                       if (f) f.scrollTop = f.scrollHeight;
                       return f ? f.innerText.includes("reached the end of the list") : false; }"""


def parse_card(raw: dict, location: str, query: str) -> dict:
    """Best-effort structure from the card's text.

    Only `website` and `name` come from real selectors; everything else is read
    off the rendered text, so the raw lines are kept in `card_text`. If Google
    reshuffles the card layout the extra columns degrade, the URLs do not - and
    the columns can be re-derived from card_text without scraping again.
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

    # The category sits on its own line with no separator when there is no
    # address, so take the first leftover line whole rather than hunting for "·"
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
            "maps_url": raw.get("maps_url", ""), "card_text": " | ".join(lines)}


async def _pass_consent(page) -> None:
    """In Europe Google puts a cookie wall in front of Maps."""
    if "consent.google" not in page.url:
        return
    for label in ("Reject all", "Accept all"):
        btn = page.get_by_role("button", name=label)
        if await btn.count():
            await btn.first.click()
            await page.wait_for_load_state("domcontentloaded")
            return


async def scrape_search(page, query: str, per_search: int):
    """Cards for one search. [] = Maps had nothing; None = Google is blocking."""
    # hl=en matters: the website link is found by its English "Visit ..." label
    url = "https://www.google.com/maps/search/" + quote_plus(query) + "?hl=en&gl=us"
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await _pass_consent(page)
    try:
        await page.wait_for_selector('div[role="feed"]', timeout=25000)
    except Exception:
        body = (await page.inner_text("body")).lower() if await page.query_selector("body") else ""
        if "/sorry/" in page.url or "unusual traffic" in body or "consent.google" in page.url:
            return None
        return []                        # a single-result redirect or "can't find" page

    prev, stagnant = 0, 0
    for _ in range(60):
        n = len({c["name"] for c in await page.evaluate(EXTRACT_JS)})
        if n >= per_search:
            break
        stagnant = stagnant + 1 if n == prev else 0
        if stagnant >= 3:
            break                        # feed exhausted
        prev = n
        if await page.evaluate(SCROLL_JS):
            break                        # Maps said so itself
        await page.wait_for_timeout(random.randint(1100, 1900))

    location = query.split(" in ", 1)[-1]
    return [parse_card(c, location, query) for c in await page.evaluate(EXTRACT_JS)][:per_search]


def read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: str, rows: list[dict], fields: list[str]) -> None:
    tmp = path + ".tmp"                  # never leave a half-written file behind
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    os.replace(tmp, path)


def append_csv(path: str, rows: list[dict], fields: list[str]) -> None:
    """Add rows to a CSV, writing the header when the file is new. With
    thousands of searches, rewriting the whole raw file each time would not do."""
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


async def scrape_all(searches: list[str], raw_path: str, per_search: int, headful: bool,
                     target: int = 0, parallel: int = 1) -> None:
    from playwright.async_api import async_playwright

    # Finished searches are listed in a sidecar, not inferred from the raw rows:
    # a search that found nothing leaves no row, and would be re-run forever.
    done_path = raw_path + ".done"
    rows = read_csv(raw_path)
    done = set(open(done_path, encoding="utf-8").read().splitlines()) \
        if rows and os.path.exists(done_path) else set()
    todo = [s for s in searches if s not in done]
    sites = {host_of(r["website"]) for r in rows if r["website"]}
    if done:
        print(f"[resume] {len(rows)} cards / {len(sites)} unique websites from {len(done)} "
              f"finished searches; {len(todo)} searches to go")
    if not todo:
        return
    if target and len(sites) >= target:
        print(f"already at {len(sites)} unique websites (--target {target}) - nothing more to search")
        return

    queue = list(todo)
    state = {"done": 0, "blocked": 0, "stop": False}
    t0 = time.time()

    async def worker(ctx):
        page = await ctx.new_page()
        try:
            while queue and not state["stop"]:
                search = queue.pop(0)
                try:
                    found = await scrape_search(page, search, per_search)
                except Exception as e:   # a timeout on one search is not a reason to stop
                    print(f"{search} - error, will retry next run: {type(e).__name__}")
                    continue
                if found is None:
                    state["blocked"] += 1
                    print(f"{search} - BLOCKED by Google [{state['blocked']}]")
                    # Grinding on while blocked yields nothing and deepens the block.
                    if state["blocked"] >= 3:
                        print("\nThree blocked searches in a row. Stopping - wait an hour "
                              "and run the same command; it resumes here. If it happens "
                              "again, add --headful and solve the CAPTCHA once.")
                        state["stop"] = True
                        break
                    await asyncio.sleep(random.uniform(20, 40))
                    continue
                state["blocked"] = 0

                append_csv(raw_path, found, RAW_FIELDS)
                with open(done_path, "a", encoding="utf-8") as f:
                    f.write(search + "\n")
                sites.update(host_of(r["website"]) for r in found if r["website"])
                state["done"] += 1
                n = state["done"]
                left = (time.time() - t0) / n * (len(todo) - n) / 60
                print(f"[{n}/{len(todo)}] {search} - {len(found)} cards -> "
                      f"{len(sites)} unique websites so far (~{left:.0f} min left)")
                if target and len(sites) >= target:
                    print(f"\nTarget reached: {len(sites)} unique websites (--target {target}). "
                          f"Run again with a higher --target, or --target 0, to keep going.")
                    state["stop"] = True
                    break
                await asyncio.sleep(random.uniform(6, 14))
        finally:
            await page.close()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headful)
        ctx = await browser.new_context(user_agent=UA, locale="en-US",
                                        viewport={"width": 1400, "height": 950})
        try:
            await asyncio.gather(*[worker(ctx) for _ in range(max(1, parallel))])
        finally:
            await browser.close()

# ---------------------------------------------------------------- cleaning

# Whole domains, never substrings. A substring test looks harmless until it
# throws away terminix.com for containing "x.com".
BAD_DOMAINS = {
    # social / directories / link pages
    "facebook.com", "fb.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "pinterest.com", "whatsapp.com", "wa.me", "yelp.com",
    "bbb.org", "yellowpages.com", "thumbtack.com", "nextdoor.com", "google.com",
    "goo.gl", "business.site", "linktr.ee", "behance.net", "calendly.com",
    # marketplaces - a brand's Amazon or Etsy page is not its own site
    "amazon.com", "etsy.com", "ebay.com", "walmart.com", "target.com", "faire.com",
    "poshmark.com", "mercari.com", "doordash.com", "ubereats.com", "grubhub.com",
    "instacart.com", "shopify.com", "wix.com", "squarespace.com",
}
# National chains and big-box retailers: they sell online, they hire, they have
# press - they would score top of the list and are not the target.
CHAIN_DOMAINS = {
    "costco.com", "bestbuy.com", "homedepot.com", "lowes.com", "kohls.com", "macys.com",
    "nordstrom.com", "nordstromrack.com", "tjx.com", "tjmaxx.tjx.com", "marshalls.com",
    "rossstores.com", "dollartree.com", "dollargeneral.com", "walgreens.com", "cvs.com",
    "heb.com", "wholefoodsmarket.com", "traderjoes.com", "samsclub.com", "michaels.com",
    "hobbylobby.com", "dickssportinggoods.com", "academy.com", "rei.com", "ulta.com",
    "sephora.com", "gap.com", "oldnavy.com", "bananarepublic.com", "nike.com", "adidas.com",
    "lululemon.com", "apple.com", "ikea.com", "petsmart.com", "petco.com", "staples.com",
    "officedepot.com", "barnesandnoble.com", "gamestop.com", "cabelas.com", "basspro.com",
    "tractorsupply.com", "autozone.com", "oreillyauto.com", "7-eleven.com", "cvs.com",
    "bedbathandbeyond.com", "worldmarket.com", "crateandbarrel.com", "potterybarn.com",
    "williams-sonoma.com", "westelm.com", "ashleyfurniture.com", "roomstogo.com",
    "mattressfirm.com", "sleepnumber.com", "verizon.com", "att.com", "t-mobile.com",
    "sprint.com", "xfinity.com", "kroger.com", "randalls.com", "albertsons.com",
    "sprouts.com", "naturalgrocers.com", "gnc.com", "vitaminshoppe.com", "zales.com",
    "kay.com", "jared.com", "pandora.net", "bathandbodyworks.com", "victoriassecret.com",
    "footlocker.com", "finishline.com", "journeys.com", "hm.com", "zara.com", "uniqlo.com",
    "forever21.com", "americaneagle.com", "hollisterco.com", "abercrombie.com", "jcrew.com",
    "anthropologie.com", "urbanoutfitters.com", "freepeople.com", "madewell.com",
    "sherwin-williams.com", "harborfreight.com", "acehardware.com", "wayfair.com",
    "smithsfoodanddrug.com", "harmonsgrocery.com", "maceys.com", "winco.com", "deseretbook.com",
}
TRACKING = re.compile(r"^(utm_|gclid|fbclid|msclkid|mc_|ref|source|y_source)", re.I)
# stores.brand.com / locations.brand.com is a store finder, not a second company
LOCATOR_PREFIXES = {"www", "stores", "store", "locations", "location", "local", "shop", "shops"}


def host_of(url: str) -> str:
    try:
        h = urlsplit(url).netloc.split("@")[-1].split(":")[0].lower()
    except ValueError:
        return ""
    parts = h.split(".")
    while len(parts) > 2 and parts[0] in LOCATOR_PREFIXES:
        parts = parts[1:]
    return ".".join(parts)


def is_bad_host(host: str) -> bool:
    """True for a marketplace, directory or social page rather than the brand's own site."""
    return any(host == d or host.endswith("." + d) for d in BAD_DOMAINS)


def is_chain_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in CHAIN_DOMAINS)


def clean_url(raw: str) -> str:
    """Canonical https URL with the tracking parameters removed."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if not parts.netloc or "." not in parts.netloc:
        return ""
    host = parts.netloc.lower()
    # A store-finder page (stores.brand.com/tx/austin/...) is not the site to
    # contact - the brand's own root is.
    if host.split(".")[0] in LOCATOR_PREFIXES - {"www", "shop", "shops"} \
            or re.search(r"/(stores?|locations?|store-locator|find-a-store)\b", parts.path, re.I):
        return urlunsplit(("https", host_of(raw), "/", "", ""))
    keep = [kv for kv in parts.query.split("&")
            if kv and not TRACKING.match(kv.split("=")[0])]
    return urlunsplit(("https", parts.netloc, parts.path.rstrip("/") or "/", "&".join(keep), ""))


def build_list(raw_rows: list[dict]) -> tuple[list[dict], list[dict], Counter]:
    """raw cards -> (one row per brand, rejects with reasons, reject tally)."""
    # How many different searches surfaced each site, and how many distinct
    # addresses it has - a brand with a dozen storefronts across the metro is a
    # chain, whatever it is called.
    seen_in: dict[str, set] = {}
    addresses: dict[str, set] = {}
    for r in raw_rows:
        h = host_of(clean_url(r.get("website", "")))
        if h:
            seen_in.setdefault(h, set()).add(r.get("query", ""))
            if r.get("address"):
                addresses.setdefault(h, set()).add(r["address"].lower())

    kept: "OrderedDict[str, dict]" = OrderedDict()
    rejects, tally = [], Counter()
    dropped_keys = set()

    def drop(row, why, key):
        if key in dropped_keys:          # the same card turns up in many searches
            return
        dropped_keys.add(key)
        tally[why.split(" (")[0]] += 1
        rejects.append({**row, "dropped_because": why})

    for r in raw_rows:
        name = r.get("name", "")
        url = clean_url(r.get("website", ""))
        if not url:
            drop(r, "no website on Maps", name); continue
        host = host_of(url)
        if host in kept:
            continue                     # same brand, seen in another search
        if is_bad_host(host):
            drop(r, "marketplace, social or directory page, not the brand's own site", host); continue
        if is_chain_host(host):
            drop(r, "national chain or big-box retailer", host); continue
        n_loc = len(addresses.get(host, ()))
        if n_loc >= CHAIN_LOCATIONS:
            drop(r, f"chain or franchise ({n_loc} addresses on Maps) - rescue by hand if it is a local brand", host); continue
        cat = r.get("category", "")
        if any(k in cat.lower() for k in DROP_CATEGORY) and not KEEP_NAME.search(name):
            drop(r, f"not a brand or shop category ({cat or 'blank'})", host); continue
        digits = re.sub(r"\D", "", r.get("phone", ""))[-10:]
        kept[host] = {
            "website": url, "company_name": name, "city": REGION, "state": STATE, "country": "USA",
            "area": r.get("location", "").split(",")[0], "address": r.get("address", ""),
            "phone": r.get("phone", ""),
            "local_phone": ("yes" if digits[:3] in AREA_CODES else "no") if len(digits) == 10 else "",
            "category": cat, "rating": r.get("rating", ""), "reviews": r.get("reviews", ""),
            "searches_seen_in": len(seen_in.get(host, ())), "locations_on_maps": n_loc,
            "sells_online": "", "platform": "", "aggregator": "",
            "growth_tier": "", "growth_score": "", "signals": "",
        }
    return list(kept.values()), rejects, tally

# ---------------------------------------------------------------- shop check

# The store platform is the surest sign a site sells online: every Shopify
# theme loads from cdn.shopify.com, every WooCommerce site ships its plugin
# name, and so on. A custom-built shop is caught by the cart / product links.
PLATFORMS = [
    ("Shopify", re.compile(r"cdn\.shopify\.com|myshopify\.com|Shopify\.theme|shopify-section", re.I)),
    ("WooCommerce", re.compile(r"woocommerce", re.I)),
    ("BigCommerce", re.compile(r"bigcommerce\.com", re.I)),
    ("Magento", re.compile(r"/static/(?:version\d+/)?frontend/|Magento_|/mage/", re.I)),
    ("Salesforce Commerce Cloud", re.compile(r"demandware\.(?:static|net)", re.I)),
    ("Wix Stores", re.compile(r"wixstores|wix-ecommerce|ecom\.wix", re.I)),
    ("Squarespace Commerce", re.compile(r"sqs-cart|squarespace-commerce|Squarespace\.Commerce", re.I)),
    ("Webflow Ecommerce", re.compile(r"w-commerce", re.I)),
    ("Shopware", re.compile(r"shopware", re.I)),
    ("PrestaShop", re.compile(r"prestashop", re.I)),
    ("Squarespace / Wix / Webflow shop", re.compile(r"\"@type\"\s*:\s*\"Product\"", re.I)),
]
CART_SIGNALS = [
    ("add-to-cart button", re.compile(r"add[\s-]+to[\s-]+(?:cart|bag|basket)", re.I)),
    ("cart / checkout link", re.compile(r"""href\s*=\s*["'][^"']*/(?:cart|checkout|bag)\b""", re.I)),
    ("product pages", re.compile(r"""href\s*=\s*["'][^"']*/(?:collections|products|product|shop)/""", re.I)),
    ("shop now", re.compile(r"shop\s+(?:now|all)\b", re.I)),
    ("free shipping", re.compile(r"free\s+shipping", re.I)),
]

HIRING_SYSTEMS = re.compile(r"greenhouse\.io|lever\.co|workable\.com|bamboohr\.com|ashbyhq\.com|"
                            r"smartrecruiters\.com|jobvite\.com|icims\.com|recruitee\.com|"
                            r"breezy\.hr|applytojob\.com|teamtailor\.com|rippling\.com/jobs|"
                            r"workdayjobs\.com|paylocity\.com/recruiting|gusto\.com/job", re.I)
_HREF = r"""href\s*=\s*["'][^"']*(?:%s)"""
CAREERS_LINK = re.compile(_HREF % r"career|/jobs|join-us|join-our-team|work-with-us|work-for-us|"
                                  r"hiring|open-positions|openings|opportunities", re.I)
PRESS = re.compile(r"as\s+seen\s+(?:in|on)|featured\s+in|in\s+the\s+press|/press\b|press\s+kit", re.I)
WHOLESALE = re.compile(r"wholesale|stockists?|store\s+locator|find\s+(?:a|in)\s+store|"
                       r"where\s+to\s+buy|retail\s+partners|find\s+us\s+in\s+stores", re.I)
AMAZON_STORE = re.compile(r"amazon\.com/(?:stores/|shops/|s\?me=|[^\"'\s]*?/dp/)", re.I)
APP_LINK = re.compile(r"apps\.apple\.com|play\.google\.com/store", re.I)
SUBSCRIPTION = re.compile(r"subscribe\s*(?:&|and)\s*save|\bsubscriptions?\b|rechargepayments|"
                          r"skio|loop\s*subscriptions", re.I)
GROWTH_WORDS = re.compile(r"series\s+[abc]\b|raised\s+\$|inc\.?\s*5000|fastest[\s-]growing|"
                          r"now\s+hiring|we'?re\s+hiring", re.I)
AGGREGATOR = re.compile(r"brand\s+aggregator|acquir(?:e|es|ed|ing)\s+(?:amazon\s+|fba\s+|e-?commerce\s+|"
                        r"dtc\s+|consumer\s+|leading\s+|top\s+|and\s+grow\s+)?brands|portfolio\s+of\s+(?:\d+\s+)?"
                        r"(?:consumer\s+|dtc\s+|e-?commerce\s+|amazon\s+)?brands|fba\s+brands|"
                        r"(?:sell|buy)\s+your\s+(?:amazon|fba|e-?commerce|shopify)\s+(?:business|brand|store)|"
                        r"amazon'?s\s+top\s+\d+\s+sellers|top\s+(?:amazon|fba)\s+sellers?|"
                        r"(?:house|family|portfolio)\s+of\s+brands|explore\s+our\s+brands|brands\s+we(?:'ve|\s+have)\s+acquired",
                        re.I)

# Reading public homepages for a few keywords: a broken certificate chain (very
# common on a fresh python.org install on a Mac) should not cost us the site.
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


def root_of(url: str) -> str:
    return urlunsplit(("https", urlsplit(url).netloc, "/", "", ""))


DEAD = "domain does not resolve - the website is gone"


def fetch_plain(url: str) -> tuple[str, str]:
    """(homepage HTML, "") over plain HTTP, or ("", why) if the site would not serve it."""
    why = ""
    # Maps often links a campaign landing page that has since been deleted, so
    # a failure on the listed URL gets one more try at the site root.
    for u in OrderedDict.fromkeys([url, root_of(url)]):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "text/html,*/*",
                                                     "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=15, context=_SSL) as resp:
                html = resp.read(800_000).decode("utf-8", "replace")
            if len(html) >= 1500:        # shorter is a bot-check stub, not a homepage
                return html, ""
            why = "served an empty page"
        except urllib.error.HTTPError as e:
            why = f"HTTP {e.code}"
        except Exception as e:
            why = DEAD if isinstance(getattr(e, "reason", None), socket.gaierror) \
                else type(e).__name__
    return "", why


async def fetch_with_browser(urls: list[str]) -> dict[str, tuple[str, str]]:
    """Second opinion for sites whose firewall turns away anything but a browser."""
    from playwright.async_api import async_playwright
    out: dict[str, tuple[str, str]] = {}
    queue = list(urls)

    async def worker(ctx):
        while queue:
            url = queue.pop()
            out[url] = ("", "did not load")
            for u in OrderedDict.fromkeys([url, root_of(url)]):
                # A page per site: a late redirect on the previous site
                # otherwise aborts the next one's navigation.
                page = await ctx.new_page()
                try:
                    resp = await page.goto(u, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(1500)        # let a JS challenge clear
                    html = await page.content()
                    if resp and resp.status < 400 and len(html) >= 1500:
                        out[url] = (html, "")
                        break
                    out[url] = ("", f"HTTP {resp.status}" if resp else "did not load")
                except Exception as e:
                    out[url] = ("", DEAD if "ERR_NAME_NOT_RESOLVED" in str(e)
                                else "timed out" if "Timeout" in str(e) else "did not load")
                finally:
                    await page.close()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="en-US", ignore_https_errors=True)
        try:
            await asyncio.gather(*[worker(ctx) for _ in range(4)])
        finally:
            await browser.close()
    return out


def check_shop(html: str) -> dict:
    """What a homepage says about selling online and about the size of the brand."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>", " ", html).lower()
    platform = next((name for name, rx in PLATFORMS if rx.search(html)), "")
    cart = [name for name, rx in CART_SIGNALS if rx.search(html)]
    if platform:
        sells = "yes"
    elif len(cart) >= 2:
        sells = "yes"
    elif cart:
        sells = "maybe"
    else:
        sells = "no"

    score, signals = 0, []
    if platform:
        signals.append(f"shop on {platform}")
    elif cart:
        signals.append("shop signals: " + ", ".join(cart))
    if HIRING_SYSTEMS.search(html):
        score += 3; signals.append("uses a hiring system")
    if CAREERS_LINK.search(html):
        score += 2; signals.append("careers page")
    if PRESS.search(html):
        score += 1; signals.append("press / as seen in")
    if WHOLESALE.search(text):
        score += 1; signals.append("wholesale / stockists")
    if AMAZON_STORE.search(html):
        score += 1; signals.append("Amazon store")
    if APP_LINK.search(html):
        score += 1; signals.append("mobile app")
    if SUBSCRIPTION.search(html):
        score += 1; signals.append("subscriptions")
    if GROWTH_WORDS.search(text):
        score += 1; signals.append("hiring / funding / growth wording")
    aggregator = "aggregator?" if AGGREGATOR.search(text) else ""
    if aggregator:
        signals.append("says it acquires or holds a portfolio of brands")
    return {"sells_online": sells, "platform": platform, "score": score,
            "signals": "; ".join(signals), "aggregator": aggregator}


def add_shop_info(rows: list[dict], cache: dict[str, dict]) -> None:
    todo = [r["website"] for r in rows if r["website"] not in cache]
    if todo:
        print(f"shop check: reading {len(todo)} homepages ...")
    pages: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=24) as pool:
        for n, (url, got) in enumerate(zip(todo, pool.map(fetch_plain, todo)), 1):
            pages[url] = got
            if n % 100 == 0:
                print(f"  {n}/{len(todo)}")

    # A domain that does not resolve will not resolve in a browser either -
    # only sites that answered with a refusal get the second look.
    refused = [u for u in todo if not pages[u][0] and pages[u][1] != DEAD]
    if refused:
        print(f"  {len(refused)} refused a plain request - retrying those in a real browser ...")
        try:
            pages.update(asyncio.run(fetch_with_browser(refused)))
        except ImportError:
            pass                         # --rebuild on a machine without Playwright
    for url in todo:
        html, why = pages[url]
        if html:
            cache[url] = check_shop(html)
        elif why == DEAD:
            cache[url] = {"sells_online": "unknown", "platform": "", "score": -2,
                          "signals": DEAD, "aggregator": ""}
        elif why in ("HTTP 401", "HTTP 403", "HTTP 418", "HTTP 422", "HTTP 429", "HTTP 503"):
            # A firewall - which is itself a sign of a bigger shop
            cache[url] = {"sells_online": "unknown", "platform": "", "score": -1,
                          "signals": f"site turns away automated visitors ({why}) - check by hand",
                          "aggregator": ""}
        else:
            cache[url] = {"sells_online": "unknown", "platform": "", "score": -1,
                          "signals": f"site did not load ({why}) - check by hand", "aggregator": ""}

    # "Shown in many searches" has to be relative: a full run is thousands of searches
    # and almost everyone clears any fixed bar. Top quarter, and never below 4.
    seen = sorted(int(r["searches_seen_in"]) for r in rows)
    often = max(4, seen[len(seen) * 3 // 4] + 1) if seen else 4

    for r in rows:
        c = cache[r["website"]]
        score, signals = int(c["score"]), c["signals"]
        if score >= 0:
            # Maps-side evidence counts too, a little
            if int(r["reviews"] or 0) >= 20:
                score += 1; signals = (signals + "; " if signals else "") + "20+ Google reviews"
            if int(r["searches_seen_in"]) >= often:
                score += 1; signals = (signals + "; " if signals else "") + \
                    f"shown in {often}+ searches (top quarter)"
        r["sells_online"] = c["sells_online"]
        r["platform"] = c["platform"]
        r["aggregator"] = c["aggregator"]
        r["growth_score"] = score if score >= 0 else ""
        r["signals"] = signals
        r["growth_tier"] = ("dead site" if score == -2 else "unknown" if score < 0
                            else "strong signals" if score >= 5
                            else "some signals" if score >= 2 else "few signals")
    # Shops first, then the strength of the growth signals; sites with no shop
    # found sit at the bottom, dead sites last of all.
    sells = {"yes": 0, "maybe": 1, "unknown": 2, "no": 3}
    order = {"strong signals": 3, "some signals": 2, "unknown": 1.5, "few signals": 1, "dead site": 0}
    rows.sort(key=lambda r: (r["growth_tier"] == "dead site", sells[r["sells_online"]],
                             -order[r["growth_tier"]], -(r["growth_score"] or 0)))

# ---------------------------------------------------------------- output

def write_xlsx_parts(out_dir: str, stem: str, rows: list[dict], fields: list[str], size=900) -> int:
    try:
        from openpyxl import Workbook
    except ImportError:
        print("(openpyxl not installed - skipped the .xlsx files; the .csv has everything)")
        return 0
    for f in os.listdir(out_dir):        # a shorter list must not leave old parts behind
        if f.startswith(stem + " part ") and f.endswith(".xlsx"):
            os.remove(os.path.join(out_dir, f))
    chunks = [rows[i:i + size] for i in range(0, len(rows), size)] or [[]]
    for n, chunk in enumerate(chunks, 1):
        wb = Workbook(); ws = wb.active; ws.title = "Brands"
        ws.append(fields)
        for r in chunk:
            ws.append([r.get(k, "") for k in fields])
        wb.save(os.path.join(out_dir, f"{stem} part {n} of {len(chunks)}.xlsx"))
    return len(chunks)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), OUT_NAME + "_output"))
    ap.add_argument("--name", default=OUT_NAME, help="file name stem for the outputs")
    ap.add_argument("--quick", action="store_true",
                    help="first two search terms, named areas only (no ZIP codes)")
    ap.add_argument("--queries", help="semicolon-separated search terms")
    ap.add_argument("--locations", help="semicolon-separated areas, e.g. 'Downtown, Austin, TX'")
    ap.add_argument("--per-search", type=int, default=200,
                    help="Maps caps a search near 120; the default means 'everything'")
    ap.add_argument("--target", type=int, default=10000,
                    help="stop searching once this many unique websites are collected (0 = no limit)")
    ap.add_argument("--parallel", type=int, default=1,
                    help="browser tabs searching at once; 2 halves the time, raises the block risk")
    ap.add_argument("--rebuild", action="store_true", help="no scraping, rebuild from the raw file")
    ap.add_argument("--no-shop-check", action="store_true")
    ap.add_argument("--headful", action="store_true", help="show the browser window")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace", line_buffering=True)   # Windows consoles

    queries = [q.strip() for q in (args.queries or "").split(";") if q.strip()] or QUERIES
    if args.quick:
        queries = queries[:2]
    # Semicolons, not commas: an area is written "Downtown, Austin, TX"
    locations = [l.strip() for l in (args.locations or "").split(";") if l.strip()] \
        or (NEIGHBOURHOODS if args.quick else LOCATIONS)
    # Term-major order: the main term covers the whole metro before the second
    # term starts, so stopping early still leaves a complete list for term one.
    searches = [f"{q} in {loc}" for q in queries for loc in locations]

    os.makedirs(args.out_dir, exist_ok=True)
    path = lambda suffix: os.path.join(args.out_dir, args.name + suffix)

    if not args.rebuild:
        try:
            import playwright  # noqa: F401
        except ImportError:
            print("Playwright is not installed. Run these two, then try again:\n"
                  f"    {os.path.basename(sys.executable)} -m pip install playwright openpyxl\n"
                  f"    {os.path.basename(sys.executable)} -m playwright install chromium")
            return 2
        hours = len(searches) * 30 / 3600 / max(1, args.parallel)
        print(f"{len(searches)} searches ({len(queries)} term(s) x {len(locations)} areas) "
              f"- up to ~{hours:.1f} h at ~30 s each"
              + (f"; stops early at {args.target} unique websites" if args.target else ""))
        try:
            asyncio.run(scrape_all(searches, path("_raw.csv"), args.per_search, args.headful,
                                   target=args.target, parallel=args.parallel))
        except KeyboardInterrupt:
            print("\nstopped - building the list from what was collected")

    raw = read_csv(path("_raw.csv"))
    if not raw:
        print("nothing scraped yet"); return 1
    rows, rejects, tally = build_list(raw)

    if not args.no_shop_check:
        cache = {r["website"]: {"sells_online": r["sells_online"], "platform": r["platform"],
                                "score": r["score"], "signals": r["signals"],
                                "aggregator": r["aggregator"]}
                 for r in read_csv(path("_shopcache.csv"))}
        try:
            add_shop_info(rows, cache)
        finally:                         # a rebuild must not re-fetch 2,000 homepages
            write_csv(path("_shopcache.csv"),
                      [{"website": w, **c} for w, c in cache.items()],
                      ["website", "sells_online", "platform", "score", "signals", "aggregator"])

    fields = list(rows[0]) if rows else ["website"]
    if args.no_shop_check:
        fields = [f for f in fields if f not in ("sells_online", "platform", "aggregator",
                                                 "growth_tier", "growth_score", "signals")]
    write_csv(path(".csv"), rows, fields)
    write_csv(path("_rejects.csv"), rejects, RAW_FIELDS + ["dropped_because"])
    parts = write_xlsx_parts(args.out_dir, args.name, rows, fields)

    print(f"\n{len(raw)} cards scraped -> {len(rows)} companies with their own website")
    for why, n in tally.most_common():
        print(f"  dropped {n:>5}  {why}")
    if not args.no_shop_check:
        for sells, n in Counter(r["sells_online"] for r in rows).most_common():
            print(f"  {n:>5}  sells online: {sells}")
        for tier, n in Counter(r["growth_tier"] for r in rows).most_common():
            print(f"  {n:>5}  {tier}")
        agg = sum(1 for r in rows if r["aggregator"])
        if agg:
            print(f"  {agg:>5}  flagged aggregator?")
    print(f"\nlist     {path('.csv')}" + (f"   (+ {parts} xlsx part(s))" if parts else ""))
    print(f"rejects  {path('_rejects.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
