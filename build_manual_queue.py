#!/usr/bin/env python3
"""Build the human-needed queue for the US campaign: sites whose contact form
exists but needs a person (CAPTCHA), parked for the user to submit by hand.

Two sources, limited to hosts from the US scrape (us_tech_output/raw_*.csv):
  1. forms_us.csv rows checked as has_form=captcha with a message box
  2. output/state.json entries where a live run met a CAPTCHA at the form

Excludes any host already contacted (form submitted) or already queued and
done. Writes us_tech_output/manual_captcha_queue.xlsx. Never opens tabs -
the CAPTCHA sites are only opened when the user says they are free.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
from urllib.parse import urlsplit

from openpyxl import Workbook

OUT_DIR = "us_tech_output"


def host_of(url: str) -> str:
    h = urlsplit(url).netloc.split("@")[-1].split(":")[0].lower()
    return h[4:] if h.startswith("www.") else h


def main() -> int:
    us_hosts: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(OUT_DIR, "raw_*.csv"))):
        metro = re.sub(r"^raw_|_[^_]+\.csv$", "", os.path.basename(path))
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                w = (r.get("website") or "").strip()
                if w.startswith("http"):
                    h = host_of(w)
                    if h and h not in us_hosts:
                        us_hosts[h] = {"company_name": r.get("name", ""),
                                       "category": r.get("category", "") or r.get("query", ""),
                                       "area": r.get("location", ""), "metro": metro}

    # Hosts a live run already submitted a form to - never re-approach.
    contacted: set[str] = set()
    state = {}
    if os.path.exists("output/state.json"):
        state = json.load(open("output/state.json", encoding="utf-8"))
        for url, rec in state.items():
            if not isinstance(rec, dict):
                continue
            if rec.get("method") == "form" and rec.get("status") in ("success", "uncertain"):
                contacted.add(host_of(rec.get("website") or url))

    queue: dict[str, dict] = {}

    forms_path = os.path.join(OUT_DIR, "forms_us.csv")
    if os.path.exists(forms_path):
        with open(forms_path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                h = r["host"]
                if (r["has_form"] == "captcha" and r.get("has_message") == "yes"
                        and h in us_hosts and h not in contacted):
                    queue[h] = {"website": r["website"],
                                "contact_page": r.get("contact_page", ""),
                                "captcha": r.get("captcha", ""),
                                "source": "pre-check", **us_hosts[h]}

    for url, rec in state.items():
        if not isinstance(rec, dict):
            continue
        h = host_of(rec.get("website") or url)
        if h not in us_hosts or h in contacted or h in queue:
            continue
        if "CAPTCHA present" in str(rec.get("detail", "")):
            queue[h] = {"website": rec.get("website") or url,
                        "contact_page": "", "captcha": "met during live run",
                        "source": "live run", **us_hosts[h]}

    cols = ["website", "company_name", "contact_page", "captcha", "source",
            "category", "area", "metro"]
    wb = Workbook()
    ws = wb.active
    ws.title = "needs human (CAPTCHA)"
    ws.append(cols)
    for h in sorted(queue):
        ws.append([queue[h].get(c, "") for c in cols])
    out = os.path.join(OUT_DIR, "manual_captcha_queue.xlsx")
    wb.save(out)
    print(f"{len(queue)} site(s) parked for manual submission -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
