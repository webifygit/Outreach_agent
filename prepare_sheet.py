#!/usr/bin/env python3
"""Turn a raw Google-Maps scrape into a sheet the outreach agent can use.

The scraper's job is to capture what Maps shows; this decides what is worth
approaching. Keeping them apart means the expensive scrape is done once and
the rules here can be re-run for free whenever they change.

    python prepare_sheet.py --input raw.csv --output uploads/ready.csv \
        --categories pest --min-branches 1 --max-branches 8

What it removes, and why:
  no website        Maps has no site on file - there is no form to fill
  duplicate host    114 Orkin branches share one orkin.com contact form
  wrong category    "pest control in Houston" also returns Walmart
  social/aggregator a Facebook page is not a contact form
  national chains   --max-branches drops the franchises whose marketing is
                    already handled centrally, keeping local businesses
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import re
import sys
from collections import Counter, OrderedDict
from urllib.parse import urlsplit, urlunsplit

# Whole domains, never substrings. A substring test looks harmless until it
# throws away terminix.com for containing "x.com" - which it did, along with
# prestox, spidexx and paratex: 118 real pest control firms silently dropped.
BAD_DOMAINS = {
    "facebook.com", "m.facebook.com", "fb.com", "instagram.com", "linkedin.com",
    "twitter.com", "x.com", "youtube.com", "tiktok.com", "pinterest.com",
    "whatsapp.com", "wa.me", "yelp.com", "bbb.org", "yellowpages.com",
    "angi.com", "angieslist.com", "thumbtack.com", "houzz.com", "nextdoor.com",
    "google.com", "goo.gl", "business.site", "linktr.ee", "sites.google.com",
}


def is_bad_host(host: str) -> bool:
    """True for a directory or social page rather than a company's own site."""
    return any(host == d or host.endswith("." + d) for d in BAD_DOMAINS)

TRACKING = re.compile(r"^(utm_|gclid|fbclid|msclkid|mc_|ref|source)", re.I)


def clean_url(raw: str) -> str:
    """Canonical https URL, tracking parameters removed.

    Path is kept - some businesses genuinely live at a subpath - but the query
    string is dropped, because a utm-tagged Maps link can resolve to a campaign
    landing page that has no contact form on it.
    """
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
    return urlunsplit(("https", parts.netloc, parts.path.rstrip("/") or "/",
                       "&".join(keep), ""))


def host_of(url: str) -> str:
    host = urlsplit(url).netloc.split("@")[-1].split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


def col(row: dict, *names: str) -> str:
    """First matching column, case-insensitively - scrapers vary in casing."""
    lower = {k.lower(): v for k, v in row.items() if k}
    for n in names:
        if lower.get(n.lower()):
            return str(lower[n.lower()]).strip()
    return ""


def city_slug(location: str) -> str:
    """'Manchester, UK' -> 'manchester'. The country suffix is noise in a filename."""
    head = (location or "unknown").split(",")[0].strip()
    return re.sub(r"[^A-Za-z0-9]+", "_", head).strip("_").lower() or "unknown"


def write_by_city(rows: list[dict], book_path: str) -> tuple[int, str]:
    """One workbook, a tab per city, plus a Summary tab.

    Deliberately not one file per city: 68 loose CSVs are harder to look at and
    to send on than a single workbook you can flick between tabs in. The
    combined CSV that the agent actually runs is written separately.
    """
    import pandas as pd

    book = pathlib.Path(book_path)
    if book.suffix.lower() != ".xlsx":
        book = book.with_suffix(".xlsx")
    book.parent.mkdir(parents=True, exist_ok=True)

    by_city: dict[str, list[dict]] = {}
    for r in rows:
        by_city.setdefault(r.get("city") or "unknown", []).append(r)
    ordered = sorted(by_city.items(), key=lambda kv: -len(kv[1]))

    with pd.ExcelWriter(book, engine="openpyxl") as xl:
        summary = pd.DataFrame([{"city": c, "businesses": len(b)} for c, b in ordered])
        summary.loc[len(summary)] = {"city": "TOTAL", "businesses": len(rows)}
        summary.to_excel(xl, sheet_name="Summary", index=False)
        pd.DataFrame(rows).to_excel(xl, sheet_name=f"All ({len(rows)})"[:31], index=False)
        used = set()
        for city, block in ordered:
            # Excel: 31 chars max, none of : \ / ? * [ ], and no duplicates
            tab = re.sub(r"[:\\/?*\[\]]", "-", city.split(",")[0]).strip()[:24] or "unknown"
            name = f"{tab} ({len(block)})"[:31]
            n = 2
            while name.lower() in used:
                name = f"{tab} ({len(block)})~{n}"[:31]; n += 1
            used.add(name.lower())
            pd.DataFrame(block).to_excel(xl, sheet_name=name, index=False)
    return len(by_city), str(book)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--categories", default="",
                    help="comma-separated substrings a row's category must match, "
                         "e.g. 'pest,exterminat,animal control'. Empty = keep all.")
    ap.add_argument("--max-branches", type=int, default=0,
                    help="drop domains appearing more than N times (0 = keep all). "
                         "8 is a reasonable line between a local multi-site "
                         "operator and a national franchise.")
    ap.add_argument("--rejects", default="", help="optional CSV of everything dropped, with a reason")
    ap.add_argument("--by-city", default="",
                    help="also write ONE .xlsx workbook with a tab per city "
                         "(plus Summary and All tabs)")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.input, newline="", encoding="utf-8-sig")))
    if not rows:
        print("input is empty", file=sys.stderr)
        return 1
    wanted = [c.strip().lower() for c in args.categories.split(",") if c.strip()]

    # pass 1 - how many rows share each host, so franchises can be recognised
    counts = Counter()
    for r in rows:
        h = host_of(clean_url(col(r, "website", "websites", "url", "site")))
        if h:
            counts[h] += 1

    kept: "OrderedDict[str, dict]" = OrderedDict()
    rejects, tally = [], Counter()

    def drop(row, why):
        tally[why] += 1
        if args.rejects:
            rejects.append({**row, "_dropped_because": why})

    for r in rows:
        url = clean_url(col(r, "website", "websites", "url", "site"))
        if not url:
            drop(r, "no website on file"); continue
        host = host_of(url)
        if is_bad_host(host):
            drop(r, "social or directory page, not a company site"); continue
        cat = col(r, "category").lower()
        if wanted and not any(w in cat for w in wanted):
            drop(r, f"category not wanted ({col(r,'category') or 'blank'})"); continue
        if args.max_branches and counts[host] > args.max_branches:
            drop(r, f"national chain ({counts[host]} branches)"); continue
        if host in kept:
            drop(r, "duplicate of a site already kept"); continue
        kept[host] = {
            "website": url,
            "company_name": col(r, "name", "company_name", "company"),
            # scrape_maps_fast writes the search area as "location"; other
            # exports call it "city". Either is the city for our purposes.
            "city": col(r, "city", "location", "town", "area"),
            "country": col(r, "country"),
            "phone": col(r, "phone"), "category": col(r, "category"),
            "rating": col(r, "rating"), "reviews": col(r, "reviews"),
            "branches_in_sheet": counts[host],
            "google_maps_url": col(r, "google_maps_url"),
        }

    out_rows = list(kept.values())
    # .xlsx or .csv, whichever the output name asks for - the agent reads both,
    # and Excel is what a person actually opens.
    if pathlib.Path(args.output).suffix.lower() in (".xlsx", ".xls"):
        import pandas as pd
        pathlib.Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(out_rows or [{"website": ""}]).to_excel(
            args.output, sheet_name="Businesses", index=False)
    else:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0]) if out_rows else ["website"])
            w.writeheader(); w.writerows(out_rows)

    if args.rejects and rejects:
        with open(args.rejects, "w", newline="", encoding="utf-8") as f:
            names = list({k for r in rejects for k in r})
            w = csv.DictWriter(f, fieldnames=names); w.writeheader(); w.writerows(rejects)

    print(f"in  {len(rows)} rows")
    for why, n in tally.most_common():
        print(f"  - {n:>5}  {why}")
    print(f"out {len(out_rows)} unique businesses -> {args.output}")
    if args.by_city:
        n, book = write_by_city(out_rows, args.by_city)
        print(f"    workbook with {n} city tabs -> {book}")
    if args.rejects:
        print(f"    dropped rows written to {args.rejects}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
