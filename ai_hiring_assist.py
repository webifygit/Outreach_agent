#!/usr/bin/env python3
"""Open the AI-hiring list's hand-only contact forms in Chrome, filled in, 10 at a time.

The 2026-10-29 test run of ai_hiring_targets/AI hiring targets - 50 US companies.xlsx
could not send to 37 of its 48 companies: a CAPTCHA, a form that only renders in a
real browser, or a demo page the agent never reached from the homepage. This opens
each one's researched contact page in the CAPTCHA tool's own Chrome profile and
fills it with the webify_hire_developers_targeted pitch (the row's own opener,
Irshad/Tarannum alternating as in run.py), leaving only the CAPTCHA and Submit to
the person. It never presses Submit.

Only filled forms stay open: the user will not copy and paste (2026-10-30), so a
page whose form the agent cannot fill is closed again and logged not_filled.
Up to ten filled tabs are open at a time, behind the current one; each one
submitted or closed makes room for the next. The first tab shows how each
company is getting on.

Results go to ai_hiring_targets/manual_log.csv and are recorded in
output/state.json at the end (or with --merge, if a run was writing it then).

    python ai_hiring_assist.py           # open the tabs
    python ai_hiring_assist.py --merge   # record hand-submitted forms in state.json
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import html
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from agent import discovery, filler, scripts
from agent.config import Config, default_config_path
from agent.senders import pick_sender
from agent.sheet import load_rows
from agent.state import CONTACTED_STATUSES, State, norm_site
from agent.templating import context_for, make_env, render_string

SCRIPT = "webify_hire_developers_targeted"
SHEET = Path("ai_hiring_targets/AI hiring targets - 50 US companies.xlsx")
LOG = Path("ai_hiring_targets/manual_log.csv")
LOG_FIELDS = ["timestamp", "website", "company_name", "contact_page", "status", "detail",
              "sender", "sender_email"]
PROFILE_DIR = str(Path("london_hotels_output/captcha_chrome_profile").resolve())
BATCH = 10
# What US demo forms ask for and config.local.yaml leaves blank. Titles given by
# the user, 2026-10-30. The number is the one config holds, with India's code
# in front - without it a US form reads it as a US number.
SENDER_EXTRA = {
    "irshad": {"designation": "Founder & CEO"},
    "tarannum": {"designation": "Business Development Manager"},
}
SENDER_COMMON = {"country": "India", "city": "Ahmedabad", "phone": "+91 9772544444"}
# Not handed to a person yet: offered again the next time this runs.
RETRY_STATUSES = ("timeout", "error", "not_filled")

# Roles whose values are facts about the sender. Everything else on a demo form
# asks about us as a buyer (industry, budget, patients, timeline), and
# filler.fill_form answers those with the first option - "Industry: Private
# Equity" on Hebbia's form, "Company type: Pharmaceutical" on Wheel's,
# 2026-10-30. The person here only solves the CAPTCHA, so nothing untrue may be
# filled in for them to send.
TRUE_ROLES = {"message", "name", "first_name", "last_name", "email", "email_confirm",
              "phone", "company", "website", "designation", "country", "city", "subject"}
# (question, options that are true of Webify.ai, best first) - an Indian AI
# development company of 10-50 people, writing to offer its engineers, which
# found these companies through online search.
SIZE_Q = re.compile(r"company size|firm size|employees|employee count|team size|headcount|"
                    r"how many (people|employees)|organi[sz]ation size", re.I)
HEARD_Q = re.compile(r"hear about|find us|how did you (hear|find|learn)|heard_about|lead source", re.I)
TRUE_ANSWERS = [
    (re.compile(r"industry|company type|type of (company|business|organi[sz]ation)|business type|sector|vertical", re.I),
     [re.compile(r"software|information technology|\bit\b|technology|saas|computer|internet|consult", re.I)]),
    (re.compile(r"country", re.I), [re.compile(r"^\s*india\s*$", re.I)]),
    (HEARD_Q, [re.compile(r"search engine|online search|\bsearch\b|internet|\bweb\b|online", re.I)]),
    (re.compile(r"inquiry|enquiry|reason|topic|interest|help you|can we help|best describes|request type|"
                r"type of request|i am a|i'm a|you are a", re.I),
     [re.compile(r"partner|vendor|supplier|business development|service provider", re.I),
      re.compile(r"general|something else", re.I)]),
]
# Job level and job function: one answer per sender, from the titles the user gave.
LEVEL_Q = re.compile(r"job level|seniority|role level|management level|job title|title", re.I)
FUNCTION_Q = re.compile(r"job function|function|department", re.I)
LEVEL_ANSWER = {
    "irshad": re.compile(r"c-?level|c-suite|executive|founder|owner|ceo|president", re.I),
    "tarannum": re.compile(r"\bmanager\b", re.I),
}
FUNCTION_ANSWER = {
    "irshad": re.compile(r"executive|c-?level|c-suite|owner|founder|leadership|general management", re.I),
    "tarannum": re.compile(r"sales|business development", re.I),
}
# What a person would type into a "How did you hear about us?" box.
HEARD_TEXT = "Online search"
CHOICE_SKIP = re.compile(r"\bother\b|please specify", re.I)


def covers_us(text: str) -> bool:
    """Is this company-size option true of a 10-50 person company?"""
    if "+" in text:
        return False
    nums = [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", text)]
    if len(nums) == 2:
        return nums[0] <= 11 and nums[1] >= 49
    if len(nums) == 1 and re.search(r"under|less than|fewer than|<|up to", text, re.I):
        return nums[0] >= 50
    return False

BANNER_JS = """(text) => {
  let b = document.getElementById('__webify_assist');
  if (!b) {
    b = document.createElement('div'); b.id = '__webify_assist';
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
      'background:#1e3a8a;color:#fff;font:600 14px/1.4 system-ui,sans-serif;' +
      'padding:8px 14px;text-align:center;pointer-events:none;opacity:.93';
    document.documentElement.appendChild(b);
  }
  b.textContent = text;
}"""

# Installed in every frame before the page loads: a hosted form (HubSpot,
# Marketo) lives in an iframe of its own, and its Submit never reaches the top
# page. Only after a press does a page change count - sites redraw forms by
# themselves (captcha_assist, 2026-09-29). add_init_script runs this source
# as it stands, so it calls itself: written as a bare arrow function it was
# only ever defined, and five Submit presses went unseen (2026-10-30).
WATCH_JS = """(() => {
  if (window.__wbWatching) return; window.__wbWatching = true;
  const mark = () => { try { window.__wbSubmitted(); } catch (e) {} };
  document.addEventListener('submit', mark, true);
  document.addEventListener('click', (e) => {
    const b = e.target.closest && e.target.closest('button, input[type=submit], input[type=image]');
    if (!b) return;
    const t = (b.innerText || b.value || '').toLowerCase();
    if (b.type === 'submit' || b.type === 'image' || /send|submit|request|contact|talk|book|schedule|get started/.test(t)) mark();
  }, true);
})();"""


def usable(form: dict) -> bool:
    """A visible form with a message box, read more leniently than run.py does.

    discovery.score_form rejects any form whose button says "Book": it guards
    unattended runs against restaurant table bookings. Here a person reads every
    form before pressing Submit, and a B2B demo form's button is "Book a call"
    (mintlify.com, 2026-10-30), so only forms that publish, log in or search
    stay out.
    """
    if not form["visible"] or "message" not in filler.classify(form["fields"]):
        return False
    if discovery.score_form(form) >= 40:
        return True
    types = {f["type"] for f in form["fields"]}
    return ("password" not in types
            and not discovery.PUBLISHING_FORM_RE.search(form["desc"] + " " + form["submit_text"])
            and not re.search(r"\bsearch\b", form["desc"]))


def true_option(field: dict, sender: dict) -> dict | None:
    """The option of a select that is true of the sender, if one is."""
    question = field["desc"] or ""
    opts = [o for o in field["options"] if o["value"] and o["value"] not in {"0", "-1"}
            and not CHOICE_SKIP.search(o["text"] or "")]
    if SIZE_Q.search(question):
        return next((o for o in opts if covers_us(o["text"] or "")), None)
    for ask, answers in TRUE_ANSWERS:
        if ask.search(question):
            for rx in answers:
                hit = next((o for o in opts if rx.search(o["text"] or "")), None)
                if hit:
                    return hit
            return None
    key = sender.get("key", "")
    for ask, answer in ((FUNCTION_Q, FUNCTION_ANSWER.get(key)), (LEVEL_Q, LEVEL_ANSWER.get(key))):
        if answer and ask.search(question):
            return next((o for o in opts if answer.search(o["text"] or "")), None)
    return None


async def true_fill(frame, form: dict, cfg, ctx: dict, env, subject: str, sender: dict) -> list[str]:
    """Fill only what is true of the sender. Returns the fields the form still refuses.

    filler.fill_form's own passes, less every guess: no first-option selects,
    radios or "required?" text, and selects only where an option says the truth.
    """
    roles = filler.classify(form["fields"])
    msg = roles.get("message")
    message = filler.render_tiered_message(env, cfg, ctx, msg.get("maxlength") if msg else None)
    values = filler.values_for(cfg, ctx, message, subject)
    if "first_name" in roles and "last_name" not in roles and "name" not in roles:
        values["first_name"] = values["name"] or values["first_name"]
    done: set[str] = set()
    typed: dict[str, str] = {}
    for role, field in roles.items():
        value = values.get(role, "")
        if role not in TRUE_ROLES or not value:
            continue
        sel = f"[data-agent-id='{field['agent_id']}']"
        if field["tag"] == "select":
            pick = next((o for o in field["options"]
                         if o["value"] and value.lower() in (o["text"] or "").lower()), None) or true_option(field, sender)
            if pick and await filler._set_value(frame, sel, pick["value"], is_select=True):
                done.add(field["agent_id"])
        elif await filler._set_value(frame, sel, value, is_select=False):
            done.add(field["agent_id"])
            typed[sel] = value
    for field in form["fields"]:
        if field["tag"] != "select" or not filler.fillable(field) or field["agent_id"] in done:
            continue
        pick = true_option(field, sender)
        if pick:
            await filler._set_value(frame, f"[data-agent-id='{field['agent_id']}']", pick["value"], is_select=True)
    # Required boxes our own words answer: "How did you hear about us?", and a
    # second message box some forms insist on ("Inquiry Details" beside cresta.com's
    # first one, 2026-10-30).
    for field in form["fields"]:
        if (not field["visible"] or field["agent_id"] in done or field["tag"] == "select"
                or field["type"] in {"checkbox", "radio", "hidden", "file", "submit"}
                or not (field["required"] or "*" in (field["desc"] or ""))):
            continue
        if field["tag"] == "textarea":
            text = message
        elif HEARD_Q.search(field["desc"] or ""):
            text = HEARD_TEXT
        else:
            continue
        sel = f"[data-agent-id='{field['agent_id']}']"
        try:
            if not (await frame.locator(sel).input_value(timeout=1500)).strip():
                await filler._set_value(frame, sel, text, is_select=False)
        except Exception:
            pass
    # Required privacy/terms boxes only, as accept_consent_checkboxes lets run.py; never an opt-in.
    if cfg.path("form", "accept_consent_checkboxes", default=True):
        for field in form["fields"]:
            if (field["type"] == "checkbox" and not field.get("checked") and field["required"]
                    and filler.CONSENT_RE.search(field["desc"])
                    and not filler.MARKETING_OPTIN_RE.search(field["desc"])):
                await filler._tick(frame, f"[data-agent-id='{field['agent_id']}']", field["visible"])
    # A value set without keystrokes can leave the site's own check unsatisfied:
    # type it again, and a refused phone number the other ways (as fill_form does).
    phone_ids = {f["agent_id"] for f in form["fields"] if f.get("type") == "tel" or f is roles.get("phone")}
    for agent_id in await filler._invalid_with_value(frame, form["form_key"]):
        loc = frame.locator(f"[data-agent-id='{agent_id}']")
        try:
            value = await loc.input_value(timeout=2000)
            tries = [value]
            if agent_id in phone_ids and values.get("phone"):
                tries += filler.phone_variants(values["phone"])
            for attempt in tries:
                await loc.fill("", timeout=3000)
                await loc.press_sequentially(attempt, timeout=60000)
                await loc.press("Tab", timeout=2000)
                if agent_id not in await filler._invalid_with_value(frame, form["form_key"]):
                    break
        except Exception:
            continue
    # Some forms look the company up from the work email and write it in a
    # moment later, on top of ours - mintlify.com ended with "WebifyWebify.ai",
    # 2026-10-30. Give them that moment, then put back anything that changed.
    await asyncio.sleep(1.5)
    for sel, value in typed.items():
        try:
            if (await frame.locator(sel).input_value(timeout=1500)) != value:
                await frame.locator(sel).fill(value, timeout=5000)
        except Exception:
            pass
    missing = await filler.invalid_fields(frame, form["form_key"])
    # Sites that check required fields in their own script mark them only with a
    # "*" in the label (wheel.com, 2026-10-30): left empty, the form would just be
    # refused when the person presses Submit.
    for field in form["fields"]:
        if not field["visible"] or field["type"] in {"checkbox", "radio", "hidden", "file", "submit"}:
            continue
        if "*" not in (field["desc"] or "") and not field["required"]:
            continue
        try:
            value = await frame.locator(f"[data-agent-id='{field['agent_id']}']").input_value(timeout=1500)
        except Exception:
            continue
        if not value.strip():
            missing.append(f"{(field['desc'] or field['type'])[:50]} (empty)")
    return missing


def run_active() -> bool:
    return subprocess.run(["pgrep", "-f", "[M]acOS/Python run.py"],
                          capture_output=True).returncode == 0


def read_log() -> list[dict]:
    if not LOG.exists():
        return []
    with open(LOG, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def append_log(row: dict) -> None:
    new = not LOG.exists()
    with open(LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def pending_sites() -> list[dict]:
    """The list's rows still to send, each offered once only.

    The rows the test run could fill (dry_run) come first and go through here
    too, not run.py: its filler guesses, and here a person presses Submit. Left
    out: any company another approach already reached
    (never-approach-same-site-twice), and any row this tool already handled -
    except a tab that timed out, broke or could not be filled.
    """
    state = State("output/state.json")
    rows, _ = load_rows(SHEET)
    handled = {norm_site(r["website"]) for r in read_log() if r["status"] not in RETRY_STATUSES}
    out = []
    for row in rows:
        entry = state.get(row["website"])
        mine = [t for t in ((entry.get("touches") or [entry]) if entry else []) if t.get("script") == SCRIPT]
        if not mine:
            continue
        if any(t.get("status") in CONTACTED_STATUSES for t in mine):
            continue
        page = str(row.get("contact_page_url") or row["website"])
        if state.contacted_site(row["website"]) or state.contacted_via_form(page):
            continue
        if norm_site(row["website"]) in handled:
            continue
        out.append(dict(row, contact_page=page, _fillable=mine[-1].get("status") == "dry_run"))
    return sorted(out, key=lambda r: not r["_fillable"])


def cfg_for_script():
    cfg = Config.load(default_config_path("."))
    scripts.apply(cfg, SCRIPT)
    return cfg


def sender_for(cfg, site: dict) -> dict:
    sender = pick_sender(cfg, site["row_index"], "form_senders")
    return {**sender, **SENDER_COMMON, **SENDER_EXTRA.get(sender.get("key", ""), {})}


def anchor_html(sites: list[dict]) -> str:
    """The first tab: what to do, and how each company is getting on."""
    rows = "".join(
        f"<tr><td>{n}</td><td>{html.escape(s['company_name'])}</td>"
        f"<td id='s{n}'>waiting</td></tr>" for n, s in enumerate(sites, 1))
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>Webify AI hiring - forms</title>
<style>body{{font:15px/1.45 system-ui,sans-serif;margin:24px 40px;color:#1f2937;background:#f8fafc}}
table{{border-collapse:collapse}} td{{border-bottom:1px solid #d1d5db;padding:6px 14px}}</style>
</head><body><h2>Webify AI hiring: {len(sites)} contact forms</h2>
<p>Filled-in forms open in the tabs to the right, up to {BATCH} at a time. In each: solve the CAPTCHA
if there is one and press <b>Submit</b>. Close a tab to skip it; each one done makes room for the next.
Forms the agent cannot fill completely are not opened.</p>
<table>{rows}</table></body></html>"""


ANCHOR = {"page": None}
# One tab created at a time: with several in flight, expect_page() handed one
# new tab to two sites and the window crashed (captcha_assist, 2026-10-02).
TAB_LOCK = {"lock": None}


async def open_tab(context):
    """A new tab that opens behind, like a middle-click, never taking focus.

    Playwright's new_page() brings Chrome to the front, which pulled the user
    out of the window they were working in (captcha_assist, 2026-09-30).
    """
    if TAB_LOCK["lock"] is None:
        TAB_LOCK["lock"] = asyncio.Lock()
    async with TAB_LOCK["lock"]:
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


async def page_text(page) -> str:
    """Visible text of every frame - a hosted form thanks you inside its iframe."""
    parts = []
    for frame in page.frames:
        try:
            parts.append(await asyncio.wait_for(
                frame.evaluate("() => document.body ? document.body.innerText : ''"), 5))
        except Exception:
            pass
    return "\n".join(parts)


def fresh_thanks(now: str, before: str) -> bool:
    for hit in filler.SUCCESS_TEXT_RE.finditer(now):
        if now[max(0, hit.start() - 20): hit.end() + 20] not in before:
            return True
    return False


async def mark_card(n: int, text: str) -> None:
    anchor = ANCHOR["page"]
    if anchor is None or anchor.is_closed():
        return
    try:
        await anchor.evaluate("([n, t]) => { const e = document.getElementById('s' + n); if (e) e.textContent = t; }",
                              [n, text])
    except Exception:
        pass


async def assist_one(context, cfg, env, site: dict, n: int, total: int, wait_s: int) -> dict:
    sender = sender_for(cfg, site)
    row = {"timestamp": datetime.now().isoformat(timespec="seconds"), "website": site["website"],
           "company_name": site["company_name"], "contact_page": site["contact_page"],
           "sender": sender["name"], "sender_email": sender["email"]}
    page = await open_tab(context)
    clicked = {"at": 0.0}
    await page.expose_binding("__wbSubmitted", lambda source: clicked.__setitem__(
        "at", clicked["at"] or asyncio.get_event_loop().time()))
    await page.add_init_script(WATCH_JS)
    try:
        try:
            await page.goto(site["contact_page"], wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass                                  # a slow page is still worth a person's look
        await discovery.settle(page, 2500)

        refused: list[str] = []

        async def prepare():
            """Fill the best form that has a message box. None when there is none,
            or when what it still wants only a buyer could answer (kept in refused)."""
            for score, form, frame in discovery.rank_forms(await discovery.forms_everywhere(page)):
                if usable(form):
                    # A CAPTCHA refuses a window the agent controls ("Failed to
                    # validate Captcha" on federato.ai, 2026-10-30), so the person
                    # would solve it for nothing.
                    if await discovery.has_captcha(page, frame):
                        refused[:] = ["a CAPTCHA, which fails in this agent-controlled window"]
                        return None
                    ctx = context_for(cfg, site, sender)
                    subject = render_string(env, str(cfg.path("form", "subject_line", default="Enquiry")), ctx)
                    left = await true_fill(frame, form, cfg, ctx, env, subject, sender)
                    if left:
                        refused[:] = left
                        return None
                    refused.clear()
                    try:
                        await frame.locator(f"form[data-agent-form='{form['form_key']}']").scroll_into_view_if_needed(timeout=4000)
                    except Exception:
                        pass
                    return form, frame
            return None

        # HubSpot-style embeds mount their form seconds after the page settles
        # (hebbia.com was still empty when first read, 2026-10-30): look again
        # for a while before handing the tab over to be filled by hand. Never
        # later - by then the person may be typing into it.
        async def try_prepare():
            try:
                return await prepare()
            except Exception:                     # a page mid-redraw: the next look will do
                return None

        ready = await try_prepare()
        for _ in range(8):
            if ready or refused or page.is_closed():
                break                             # a form that asks buyer questions will not change its mind
            await asyncio.sleep(2.5)
            ready = await try_prepare()
        if not ready:
            # Not left open for copying: the user will not paste (2026-10-30).
            why = (f"form asks what we cannot truthfully answer: {'; '.join(refused)[:150]}" if refused
                   else "no form with a message box the agent could fill")
            row.update(status="not_filled", detail=why + " - not opened for the person")
            return row
        banner = (f"Webify outreach {n}/{total} - {site['company_name']}: form filled. "
                  "Solve any CAPTCHA and press Submit. Close this tab to skip.")
        print(f"[{n}/{total}] {site['company_name']} - filled, tab ready", flush=True)
        await mark_card(n, "filled - tab open")
        try:
            if not page.is_closed():
                await page.evaluate(BANNER_JS, banner)
        except Exception:
            pass

        before_url, before = page.url, await page_text(page)
        refills = 0
        for _ in range(wait_s // 2):
            await asyncio.sleep(2)
            if page.is_closed():
                if clicked["at"]:
                    row.update(status="uncertain", detail="Submit was pressed, then the tab was closed before a confirmation showed")
                else:
                    row.update(status="skipped", detail="tab closed - not submitted")
                return row
            if not clicked["at"]:
                # Chrome's Memory Saver reloads an unwatched tab empty, and Wix
                # rebuilds its message box after load (captcha_assist,
                # 2026-09-30 and 2026-10-02): a lost message means fill again.
                if ready and refills < 30:
                    form, frame = ready
                    try:
                        msg_id = filler.classify(form["fields"])["message"]["agent_id"]
                        box = frame.locator(f"[data-agent-id='{msg_id}']")
                        lost = await box.count() == 0 or not (await box.input_value(timeout=2000)).strip()
                    except Exception:
                        lost = True
                    if lost:
                        try:
                            await discovery.settle(page, 1500)
                            again = await prepare()
                        except Exception:
                            again = None
                        if again:
                            ready, refills = again, refills + 1
                            before_url, before = page.url, await page_text(page)
                            try:
                                await page.evaluate(BANNER_JS, banner)
                            except Exception:
                                pass
                continue                          # nothing counts until the person presses Submit
            now = await page_text(page)
            if fresh_thanks(now, before):
                row.update(status="success", detail="submitted by hand; the site thanked us")
                return row
            if page.url != before_url:
                row.update(status="uncertain", detail=f"submitted by hand; page moved to {page.url[:120]}")
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


async def assist(wait_s: int) -> None:
    sites = pending_sites()
    if not sites:
        print("nothing left to do by hand", flush=True)
        return
    cfg, env = cfg_for_script(), make_env(Path("."))
    total = len(sites)
    print(f"{total} sites to finish by hand, up to {BATCH} filled tabs at a time", flush=True)
    counts: dict[str, int] = {}
    # A slot is held only while a tab is open, so a page the agent cannot fill
    # gives its place straight to the next one.
    room = asyncio.Semaphore(BATCH)

    async def one(n: int, site: dict) -> None:
        async with room:
            await asyncio.sleep(1.5)                      # tabs open behind, a moment apart
            try:
                row = await assist_one(context, cfg, env, site, n, total, wait_s)
            except Exception as e:                        # one tab's trouble never closes the window
                row = {"timestamp": datetime.now().isoformat(timespec="seconds"), "website": site["website"],
                       "company_name": site["company_name"], "contact_page": site["contact_page"],
                       "status": "error", "detail": f"{type(e).__name__}: {str(e)[:100]}"}
        append_log(row)
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        await mark_card(n, row["status"])
        print(f"    [{n}] {site['company_name']} -> {row['status']}: {row['detail'][:90]}", flush=True)

    async with async_playwright() as pw:
        # No flags that hide the automation: sites may refuse a submit from a
        # program-driven window, and the copy cards are the way round that.
        context = await pw.chromium.launch_persistent_context(
            PROFILE_DIR, channel="chrome", headless=False, no_viewport=True, locale="en-US")
        ANCHOR["page"] = context.pages[0] if context.pages else await context.new_page()
        await ANCHOR["page"].set_content(anchor_html(sites))
        await asyncio.gather(*(one(n, site) for n, site in enumerate(sites, 1)))
        print("done:", counts, flush=True)
        try:
            await ANCHOR["page"].evaluate(BANNER_JS, "All batches done - you can close this window.")
        except Exception:
            pass
        await asyncio.sleep(5)
        await context.close()
    if run_active():
        print("a run is writing state.json - record these later with --merge", flush=True)
    else:
        merge()


def merge() -> None:
    if run_active():
        sys.exit("a run is writing state.json right now - merge again when it has finished")
    state = State("output/state.json", sheet=SHEET.name)
    added = 0
    for r in read_log():
        if r["status"] not in CONTACTED_STATUSES:
            continue
        entry = state.get(r["website"]) or {}
        if any(t.get("script") == SCRIPT and t.get("status") in CONTACTED_STATUSES
               for t in ((entry.get("touches") or [entry]) if entry else [])):
            continue                                # already on record
        state.record(r["website"], {
            "row_index": entry.get("row_index", 0), "website": r["website"],
            "company_name": r["company_name"], "method": "form", "status": r["status"],
            "detail": r["detail"], "contact_page": r["contact_page"], "email_used": "",
            "sender": r["sender"], "sender_email": r["sender_email"], "screenshot_before": "",
            "screenshot_after": "", "timestamp": r["timestamp"], "script": SCRIPT, "follow_up": False})
        added += 1
    print(f"recorded {added} hand-submitted site(s) in output/state.json", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=4 * 3600, help="seconds a tab stays open for you")
    ap.add_argument("--merge", action="store_true")
    args = ap.parse_args()
    if args.merge:
        merge()
        return 0
    asyncio.run(assist(args.wait))
    return 0


if __name__ == "__main__":
    sys.exit(main())
