#!/usr/bin/env python3
"""Check a sheet before uploading it. Sends nothing, opens no browser.

    .venv/bin/python preflight.py uploads/batch7.csv

Reports what the agent will actually do with the file: how many rows survive,
what gets silently dropped, and which rows are already in the contacted history.
"""
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from agent.sheet import load_rows          # noqa: E402
from agent.state import norm_site          # noqa: E402

# Hosts that are somebody's page, not somebody's website. The agent has no
# filter for these: the first one claims the host and every later row on the
# same host is dropped as a duplicate, so 50 businesses become 1 attempt.
SOCIAL = {
    "facebook.com", "m.facebook.com", "web.facebook.com", "fb.com",
    "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "pinterest.com", "wa.me", "api.whatsapp.com",
    "sites.google.com", "business.site", "wixsite.com", "blogspot.com",
    "yellowpages.co.za", "brabys.com", "snupit.co.za", "hotfrog.co.za",
}

STATE = Path("output/state.json")


# Builder platforms where each business gets its OWN subdomain
# (shirtsalone.business.site). Distinct hosts, so dedup keeps them apart and
# the agent contacts them normally - reported for visibility, not removal.
PLATFORM_SUFFIXES = ("business.site", "wixsite.com", "blogspot.com",
                     "weebly.com", "webnode.com", "godaddysites.com",
                     "square.site", "myshopify.com")


def bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def platform_of(host: str) -> str:
    return next((suf for suf in PLATFORM_SUFFIXES if host.endswith(suf)), "")


def main(path: str) -> int:
    try:
        rows, skipped = load_rows(path)
    except Exception as exc:
        print(f"FATAL  {exc}")
        return 2

    seen: dict[str, dict] = {}
    contacted: set[str] = set()
    if STATE.exists():
        data = json.loads(STATE.read_text(encoding="utf-8"))
        contacted = {norm_site(u, "host") for u in data}

    dupes, social, platform, already, usable = [], [], [], [], []
    for r in rows:
        key = norm_site(r["website"], "host")
        host = bare(urlparse(r["website"]).netloc.lower())
        if platform_of(host):
            # own subdomain per business - a real, contactable site
            platform.append(r)
        if host in SOCIAL:
            social.append(r)
        elif key in seen:
            dupes.append((r, seen[key]))
        elif key in contacted:
            already.append(r)
            seen[key] = r
        else:
            usable.append(r)
            seen[key] = r

    total = len(rows) + len(skipped)
    print(f"file          {path}")
    print(f"rows read     {total}")
    print()
    print(f"  WILL CONTACT      {len(usable):4d}")
    print(f"  already contacted {len(already):4d}  (skipped automatically)")
    print(f"  duplicate host    {len(dupes):4d}  (first of each kept)")
    print(f"  social/directory  {len(social):4d}" +
          ("  <-- REMOVE THESE" if social else ""))
    print(f"  builder platform  {len(platform):4d}  (fine - own subdomain each)")
    print(f"  unusable URL      {len(skipped):4d}")

    if social:
        by_host = Counter(bare(urlparse(r["website"]).netloc.lower()) for r in social)
        print("\nsocial/directory hosts - these share ONE host, so dedup keeps only")
        print("the first and silently drops the rest:")
        for host, n in by_host.most_common(10):
            print(f"    {host:32s} {n:4d} row(s)")
    if skipped:
        print("\nunusable website values:")
        for s in skipped[:10]:
            print(f"    row {s['row_index']:5d}  {s['website']!r}")
        if len(skipped) > 10:
            print(f"    and {len(skipped) - 10} more")

    est = len(usable) / 174
    print(f"\nestimated run time  {est:.1f} h at ~174 sites/hr (workers=6, typical batch)")
    if not usable:
        print("\nNOTHING TO SEND - do not upload this sheet.")
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
