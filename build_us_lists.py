#!/usr/bin/env python3
"""Build the send sheet(s) from the US scrape.

Keeps only hosts whose checked contact form is usable by the Contact Agent:
has_form = yes (so no CAPTCHA), has_message = yes (require_message is on).
Excludes anything ever touched in output/state.json, dedupes one row per
registrable host, interleaves metros so the list is a similar geographic mix
throughout, and writes up to --per-file rows per machine in MACHINES.

2026-10-15: the user decided to run everything on this Mac alone, so MACHINES
is just ["Macbook M1 Pro"] and the whole qualified list lands in
us_tech_output/Macbook M1 Pro.xlsx. Add the other machines back to MACHINES
to split three ways again.

Safe to re-run any time - it rebuilds from the CSVs.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import sys
from urllib.parse import urlsplit

from openpyxl import Workbook

OUT_DIR = "us_tech_output"
MACHINES = ["Macbook M1 Pro"]
PER_FILE = int(sys.argv[sys.argv.index("--per-file") + 1]) if "--per-file" in sys.argv else 45000


def host_of(url: str) -> str:
    h = urlsplit(url).netloc.split("@")[-1].split(":")[0].lower()
    return h[4:] if h.startswith("www.") else h


def contacted_hosts() -> set[str]:
    """Every host the Contact Agent has ever recorded, in any status."""
    path = os.path.join("output", "state.json")
    if not os.path.exists(path):
        return set()
    text = open(path, encoding="utf-8").read()
    return {h[4:] if h.startswith("www.") else h
            for h in re.findall(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", text.lower())}


def main() -> int:
    forms_path = os.path.join(OUT_DIR, "forms_us.csv")
    if not os.path.exists(forms_path):
        sys.exit("no forms_us.csv yet - run check_forms_us.py first")

    # host -> business details from whichever raw file saw it first
    details: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(OUT_DIR, "raw_*.csv"))):
        metro = re.sub(r"^raw_|_[^_]+\.csv$", "", os.path.basename(path))
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                w = (r.get("website") or "").strip()
                if not w.startswith("http"):
                    continue
                h = host_of(w)
                if h and h not in details:
                    details[h] = {"company_name": r.get("name", ""),
                                  "category": r.get("category", "") or r.get("query", ""),
                                  "area": r.get("location", ""), "metro": metro,
                                  "phone": r.get("phone", "")}

    skip = contacted_hosts()
    qualified: list[dict] = []
    with open(forms_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["has_form"] != "yes" or r.get("has_message") != "yes":
                continue
            h = r["host"]
            if h in skip:
                continue
            d = details.get(h, {})
            qualified.append({
                "website": r["website"], "company_name": d.get("company_name", ""),
                "category": d.get("category", ""), "area": d.get("area", ""),
                "metro": d.get("metro", ""), "phone": d.get("phone", ""),
                "contact_page": r.get("contact_page", ""),
                "form_fields": r.get("form_fields", ""),
            })

    # Interleave by metro so all three machines get a similar geographic mix.
    by_metro: dict[str, list] = {}
    for q in qualified:
        by_metro.setdefault(q["metro"], []).append(q)
    ordered: list[dict] = []
    pools = [sorted(v, key=lambda r: r["area"]) for v in by_metro.values()]
    while any(pools):
        for p in pools:
            if p:
                ordered.append(p.pop(0))

    cols = ["website", "company_name", "category", "area", "metro", "phone",
            "contact_page", "form_fields"]
    counts = []
    for i, machine in enumerate(MACHINES):
        rows = [r for j, r in enumerate(ordered) if j % len(MACHINES) == i][:PER_FILE]
        wb = Workbook()
        ws = wb.active
        ws.title = "no CAPTCHA contact forms"
        ws.append(cols)
        for r in rows:
            ws.append([r[c] for c in cols])
        out = os.path.join(OUT_DIR, f"{machine}.xlsx")
        wb.save(out)
        counts.append((machine, len(rows)))

    total_checked = sum(1 for _ in open(forms_path)) - 1
    print(f"checked {total_checked} hosts, {len(qualified)} qualify "
          f"(no-CAPTCHA form with message box, never contacted)")
    for machine, n in counts:
        print(f"  {machine}.xlsx: {n} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
