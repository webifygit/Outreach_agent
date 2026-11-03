#!/usr/bin/env python3
"""Check scraped websites for a usable contact form, and whether it is
CAPTCHA-guarded. Standalone rebuild of the form-check phase the London
pipeline had (its forms.csv columns are kept, so downstream filters match).

Reads every us_tech_output/raw_*.csv, visits each unique host once, and
appends one row per host to us_tech_output/forms_us.csv:

    host, website, status, has_form, score, has_message, captcha,
    contact_page, form_fields, site_title

    has_form: yes     - a visible enquiry form with an email or message field
              captcha - a form exists but reCAPTCHA/hCaptcha/Turnstile guards it
              no      - nothing usable found
    has_message: yes when the form has a message box (the Contact Agent's
                 require_message needs it)

Resumable: hosts already in the output are skipped. Run with --once to make a
single pass over whatever is unchecked right now, or no flag to loop forever
(used while the scrapers are still adding rows).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import os
import random
import re
import sys
import time
from urllib.parse import urljoin, urlsplit

from playwright.async_api import async_playwright

OUT_DIR = "us_tech_output"
FIELDS = ["host", "website", "status", "has_form", "score", "has_message",
          "captcha", "contact_page", "form_fields", "site_title"]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Maps sometimes lists a profile page instead of the business's own site.
THIRD_PARTY = re.compile(
    r"(^|\.)(facebook\.com|instagram\.com|linkedin\.com|twitter\.com|x\.com|"
    r"yelp\.com|google\.com|youtube\.com|tiktok\.com|pinterest\.com|"
    r"wixsite\.com|business\.site|clutch\.co|upwork\.com|fiverr\.com|"
    r"crunchbase\.com|glassdoor\.com|indeed\.com|medium\.com|linktr\.ee|"
    r"godaddysites\.com|square\.site|bit\.ly)$", re.I)

CONTACT_LINKS_JS = """() => {
  const rx = /contact|get in touch|enquir|inquir|reach (us|out)|talk to us|request a (quote|demo|consultation)|book a (call|demo)|let's talk|lets talk/i;
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    if (/^(mailto:|tel:|javascript:|#)/i.test(href)) continue;
    if (rx.test((a.innerText || '') + ' ' + href)) out.push(a.href);
  }
  return [...new Set(out)].slice(0, 6);
}"""

# Scores every visible form in one document and reports the best candidate,
# plus whether a CAPTCHA widget is anywhere on the page. Search boxes, login
# and newsletter forms are rejected the same way agent/discovery.py does.
FORM_SCAN_JS = """() => {
  const vis = el => { const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const roleText = el => ((el.name || '') + ' ' + (el.id || '') + ' ' +
    (el.placeholder || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
  let best = null;
  for (const f of document.querySelectorAll('form')) {
    if (f.querySelector('input[type=password]')) continue;           // login
    const fields = [...f.querySelectorAll('input, textarea, select')].filter(el =>
      vis(el) && !['hidden', 'submit', 'button', 'image', 'search']
        .includes((el.type || '').toLowerCase()));
    if (!fields.length) continue;
    const action = ((f.getAttribute('action') || '') + ' ' + (f.id || '') +
                    ' ' + (f.className || '')).toLowerCase();
    if (/search/.test(action) && fields.length < 3) continue;        // search box
    const roles = new Set();
    let hasMsg = false, hasEmail = false;
    for (const el of fields) {
      const r = roleText(el);
      if (el.tagName === 'TEXTAREA' || /message|comment|enquiry|inquiry|tell us|how can we help|project detail/.test(r)) {
        roles.add('message'); hasMsg = true;
      } else if ((el.type || '').toLowerCase() === 'email' || /e-?mail/.test(r)) {
        roles.add('email'); hasEmail = true;
      } else if (/first.?name/.test(r)) roles.add('first_name');
      else if (/last.?name|surname/.test(r)) roles.add('last_name');
      else if (/full.?name|your.?name|^name\\b|\\bname\\b/.test(r)) roles.add('name');
      else if (/phone|tel\\b|mobile/.test(r)) roles.add('phone');
      else if (/company|organi[sz]ation|business.?name/.test(r)) roles.add('company');
      else if (/subject/.test(r)) roles.add('subject');
      else if (/address|city|country|state|zip|postcode/.test(r)) roles.add('address');
    }
    if (!hasEmail && !hasMsg) continue;
    if (hasEmail && !hasMsg && fields.length <= 2) continue;         // newsletter
    const score = fields.length * 10 + (hasMsg ? 50 : 0) + (hasEmail ? 30 : 0) + roles.size * 8;
    if (!best || score > best.score)
      best = { score, hasMsg, hasEmail, fields: [...roles].sort() };
  }
  const capEl = document.querySelector(
    "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], " +
    ".g-recaptcha[data-sitekey], div[data-sitekey], .h-captcha, .cf-turnstile");
  let cap = '';
  if (capEl) {
    cap = capEl.tagName === 'IFRAME'
      ? "iframe[src*='" + (/recaptcha|hcaptcha|turnstile/.exec(capEl.src) || ['captcha'])[0] + "']"
      : (capEl.getAttribute('class') || 'data-sitekey').split(' ')[0];
  }
  return { best, cap, title: document.title || '' };
}"""


def host_of(url: str) -> str:
    h = urlsplit(url).netloc.split("@")[-1].split(":")[0].lower()
    return h[4:] if h.startswith("www.") else h


def gather_hosts(raw_glob: str) -> dict[str, str]:
    """host -> website URL, from every raw scrape CSV."""
    hosts: dict[str, str] = {}
    for path in sorted(glob.glob(raw_glob)):
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    w = (r.get("website") or "").strip()
                    if not w.startswith("http"):
                        continue
                    h = host_of(w)
                    if h and not THIRD_PARTY.search(h) and h not in hosts:
                        hosts[h] = w
        except Exception as exc:
            print(f"[warn] unreadable {path}: {exc}", flush=True)
    return hosts


def done_hosts(out_path: str) -> set[str]:
    if not os.path.exists(out_path):
        return set()
    with open(out_path, newline="", encoding="utf-8-sig") as f:
        return {r["host"] for r in csv.DictReader(f)}


async def scan_all_frames(page) -> dict:
    """Best form across the main document and every iframe (embedded form
    providers render inside one). A CAPTCHA seen anywhere on the page taints
    the result - that is also how the Contact Agent will treat it."""
    best, cap, title = None, "", ""
    for frame in page.frames:
        u = (frame.url or "").lower()
        if any(s in u for s in ("recaptcha", "hcaptcha", "turnstile")):
            cap = cap or "iframe[captcha]"
            continue
        if any(s in u for s in ("doubleclick", "googletag", "analytics", "facebook")):
            continue
        try:
            r = await asyncio.wait_for(frame.evaluate(FORM_SCAN_JS), timeout=12)
        except Exception:
            continue
        if frame is page.main_frame:
            title = r.get("title") or ""
        if r.get("cap"):
            cap = cap or r["cap"]
        b = r.get("best")
        if b and (not best or b["score"] > best["score"]):
            best = b
    return {"best": best, "cap": cap, "title": title}


async def check_host(ctx, host: str, website: str) -> dict:
    row = {"host": host, "website": website, "status": "ok", "has_form": "no",
           "score": "", "has_message": "", "captcha": "", "contact_page": "",
           "form_fields": "", "site_title": ""}
    page = await ctx.new_page()
    await page.route("**/*", lambda route: asyncio.ensure_future(
        route.abort() if route.request.resource_type in ("image", "media", "font")
        else route.continue_()))
    try:
        try:
            await page.goto(website, wait_until="domcontentloaded", timeout=25000)
        except Exception:
            row["status"] = "unreachable"
            return row
        await page.wait_for_timeout(2500)

        found = await scan_all_frames(page)
        row["site_title"] = (found["title"] or "")[:120]
        best, cap, where = found["best"], found["cap"], page.url

        # No message-capable form on the homepage: try the contact pages.
        if not (best and best["hasMsg"]):
            try:
                links = await page.evaluate(CONTACT_LINKS_JS)
            except Exception:
                links = []
            for guess in ("/contact", "/contact-us", "/contact.html"):
                links.append(urljoin(page.url, guess))
            seen = set()
            for url in links:
                if url in seen or host_of(url) != host:
                    continue
                seen.add(url)
                if len(seen) > 4:
                    break
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(2000)
                except Exception:
                    continue
                f2 = await scan_all_frames(page)
                b2, c2 = f2["best"], f2["cap"]
                if b2 and (not best or b2["score"] > best["score"]):
                    best, cap, where = b2, c2 or cap, page.url
                    if b2["hasMsg"]:
                        break

        if best:
            row["score"] = str(best["score"])
            row["has_message"] = "yes" if best["hasMsg"] else "no"
            row["form_fields"] = ", ".join(best["fields"])
            row["contact_page"] = where
            if cap:
                row["has_form"], row["captcha"] = "captcha", cap
            else:
                row["has_form"] = "yes"
    except Exception as exc:
        row["status"] = f"error: {str(exc)[:80]}"
    finally:
        try:
            await page.close()
        except Exception:
            pass
    return row


async def run_pass(todo: list[tuple[str, str]], out_path: str, parallel: int) -> int:
    new_file = not os.path.exists(out_path)
    out = open(out_path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out, fieldnames=FIELDS, extrasaction="ignore")
    if new_file:
        writer.writeheader(); out.flush()
    lock = asyncio.Lock()
    done = 0
    t0 = time.time()

    async with async_playwright() as pw:
        # Recycled every chunk - long checker runs leak browser memory.
        for start in range(0, len(todo), 400):
            chunk = todo[start:start + 400]
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=UA, locale="en-US",
                                            viewport={"width": 1366, "height": 900})
            sem = asyncio.Semaphore(parallel)

            async def one(host, website):
                nonlocal done
                async with sem:
                    try:
                        row = await asyncio.wait_for(check_host(ctx, host, website),
                                                     timeout=120)
                    except Exception:
                        row = {"host": host, "website": website, "status": "timeout",
                               "has_form": "no", "score": "", "has_message": "",
                               "captcha": "", "contact_page": "", "form_fields": "",
                               "site_title": ""}
                    async with lock:
                        writer.writerow(row); out.flush()
                        done += 1
                        if done % 25 == 0:
                            rate = done / max(time.time() - t0, 1) * 3600
                            print(f"  checked {done}/{len(todo)} (~{rate:.0f}/h)", flush=True)

            await asyncio.gather(*(one(h, w) for h, w in chunk))
            await browser.close()
    out.close()
    return done


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-glob", default=os.path.join(OUT_DIR, "raw_*.csv"))
    ap.add_argument("--output", default=os.path.join(OUT_DIR, "forms_us.csv"))
    ap.add_argument("--parallel", type=int, default=12)
    ap.add_argument("--once", action="store_true",
                    help="one pass over what is unchecked now, then exit")
    ap.add_argument("--limit", type=int, default=0, help="stop after N hosts (testing)")
    args = ap.parse_args()

    while True:
        hosts = gather_hosts(args.raw_glob)
        todo = [(h, w) for h, w in hosts.items() if h not in done_hosts(args.output)]
        random.shuffle(todo)
        if args.limit:
            todo = todo[:args.limit]
        if todo:
            print(f"[forms] {len(todo)} host(s) to check "
                  f"({len(hosts)} known, parallel {args.parallel})", flush=True)
            await run_pass(todo, args.output, args.parallel)
        else:
            print("[forms] nothing unchecked", flush=True)
        if args.once or args.limit:
            return 0
        if os.path.exists(os.path.join(OUT_DIR, "scrape.done")) and not todo:
            print("[forms] scraping finished and everything checked - done")
            return 0
        await asyncio.sleep(60)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
