#!/usr/bin/env python3
"""Open CAPTCHA-protected contact forms in Chrome, filled in, for a person to finish.

The agent never answers a CAPTCHA, so a campaign's sites whose only form sits
behind one are skipped by the automatic runs. This opens each of those contact
pages in a visible Chrome window with the form already filled in by the agent's
own code - same pitch, same Pest Stop identity - and waits. The person solves
the CAPTCHA and presses Submit; a confirmation (fresh "thank you" text, a
redirect, or the form disappearing) is logged and the next site opens. Closing
the tab skips a site. This script never presses submit itself.

Results go to london_hotels_output/captcha_assist_log.csv rather than
output/state.json, because a live run may be rewriting state.json at the same
moment. --merge records them there once no run is active, so later runs treat
those sites as contacted.

    python captcha_assist.py --campaign care_homes     # work through the list
    python captcha_assist.py --merge                   # record into state.json
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

import london_lists as m
from agent import discovery, filler, scripts
from agent.config import Config, default_config_path
from agent.senders import pick_sender
from agent.state import State, norm_site
from agent.templating import (context_for, greeting_for, make_env, presentable_company,
                              render_string)

LOG = Path(m.OUT) / "captcha_assist_log.csv"
PROFILE_DIR = str(Path(m.OUT) / "captcha_chrome_profile")
LOG_FIELDS = ["timestamp", "host", "website", "company_name", "contact_page",
              "status", "detail", "script"]
CAMPAIGNS = {
    "care_homes": {"script": "pest_control_care_homes", "queries": set(m.CARE),
                   "category": m.CARE_CATEGORY_RE},
}
BANNER_JS = """(text) => {
  let b = document.getElementById('__pest_stop_assist');
  if (!b) {
    b = document.createElement('div'); b.id = '__pest_stop_assist';
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
      'background:#14532d;color:#fff;font:600 14px/1.4 system-ui,sans-serif;' +
      'padding:8px 14px;text-align:center;pointer-events:none;opacity:.93';
    document.documentElement.appendChild(b);
  }
  b.textContent = text;
}"""


# Tells Python the person pressed the form's submit. Only after that does a
# page change count: sites redraw forms and load text by themselves, which
# read as "sent" for homes nobody had submitted (Everdale Grange was logged
# seconds after opening, 2026-09-29).
SUBMIT_WATCH_JS = """(key) => {
  if (window.__psWatching) return; window.__psWatching = true;
  const form = () => document.querySelector(`form[data-agent-form='${key}']`);
  const mark = () => { try { window.__psSubmitted(); } catch (e) {} };
  document.addEventListener('submit', mark, true);
  document.addEventListener('click', (e) => {
    const b = e.target.closest('button, input[type=submit], input[type=image]');
    if (!b) return;
    const f = form();
    if (f && !f.contains(b)) return;       // a button elsewhere on the page
    const t = (b.innerText || b.value || '').toLowerCase();
    if (b.type === 'submit' || b.type === 'image' || /send|submit|enquir|request|contact|register/.test(t)) mark();
  }, true);
}"""


def run_active() -> bool:
    return subprocess.run(["pgrep", "-f", "[M]acOS/Python run.py"],
                          capture_output=True).returncode == 0


def logged_hosts(done_only: bool = True) -> set[str]:
    rows = m.read_csv(str(LOG))
    return {r["host"] for r in rows if not done_only or r["status"] in ("success", "uncertain")}


def captcha_sites(campaign: dict) -> list[dict]:
    """Campaign sites whose best form is behind a CAPTCHA and nobody has reached yet."""
    state = State("output/state.json")
    wanted = m.london_hosts(campaign["queries"]) - m.lodging_hosts() - m.hosts_for(m.FOOD)
    sites = m.unique_sites()
    # Once only: a site this tool has ever opened is never offered again,
    # whatever came of it (the user, 2026-09-29: "do not approach same sites
    # again and again" - Norfolk House had come round four times).
    done = logged_hosts(done_only=False)
    out = []
    for f in m.read_csv(m.FORMS):
        h = f["host"]
        s = sites.get(h)
        if f["has_form"] != "captcha" or h not in wanted or s is None or h in done:
            continue
        if not campaign["category"].search((s.get("category") or "").strip()):
            continue
        page = f.get("contact_page") or s["website"]
        # One company, one message: skip a form whose site already carried
        # the pitch, however the company was listed (see run.py).
        if state.contacted_site(s["website"]) or state.contacted_via_form(page):
            continue
        out.append({"host": h, "website": s["website"], "company_name": s["name"],
                    "contact_page": page, "reviews": int(s.get("reviews") or 0)})
    done_forms = {norm_site(r["contact_page"], "host") for r in m.read_csv(str(LOG))}
    seen: set[str] = set()
    unique = []
    for r in sorted(out, key=lambda r: -r["reviews"]):
        key = norm_site(r["contact_page"], "host")
        if key in done_forms or key in seen:
            continue
        seen.add(key)
        unique.append(r)
    return unique


FIRST_PITCHES = {
    "pest_control_bedbugs": set(m.LODGING),
    "pest_control_restaurants": m.FOOD,
    "pest_control_care_homes": set(m.CARE),
}
ORDER = ["pest_control_bedbugs", "pest_control_restaurants", "pest_control_care_homes",
         "pest_control_care_homes_followup_1", "pest_control_letting_agents_bedbugs"]
# The letting agents the 2 Nov sends could not finish: a human check, or a form
# that wanted more than the agent could give. A person finishes them with the
# bed bug pitch, the one the user chose that day. Sites with no form to fill stay out.
LETTING = "pest_control_letting_agents"
LETTING_BB = "pest_control_letting_agents_bedbugs"
LETTING_SINCE = "2026-11-02T13:23:10"
LETTING_SHEET = Path("uploads/20261102_133500_letting_agents_rest_of_london.xlsx")
HAND_RE = re.compile(r"captcha|human|altcha|cleantalk|numeric verification|submitted too quickly|"
                     r"form incomplete|validation error|no_submit_control", re.I)


def letting_bedbugs(state, reached: set[str]) -> list[dict]:
    import openpyxl
    from agent.state import CONTACTED_STATUSES
    rows = list(openpyxl.load_workbook(LETTING_SHEET, read_only=True).worksheets[0].iter_rows(values_only=True))
    col = rows[0].index("contact_page")
    pages = {r[0]: r[col] for r in rows[1:] if r[0]}
    out = []
    for u, e in state.data.items():
        ts = [t for t in (e.get("touches") or [e]) if t.get("script") in (LETTING, LETTING_BB)]
        recent = [t for t in ts if str(t.get("timestamp", "")) >= LETTING_SINCE]
        if not recent or any(t.get("status") in CONTACTED_STATUSES for t in ts):
            continue
        if not HAND_RE.search(str(recent[-1].get("detail", ""))):
            continue
        page = recent[-1].get("contact_page") or u
        if not urlsplit(page).path.strip("/"):          # the scrape knows where its form is
            page = pages.get(u) or page
        if norm_site(page, "host") in reached:
            continue
        out.append({"script": LETTING_BB, "page": page, "host": m.host_of(u), "website": u,
                    "company_name": e.get("company_name", "") or m.host_of(u), "contact_page": page})
    return out


def pending_sites() -> list[dict]:
    """What is left to do by hand, each with the pitch it needs.

    - businesses whose only first-pitch attempt was stopped by a CAPTCHA that
      appeared after Submit (status captcha_after_submit) - nothing reached them;
    - care homes that got the first pitch by hand and still need the follow-up
      (a CAPTCHA site, so the automatic follow-up could not reach them).
    Once only: a (site, pitch) this tool already handled is not offered again,
    nor a company another of whose websites already got that pitch.
    """
    from agent.state import CONTACTED_STATUSES
    state = State("output/state.json")
    # never handled by a person, or the form would not submit: offer again
    handled = {(r["host"], r["script"]) for r in m.read_csv(str(LOG))
               if r["status"] not in ("timeout", "not_submitted")}
    london = {s: m.london_hosts(q) for s, q in FIRST_PITCHES.items()}
    reached_forms: dict[str, set[str]] = {}
    for u, e in state.data.items():
        for t in (e.get("touches") or [e]):
            if t.get("status") in CONTACTED_STATUSES and t.get("contact_page"):
                reached_forms.setdefault(t.get("script", ""), set()).add(norm_site(t["contact_page"], "host"))
    out = []
    for u, e in state.data.items():
        h = m.host_of(u)
        if m.NOT_OWN_SITE.search(h):
            continue
        ts = e.get("touches") or [e]
        for first in FIRST_PITCHES:
            mine = [t for t in ts if t.get("script") == first]
            if not mine or h not in london[first]:
                continue
            got = [t for t in mine if t.get("status") in CONTACTED_STATUSES]
            if not got:
                blocked = [t for t in mine if t.get("status") == "captcha_after_submit"]
                if blocked:
                    out.append({"script": first, "page": blocked[-1].get("contact_page") or u})
            elif first == "pest_control_care_homes" and any("by hand" in str(t.get("detail", "")) for t in got):
                fu = first + "_followup_1"
                if not any(t.get("script") == fu for t in ts):
                    out.append({"script": fu, "page": got[-1].get("contact_page") or u})
            for item in out[-1:]:
                if item.get("host") is None and item["page"] and item["script"] in (first, first + "_followup_1"):
                    item.update(host=h, website=u, company_name=e.get("company_name", "") or h,
                                contact_page=item["page"])
    out += letting_bedbugs(state, {f for k, v in reached_forms.items() if k.startswith("pest_control_") for f in v})
    seen, unique = set(), []
    for r in sorted(out, key=lambda r: ORDER.index(r["script"])):
        form = norm_site(r["contact_page"], "host")
        key = (r["script"], form)
        if (r["host"], r["script"]) in handled or key in seen or form in reached_forms.get(r["script"], set()):
            continue
        seen.add(key)
        unique.append(r)
    return unique


def append_log(row: dict) -> None:
    new = not LOG.exists()
    with open(LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


async def fresh_success(page, frame, before_body: str) -> bool:
    body = await filler.read_context_body(page, frame)
    for hit in filler.SUCCESS_TEXT_RE.finditer(body):
        probe = body[max(0, hit.start() - 20): hit.end() + 20]
        if probe not in before_body:
            return True
    return False


ANCHOR = {"page": None}
# Tabs are created one at a time: with several in flight, expect_page() handed
# the same new tab to two sites and the whole window crashed (2026-10-02).
# (created inside the running loop - on Python 3.9 a Lock binds to the loop
# that exists when it is made)
TAB_LOCK = {"lock": None}


async def open_tab(context):
    """A new tab that does not come to the front or take focus.

    Playwright's new_page() activates Chrome, which pulled the user out of the
    window they were working in every few seconds (2026-09-30). A target
    created with background=True opens behind, like a middle-click.
    """
    if TAB_LOCK["lock"] is None:
        TAB_LOCK["lock"] = asyncio.Lock()
    async with TAB_LOCK["lock"]:
        return await _open_tab(context)


async def _open_tab(context):
    anchor = ANCHOR["page"]
    if anchor is None or anchor.is_closed():
        return await context.new_page()
    cdp = await context.new_cdp_session(anchor)
    try:
        async with context.expect_page() as info:
            await cdp.send("Target.createTarget", {"url": "about:blank", "background": True})
        return await info.value
    finally:
        try:
            await cdp.detach()
        except Exception:
            pass


async def assist_one(context, cfg, env, site: dict, n: int, total: int, wait_s: int) -> dict:
    row = {"timestamp": datetime.now().isoformat(timespec="seconds"), "host": site["host"],
           "website": site["website"], "company_name": site["company_name"],
           "contact_page": site["contact_page"], "script": cfg["_script_key"]}
    page = await open_tab(context)
    clicked = {"at": 0.0}
    await page.expose_binding("__psSubmitted", lambda source: clicked.__setitem__("at", clicked["at"] or asyncio.get_event_loop().time()))
    try:
        await page.goto(site["contact_page"], wait_until="domcontentloaded", timeout=30000)
        await discovery.settle(page, 2500)
        async def prepare():
            """Find the form and fill it. None when the page has no usable form."""
            chosen = None
            for score, form, frame in discovery.rank_forms(await discovery.forms_everywhere(page)):
                if score >= 40 and "message" in filler.classify(form["fields"]):
                    chosen = (form, frame)
                    break
            if chosen is None:
                return None
            form, frame = chosen
            sender = pick_sender(cfg, n, "form_senders")
            ctx = context_for(cfg, {"website": site["website"], "company_name": site["company_name"]}, sender)
            scraped = await discovery.site_name(page)
            if scraped and presentable_company(scraped):
                ctx["company_name"] = scraped
            ctx["greeting_name"] = greeting_for(ctx.get("company_name", ""), "")
            subject = render_string(env, str(cfg.path("form", "subject_line", default="Enquiry")), ctx)
            await filler.fill_form(frame, form, cfg, ctx, env, subject)
            key = form["form_key"]
            try:
                await frame.locator(f"form[data-agent-form='{key}']").scroll_into_view_if_needed(timeout=4000)
            except Exception:
                pass
            try:
                await frame.evaluate(SUBMIT_WATCH_JS, key)
            except Exception:
                pass
            await page.evaluate(BANNER_JS, f"Pest Stop outreach {n}/{total} - {site['company_name']}: "
                                "form filled. Solve the CAPTCHA and press Submit. Close this tab to skip.")
            # Never page.bring_to_front(): the user works in other windows and
            # asked not to have tabs pushed at them (2026-09-30).
            return form, frame, key

        ready = await prepare()
        if ready is None:
            row.update(status="no_form", detail="no usable contact form on the page now")
            return row
        form, frame, key = ready
        msg_id = filler.classify(form["fields"])["message"]["agent_id"]
        print(f"[{n}/{total}] {site['company_name']} - filled, tab ready", flush=True)
        refills = 0

        before_url = page.url
        before_body = await filler.read_context_body(page, frame)
        waiting_after_error = False
        for _ in range(wait_s // 2):
            await asyncio.sleep(2)
            if page.is_closed():
                if clicked["at"]:
                    row.update(status="uncertain", detail="Submit was pressed, then the tab was closed before a confirmation showed")
                else:
                    row.update(status="skipped", detail="tab closed - not submitted")
                return row
            if not clicked["at"]:
                # Chrome's Memory Saver unloads tabs nobody is looking at and
                # reloads them empty when clicked - carebase.org.uk showed a
                # blank form, 2026-09-30. A reload before Submit: fill it again.
                # Wix rebuilds its message box a few seconds after load and drops
                # what was typed (hotellily.co.uk, haydennottinghill.com,
                # 2026-10-02): an empty or replaced message box counts as lost.
                try:
                    lost = await frame.locator(f"form[data-agent-form='{key}']").count() == 0
                    if not lost:
                        msg_box = frame.locator(f"[data-agent-id='{msg_id}']")
                        lost = (await msg_box.count() == 0
                                or not (await msg_box.input_value(timeout=2000)).strip())
                except Exception:
                    lost = True
                if lost and refills < 30:
                    try:
                        await discovery.settle(page, 1500)
                        again = await prepare()
                    except Exception:
                        again = None
                    if again is not None:
                        form, frame, key = again
                        msg_id = filler.classify(form["fields"])["message"]["agent_id"]
                        refills += 1
                        before_url = page.url
                        before_body = await filler.read_context_body(page, frame)
                        print(f"    [{n}] {site['company_name']} - page reloaded, filled again", flush=True)
                continue                      # nothing counts until the person presses Submit
            try:
                moved = page.url != before_url
                gone = await frame.locator(f"form[data-agent-form='{key}']").count() == 0
                thanked = await fresh_success(page, frame, before_body)
            except Exception:
                moved, gone, thanked = True, True, False     # frame replaced by a new page
            if (moved or gone or thanked) and not waiting_after_error or (waiting_after_error and (moved or thanked)):
                await asyncio.sleep(2)
                try:
                    status, detail, _ = await filler.verify_submission(page, frame, before_url, key, before_body)
                except Exception:
                    status, detail = "uncertain", "page changed after submit"
                if status == "failed" and "CAPTCHA appeared after submit" in detail:
                    # Shown only once Submit is pressed (Wix and others). Solving
                    # it sends the form by itself - keep watching for the thanks.
                    if not waiting_after_error:
                        print(f"    [{n}] CAPTCHA after Submit - waiting for it to be solved", flush=True)
                        try:
                            await page.evaluate(BANNER_JS, "Solve the CAPTCHA - the form sends itself once it is "
                                                "solved (press Submit again if it does not).")
                        except Exception:
                            pass
                    waiting_after_error = True          # from here only thanks/redirect count
                    continue
                if status == "failed":
                    # The site refused it (CAPTCHA expired, a box it wanted, ...).
                    # Leave the tab for the person to fix and press Submit again -
                    # moving on here recorded Norfolk House as sent, 2026-09-29.
                    print(f"    site showed an error - tab left open to fix and resubmit ({detail[:80]})", flush=True)
                    try:
                        await page.evaluate(BANNER_JS, "The site showed an error - fix it (e.g. redo the CAPTCHA) "
                                            "and press Submit again. Close this tab to skip.")
                        before_body = await filler.read_context_body(page, frame)
                    except Exception:
                        pass
                    before_url = page.url
                    waiting_after_error = True
                    clicked["at"] = 0.0          # wait for the next Submit press
                    continue
                row.update(status=status, detail="submitted by hand after solving the CAPTCHA; " + detail)
                return row
        row.update(status="timeout", detail=f"not submitted within {wait_s // 60} minutes")
        return row
    except Exception as e:
        row.update(status="error", detail=f"{type(e).__name__}: {str(e)[:120]}")
        return row
    finally:
        try:
            if not page.is_closed():
                await asyncio.sleep(1.5)
                await page.close()
        except Exception:
            pass


def cfg_for(script_key: str):
    cfg = Config.load(default_config_path("."))
    key, _ = scripts.apply(cfg, script_key)
    cfg["_script_key"] = key
    return cfg


async def assist(campaign_name: str, wait_s: int, at_once: int = 15) -> None:
    env = make_env(Path("."))
    if campaign_name == "pending":
        sites = pending_sites()
    else:
        sites = [dict(x, script=CAMPAIGNS[campaign_name]["script"])
                 for x in captcha_sites(CAMPAIGNS[campaign_name])]
    cfgs = {k: cfg_for(k) for k in {x["script"] for x in sites}}
    print(f"{len(sites)} sites to do by hand ({at_once} tabs open at a time)", flush=True)
    # A fixed number of tabs at a time: 174 at once would freeze Chrome and
    # Memory Saver would unload most of them anyway. A finished tab makes room
    # for the next, opened in the background.
    room = asyncio.Semaphore(at_once)
    counts: dict[str, int] = {}
    # All tabs at once, in one window of one Chrome profile of its own - started
    # only when the user says they are free, so nothing appears while they
    # work elsewhere (2026-09-30). The profile keeps cookie choices between
    # sessions.
    async def one(n: int, site: dict) -> None:
        async with room:
            await asyncio.sleep(1.5)                # tabs open behind, a moment apart
            try:
                row = await assist_one(context, cfgs[site["script"]], env, site, n, len(sites), wait_s)
            except Exception as e:                  # one tab's trouble never closes the window
                row = {"timestamp": datetime.now().isoformat(timespec="seconds"), "host": site["host"],
                       "website": site["website"], "company_name": site["company_name"],
                       "contact_page": site["contact_page"], "script": site["script"],
                       "status": "error", "detail": f"{type(e).__name__}: {str(e)[:100]}"}
        append_log(row)
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        print(f"    [{n}] {site['company_name']} ({site['script']}) -> {row['status']}: {row['detail'][:80]}", flush=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            PROFILE_DIR, channel="chrome", headless=False, no_viewport=True, locale="en-GB",
            args=["--disable-blink-features=AutomationControlled"])
        ANCHOR["page"] = context.pages[0] if context.pages else await context.new_page()
        try:
            await ANCHOR["page"].set_content(
                "<h2 style='font-family:system-ui;margin:40px'>Pest Stop CAPTCHA tabs</h2>"
                "<p style='font-family:system-ui;margin:0 40px'>Each tab to the right is a filled-in form. "
                "Solve its CAPTCHA and press Submit; close a tab to skip it.</p>")
        except Exception:
            pass
        await asyncio.gather(*(one(n, site) for n, site in enumerate(sites, 1)))
        await context.close()
    print("done:", counts, flush=True)


def merge() -> None:
    if run_active():
        sys.exit("a run is writing state.json right now - merge again when it has finished")
    state = State("output/state.json")
    added = 0
    for r in m.read_csv(str(LOG)):
        if r["status"] not in ("success", "uncertain"):
            continue
        entry = state.data.get(r["website"]) or {}
        if any(t.get("script") == r["script"] and t.get("status") in ("success", "uncertain", "sent")
               for t in (entry.get("touches") or [entry] if entry else [])):
            continue                                # this pitch is already on record
        state.record(r["website"], {
            "row_index": 0, "website": r["website"], "company_name": r["company_name"],
            "method": "form", "status": r["status"], "detail": r["detail"],
            "contact_page": r["contact_page"], "email_used": "", "sender": "Pest Stop Control Team",
            "sender_email": "contact@peststopcontrol.com", "screenshot_before": "",
            "screenshot_after": "", "timestamp": r["timestamp"], "script": r["script"],
            "follow_up": r["script"].endswith("_followup_1")})
        added += 1
    print(f"recorded {added} hand-submitted site(s) in output/state.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", choices=sorted(CAMPAIGNS) + ["pending"])
    ap.add_argument("--wait", type=int, default=4 * 3600, help="seconds a tab stays open for you")
    ap.add_argument("--merge", action="store_true")
    args = ap.parse_args()
    if args.merge:
        merge(); return 0
    if not args.campaign:
        ap.error("give --campaign or --merge")
    asyncio.run(assist(args.campaign, args.wait))
    return 0


if __name__ == "__main__":
    sys.exit(main())
