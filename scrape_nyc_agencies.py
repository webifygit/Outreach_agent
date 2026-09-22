#!/usr/bin/env python3
"""Website URLs of digital marketing agencies in New York City - one file.

SETUP (once)
    Mac:      python3 -m pip install playwright openpyxl
              python3 -m playwright install chromium
    Windows:  py -m pip install playwright openpyxl
              py -m playwright install chromium

RUN
    python3 scrape_nyc_agencies.py            (Windows: py scrape_nyc_agencies.py)

    Stop it with Ctrl+C whenever you like and run the same command again - it
    carries on from the search it had reached. Everything lands in the folder
    nyc_agencies_output/ next to this file:

    nyc_agencies.csv               the list: one row per agency, best first
    nyc_agencies part 1 of N.xlsx  same list in 900-row pieces (Google Sheets
                                   refuses an import over 1,000 rows)
    nyc_agencies_rejects.csv       everything dropped, with the reason - read it
    nyc_agencies_raw.csv           every Maps card as scraped; keep it, the
                                   list can be rebuilt from it without re-scraping

HOW IT WORKS
    1. Google Maps caps one search at ~120 results, so "agencies in New York"
       would return 120 of several thousand. Instead it runs each search term
       once per neighbourhood (SoHo, Flatiron, DUMBO ...) and merges them.
       The website is read straight off the result card - no place page is
       opened - which is ~36x faster and far less likely to get blocked.
    2. Drops rows with no website, Facebook/Yelp/Clutch-style pages, the wrong
       trade, and repeats of a domain already kept.
    3. "Mid-to-large": Maps does not publish headcount, so nothing is dropped
       for size. Each agency's homepage is fetched once and scored on what a
       bigger shop leaves lying around - a careers page, a hiring system
       (Greenhouse, Lever ...), a team page, offices in other cities. The list
       is sorted by that score and carries size_tier / size_signals columns, so
       you filter in the sheet and can see why each one scored as it did.
       It is a guess from public signals, not a headcount.

USEFUL OPTIONS
    --quick              main search term only (~20 min instead of ~75 min;
                         finds roughly half as many agencies)
    --rebuild            skip scraping; rebuild the list from the raw file
    --no-size-check      skip step 3
    --headful            show the browser (use this if it reports a block)
    --queries "a;b"      your own search terms, semicolon-separated
    --locations "a;b"    your own areas - this is how the same file does
                         Austin or Salt Lake City
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

# ---------------------------------------------------------------- what to search

QUERIES = [
    "digital marketing agency",          # --quick runs only this one
    "advertising agency",
    "SEO agency",
    "social media marketing agency",
]

# Neighbourhoods rather than "New York": each search is capped at ~120 cards,
# so the only way to see the whole city is many small overlapping searches.
_MANHATTAN = ["Financial District", "Tribeca", "SoHo", "Lower East Side", "East Village",
              "West Village", "Greenwich Village", "Union Square", "Flatiron District",
              "NoMad", "Chelsea", "Gramercy", "Murray Hill", "Garment District",
              "Midtown", "Midtown East", "Times Square", "Hudson Yards",
              "Hell's Kitchen", "Upper East Side", "Upper West Side", "Harlem"]
_BROOKLYN = ["DUMBO", "Downtown Brooklyn", "Williamsburg", "Greenpoint", "Bushwick",
             "Park Slope", "Gowanus", "Sunset Park", "Brooklyn Navy Yard"]
_QUEENS = ["Long Island City", "Astoria", "Flushing", "Forest Hills", "Jamaica"]
LOCATIONS = ([f"{n}, Manhattan, New York, NY" for n in _MANHATTAN]
             + [f"{n}, Brooklyn, NY" for n in _BROOKLYN]
             + [f"{n}, Queens, NY" for n in _QUEENS]
             + ["The Bronx, New York, NY", "Staten Island, New York, NY"])

# A Maps category has to contain one of these to count as an agency. Maps pads
# every search with neighbours - printers, recruiters, co-working spaces.
# "software" is here on purpose: Maps files several real digital agencies
# (Work & Co, Mint Digital) under "Software company", and losing a 400-person
# agency costs more than letting the odd SaaS firm through.
KEEP_CATEGORY = ["marketing", "advertis", "seo", "social media", "media",
                 "brand", "digital", "design", "creative", "public relations",
                 "e-commerce", "ecommerce", "video production", "software"]
# ... or the business can say so in its own name, whatever Maps filed it under
KEEP_NAME = re.compile(r"\b(agency|marketing|advertising|digital|media|creative|seo|"
                       r"branding|design)\b", re.I)

NYC_AREA_CODES = {"212", "332", "646", "917", "718", "347", "929"}

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
               "in-store shopping", "in-store pick-up", "delivery",
               "wheelchair accessible entrance", "identifies as women-owned",
               "call", "share", "save", "book online", "order online"}


# Google salts card text with icon glyphs from the Unicode Private Use Area
# (U+E000-U+F8FF). They carry no meaning and wreck naive text parsing.
def _clean(text: str) -> str:
    return "".join(ch for ch in text if not ("\ue000" <= ch <= "\uf8ff")).strip()


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


async def scrape_all(searches: list[str], raw_path: str, per_search: int, headful: bool) -> None:
    from playwright.async_api import async_playwright

    # Finished searches are listed in a sidecar, not inferred from the raw rows:
    # a search that found nothing leaves no row, and would be re-run forever.
    done_path = raw_path + ".done"
    rows = read_csv(raw_path)
    done = set(open(done_path, encoding="utf-8").read().splitlines()) \
        if rows and os.path.exists(done_path) else set()
    todo = [s for s in searches if s not in done]
    if done:
        print(f"[resume] {len(rows)} cards from {len(done)} finished searches; {len(todo)} to go")
    if not todo:
        return

    blocked, t0 = 0, time.time()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headful)
        ctx = await browser.new_context(user_agent=UA, locale="en-US",
                                        viewport={"width": 1400, "height": 950})
        page = await ctx.new_page()
        try:
            for i, search in enumerate(todo, 1):
                try:
                    found = await scrape_search(page, search, per_search)
                except Exception as e:   # a timeout on one search is not a reason to stop
                    print(f"[{i}/{len(todo)}] {search} - error, will retry next run: "
                          f"{type(e).__name__}")
                    continue
                if found is None:
                    blocked += 1
                    print(f"[{i}/{len(todo)}] {search} - BLOCKED by Google [{blocked}]")
                    # Grinding on while blocked yields nothing and deepens the block.
                    if blocked >= 3:
                        print("\nThree blocked searches in a row. Stopping - wait an hour "
                              "and run the same command; it resumes here. If it happens "
                              "again, add --headful and solve the CAPTCHA once.")
                        break
                    await asyncio.sleep(random.uniform(20, 40))
                    continue
                blocked = 0

                rows.extend(found)
                write_csv(raw_path, rows, RAW_FIELDS)
                with open(done_path, "a", encoding="utf-8") as f:
                    f.write(search + "\n")
                sites = {host_of(r["website"]) for r in rows if r["website"]}
                left = (time.time() - t0) / i * (len(todo) - i) / 60
                print(f"[{i}/{len(todo)}] {search} - {len(found)} cards -> "
                      f"{len(sites)} unique websites so far (~{left:.0f} min left)")
                await asyncio.sleep(random.uniform(6, 14))
        finally:
            await browser.close()

# ---------------------------------------------------------------- cleaning

# Whole domains, never substrings. A substring test looks harmless until it
# throws away terminix.com for containing "x.com".
BAD_DOMAINS = {
    "facebook.com", "fb.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "pinterest.com", "whatsapp.com", "wa.me", "yelp.com",
    "bbb.org", "yellowpages.com", "thumbtack.com", "nextdoor.com", "google.com",
    "goo.gl", "business.site", "linktr.ee", "clutch.co", "upcity.com", "sortlist.com",
    "designrush.com", "goodfirms.co", "agencyspotter.com", "behance.net", "dribbble.com",
    "fiverr.com", "upwork.com", "calendly.com",
}
TRACKING = re.compile(r"^(utm_|gclid|fbclid|msclkid|mc_|ref|source|y_source)", re.I)


def host_of(url: str) -> str:
    try:
        h = urlsplit(url).netloc.split("@")[-1].split(":")[0].lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def is_bad_host(host: str) -> bool:
    """True for a directory or social page rather than a company's own site."""
    return any(host == d or host.endswith("." + d) for d in BAD_DOMAINS)


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
    keep = [kv for kv in parts.query.split("&")
            if kv and not TRACKING.match(kv.split("=")[0])]
    return urlunsplit(("https", parts.netloc, parts.path.rstrip("/") or "/", "&".join(keep), ""))


def build_list(raw_rows: list[dict]) -> tuple[list[dict], list[dict], Counter]:
    """raw cards -> (one row per agency, rejects with reasons, reject tally)."""
    # How many different searches surfaced each site. An agency Maps shows for
    # six neighbourhoods and three search terms is a more established one.
    seen_in: dict[str, set] = {}
    for r in raw_rows:
        h = host_of(clean_url(r.get("website", "")))
        if h:
            seen_in.setdefault(h, set()).add(r.get("query", ""))

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
            continue                     # same agency, seen in another search
        if is_bad_host(host):
            drop(r, "social or directory page, not the agency's own site", host); continue
        cat = r.get("category", "")
        if not any(k in cat.lower() for k in KEEP_CATEGORY) and not KEEP_NAME.search(name):
            drop(r, f"not an agency category ({cat or 'blank'})", host); continue
        digits = re.sub(r"\D", "", r.get("phone", ""))[-10:]
        kept[host] = {
            "website": url, "company_name": name, "city": "New York", "country": "USA",
            "area": r.get("location", "").split(",")[0], "address": r.get("address", ""),
            "phone": r.get("phone", ""),
            "nyc_phone": ("yes" if digits[:3] in NYC_AREA_CODES else "no") if len(digits) == 10 else "",
            "category": cat, "rating": r.get("rating", ""), "reviews": r.get("reviews", ""),
            "searches_seen_in": len(seen_in.get(host, ())),
            "size_tier": "", "size_score": "", "size_signals": "",
        }
    return list(kept.values()), rejects, tally

# ---------------------------------------------------------------- size check

HIRING_SYSTEMS = re.compile(r"greenhouse\.io|lever\.co|workable\.com|bamboohr\.com|ashbyhq\.com|"
                            r"smartrecruiters\.com|jobvite\.com|icims\.com|recruitee\.com|"
                            r"breezy\.hr|applytojob\.com|teamtailor\.com|rippling\.com/jobs|"
                            r"workdayjobs\.com|paylocity\.com/recruiting", re.I)
_HREF = r"""href\s*=\s*["'][^"']*(?:%s)"""
CAREERS_LINK = re.compile(_HREF % r"career|/jobs|join-us|join-our-team|work-with-us|work-for-us|"
                                  r"hiring|open-positions|openings|opportunities", re.I)
TEAM_LINK = re.compile(_HREF % r"our-team|/team|leadership|/people|who-we-are|our-people", re.I)
WORK_LINK = re.compile(_HREF % r"case-stud|/work|our-work|/clients|portfolio|success-stor", re.I)
OTHER_OFFICES = ["london", "los angeles", "chicago", "san francisco", "miami", "toronto",
                 "boston", "austin", "dallas", "atlanta", "seattle", "denver", "singapore",
                 "sydney", "dubai", "berlin", "paris", "amsterdam", "washington", "philadelphia",
                 "hong kong", "tokyo", "mumbai", "são paulo", "sao paulo", "mexico city"]

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


def score_html(html: str) -> tuple[int, str]:
    """(score, signals) from a homepage."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>", " ", html).lower()
    score, signals = 0, []
    if HIRING_SYSTEMS.search(html):
        score += 3; signals.append("uses a hiring system")
    if CAREERS_LINK.search(html):
        score += 2; signals.append("careers page")
    offices = [c for c in OTHER_OFFICES if re.search(rf"\b{re.escape(c)}\b", text)]
    if len(offices) >= 2:
        score += 2; signals.append("other cities named: " + ", ".join(offices[:4]))
    if TEAM_LINK.search(html):
        score += 1; signals.append("team page")
    if WORK_LINK.search(html):
        score += 1; signals.append("case studies / clients page")
    return score, "; ".join(signals)


def add_sizes(rows: list[dict], cache: dict[str, dict]) -> None:
    todo = [r["website"] for r in rows if r["website"] not in cache]
    if todo:
        print(f"size check: reading {len(todo)} homepages ...")
    pages: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        for n, (url, got) in enumerate(zip(todo, pool.map(fetch_plain, todo)), 1):
            pages[url] = got
            if n % 100 == 0:
                print(f"  {n}/{len(todo)}")

    refused = [u for u in todo if not pages[u][0]]
    if refused:
        print(f"  {len(refused)} refused a plain request - retrying those in a real browser ...")
        try:
            pages.update(asyncio.run(fetch_with_browser(refused)))
        except ImportError:
            pass                         # --rebuild on a machine without Playwright
    for url in todo:
        html, why = pages[url]
        if html:
            score, signals = score_html(html)
            cache[url] = {"score": score, "signals": signals}
        elif why == DEAD:
            cache[url] = {"score": -2, "signals": DEAD}
        elif why in ("HTTP 401", "HTTP 403", "HTTP 418", "HTTP 422", "HTTP 429", "HTTP 503"):
            # A firewall - which is itself a sign of a bigger shop
            cache[url] = {"score": -1, "signals": f"site turns away automated visitors "
                                                  f"({why}) - check by hand"}
        else:
            cache[url] = {"score": -1, "signals": f"site did not load ({why}) - check by hand"}

    # "Shown in many searches" has to be relative: a full run is 150 searches
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
        r["size_score"] = score if score >= 0 else ""
        r["size_signals"] = signals
        r["size_tier"] = ("dead site" if score == -2 else "unknown" if score < 0
                          else "likely mid-large" if score >= 5
                          else "possible" if score >= 3 else "likely small")
    # unknowns sit between "possible" and "likely small"; dead sites go last
    order = {"unknown": 2.5, "dead site": -1}
    rows.sort(key=lambda r: -order.get(r["size_tier"], r["size_score"] or 0))

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
        wb = Workbook(); ws = wb.active; ws.title = "Agencies"
        ws.append(fields)
        for r in chunk:
            ws.append([r.get(k, "") for k in fields])
        wb.save(os.path.join(out_dir, f"{stem} part {n} of {len(chunks)}.xlsx"))
    return len(chunks)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "nyc_agencies_output"))
    ap.add_argument("--name", default="nyc_agencies", help="file name stem for the outputs")
    ap.add_argument("--quick", action="store_true", help="first search term only")
    ap.add_argument("--queries", help="semicolon-separated search terms")
    ap.add_argument("--locations", help="semicolon-separated areas, e.g. 'Downtown, Austin, TX'")
    ap.add_argument("--per-search", type=int, default=200,
                    help="Maps caps a search near 120; the default means 'everything'")
    ap.add_argument("--rebuild", action="store_true", help="no scraping, rebuild from the raw file")
    ap.add_argument("--no-size-check", action="store_true")
    ap.add_argument("--headful", action="store_true", help="show the browser window")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace", line_buffering=True)   # Windows consoles

    queries = [q.strip() for q in (args.queries or "").split(";") if q.strip()] or QUERIES
    if args.quick:
        queries = queries[:1]
    # Semicolons, not commas: an area is written "SoHo, Manhattan, New York, NY"
    locations = [l.strip() for l in (args.locations or "").split(";") if l.strip()] or LOCATIONS
    # Term-major order: the main term covers the whole city before the second
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
        print(f"{len(searches)} searches ({len(queries)} term(s) x {len(locations)} areas)")
        try:
            asyncio.run(scrape_all(searches, path("_raw.csv"), args.per_search, args.headful))
        except KeyboardInterrupt:
            print("\nstopped - building the list from what was collected")

    raw = read_csv(path("_raw.csv"))
    if not raw:
        print("nothing scraped yet"); return 1
    rows, rejects, tally = build_list(raw)

    if not args.no_size_check:
        cache = {r["website"]: {"score": r["size_score"], "signals": r["size_signals"]}
                 for r in read_csv(path("_sizecache.csv"))}
        try:
            add_sizes(rows, cache)
        finally:                         # a rebuild must not re-fetch 2,000 homepages
            write_csv(path("_sizecache.csv"),
                      [{"website": w, "size_score": c["score"], "size_signals": c["signals"]}
                       for w, c in cache.items()],
                      ["website", "size_score", "size_signals"])

    fields = list(rows[0]) if rows else ["website"]
    if args.no_size_check:
        fields = [f for f in fields if not f.startswith("size_")]
    write_csv(path(".csv"), rows, fields)
    write_csv(path("_rejects.csv"), rejects, RAW_FIELDS + ["dropped_because"])
    parts = write_xlsx_parts(args.out_dir, args.name, rows, fields)

    print(f"\n{len(raw)} cards scraped -> {len(rows)} agencies with their own website")
    for why, n in tally.most_common():
        print(f"  dropped {n:>5}  {why}")
    if not args.no_size_check:
        for tier, n in Counter(r["size_tier"] for r in rows).most_common():
            print(f"  {n:>5}  {tier}")
    print(f"\nlist     {path('.csv')}" + (f"   (+ {parts} xlsx part(s))" if parts else ""))
    print(f"rejects  {path('_rejects.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
