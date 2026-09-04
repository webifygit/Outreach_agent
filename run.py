#!/usr/bin/env python3
"""Contact-form + email outreach agent.

    python run.py --input leads.xlsx                 # dry run (default, nothing is sent)
    python run.py --input leads.xlsx --live          # actually submit and send
    python run.py --input leads.xlsx --limit 5 --no-headless   # watch it work

Per website:
    homepage -> find contact page -> find contact form
        form found and no CAPTCHA  -> fill, screenshot, submit, screenshot, verify
        otherwise                  -> scrape/fall back to an email address and send
    every outcome is screenshotted and written to output/results.xlsx
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from agent import discovery, filler, llm
from agent.config import (Config, default_config_path, identity_placeholders,
                          jitter, load_env_file, placeholder_advice)
from agent.evidence import Evidence
from agent.ledger import build_ledger
from agent.outcomes import build_outcomes
from agent.mailer import Mailer
from agent.report import build_report
from agent.scripts import apply as apply_script
from agent.senders import pick_sender
from agent.sheet import load_rows, write_results
from agent.state import State, norm_site
from agent.templating import (context_for, greeting_for, make_env, presentable_company,
                              render_file, render_string)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def log(msg: str, path: Path | None = None) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


async def open_page(context, url: str, timeout: int, retries: int):
    page = await context.new_page()
    last = ""
    for attempt in range(retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_timeout(1500)
            return page, ""
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
            if attempt < retries:
                await page.wait_for_timeout(2000)
    return page, last


async def locate_form(page, cfg, timeout: int, retries: int, context,
                      contact_candidates: list | None = None):
    """Return (page, ranked, contact_url) for the best contact page we can find.

    ``ranked`` holds (score, form, frame) triples, best first. The frame is part
    of the result because a form may live inside an iframe (HubSpot, Jotform,
    Google Forms...) and every later step has to be scoped to its own document.
    """
    contact_candidates = contact_candidates if contact_candidates is not None else []
    adaptive = bool(cfg.path("run", "adaptive_settle", default=True))
    last_good_url = page.url  # most recent page that actually loaded (status < 400)
    best = (page, [], page.url)  # always a real page, even if no form is ever found
    await discovery.settle(page, 900, adaptive)
    ranked = discovery.rank_forms(await discovery.forms_everywhere(page))
    if ranked and ranked[0][0] >= 60:
        return page, ranked, page.url

    limit = int(cfg.path("form", "max_contact_pages_to_try", default=6))
    candidates = await discovery.find_contact_links(page, page.url, limit)
    for guess in discovery.guessed_paths(page.url):
        if guess not in candidates and len(candidates) < limit:
            candidates.append(guess)
    contact_candidates.extend(candidates)

    best_score = ranked[0][0] if ranked else -999
    if ranked:
        best = (page, ranked, page.url)

    for cand in candidates[:limit]:
        try:
            resp = await page.goto(cand, wait_until="domcontentloaded", timeout=timeout)
            if resp and resp.status >= 400:
                continue
            await page.wait_for_timeout(1200)
        except Exception:
            continue
        last_good_url = page.url
        await discovery.settle(page, 1800, adaptive)
        ranked = discovery.rank_forms(await discovery.forms_everywhere(page))
        if (not ranked or ranked[0][0] < 40):
            # Nothing yet is usually a form that has not mounted, not a page
            # without one. Look once more before writing the page off.
            await discovery.settle(page, 2000, adaptive)
            ranked = discovery.rank_forms(await discovery.forms_everywhere(page))
        if ranked and ranked[0][0] > best_score:
            best_score, best = ranked[0][0], (page, ranked, page.url)
        if best_score >= 60:
            break

    if best_score < 40 and page.url != last_good_url:
        # Every remaining candidate after the last usable one was a dead end
        # (404, timeout...) - don't leave the browser stranded there. Email
        # scraping needs to run on real content, e.g. a contact page whose
        # only "form" is a mailto: link.
        try:
            await page.goto(last_good_url, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_timeout(1000)
        except Exception:
            pass
        best = (page, best[1], last_good_url)
    return best


# Browser launches are serialised so a launch can be attributed to the worker
# that asked for it: the only handle on an individual browser process is the
# set of driver children that appeared while it was starting, and concurrent
# launches make that ambiguous. Launches are rare (startup, recycle, relaunch)
# and take about a second, so the contention costs nothing.
_LAUNCH_LOCK = asyncio.Lock()


def _driver_pid(pw) -> int:
    """PID of the node driver every browser in this run is a child of.

    Playwright internals, so it is guarded: losing it costs the per-worker kill
    and nothing else - the run falls back to the whole-process watchdog.
    """
    try:
        return pw.chromium._impl_obj._connection._transport._proc.pid
    except Exception:
        return 0


def _child_pids(pid: int) -> set[int]:
    if not pid:
        return set()
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return set()
    kids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) == pid:
            kids.add(int(parts[0]))
    return kids


async def open_browser(pw, cfg, old_browser=None):
    """Launch a fresh browser + context, discarding any previous one.

    Chromium can die mid-batch (it did, after ~90 sites on a 118-site run:
    "Connection closed while reading from the driver"). Every later
    context.new_page() then throws, so without this the run marches on
    recording failures for sites it never actually visited.
    """
    if old_browser is not None:
        # A browser that has stopped responding does not answer close() either,
        # so this is bounded. Leaving it is safe: the caller only ever discards
        # a browser it has already given up on, and the watchdog SIGKILLs the
        # process when it was wedged.
        try:
            await asyncio.wait_for(old_browser.close(), timeout=20)
        except Exception:
            pass
    launch: dict = {"headless": bool(cfg.path("run", "headless", default=True))}
    # an installed browser (msedge, chrome) rather than the one Playwright ships
    channel = str(cfg.path("run", "browser_channel", default="") or "").strip()
    if channel:
        launch["channel"] = channel
    async with _LAUNCH_LOCK:
        drv = _driver_pid(pw)
        before = _child_pids(drv)
        try:
            browser = await asyncio.wait_for(pw.chromium.launch(**launch), timeout=45)
        except Exception as exc:
            if not channel:
                raise
            # a machine without that browser should still run, not stop dead
            print(f"    ! {channel} could not be launched ({exc}); "
                  f"falling back to the bundled browser", flush=True)
            launch.pop("channel")
            browser = await asyncio.wait_for(pw.chromium.launch(**launch), timeout=45)
        # exactly one new child means we know which process is this browser;
        # anything else and we simply go without a pid for it
        fresh = _child_pids(drv) - before
        browser_pid = fresh.pop() if len(fresh) == 1 else 0
    context = await browser.new_context(
        user_agent=UA, viewport={"width": 1440, "height": 960},
        locale="en-US", ignore_https_errors=True,
    )
    context.set_default_timeout(int(cfg.path("run", "page_timeout_ms", default=30000)))
    return browser, context, browser_pid


def _row_result(row, status: str, detail: str, method: str = "none") -> dict:
    return {
        "row_index": row["row_index"], "website": row["website"],
        "company_name": row["company_name"], "method": method,
        "status": status, "detail": detail,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


async def _close_stray_pages(context) -> None:
    """process() owns a page it may not have closed; drop leftovers."""
    try:
        pages = list(context.pages)
    except Exception:
        return
    for page in pages:
        # bounded for the same reason as above - this runs right after a site
        # timed out, i.e. on a browser that may never answer again
        try:
            await asyncio.wait_for(page.close(), timeout=10)
        except Exception:
            pass


def _embed_label(form: dict) -> str:
    """Host of the iframe a form came from - "hsforms.net", "jotform.com", ..."""
    host = urlparse(form.get("frame_url", "")).netloc
    return host or "same-origin iframe"


async def process(row, context, cfg, env, mailer, ev, args, logfile, state=None):
    url = row["website"]
    idx = row["row_index"]
    form_sender = pick_sender(cfg, idx, "form_senders")
    ctx = context_for(cfg, row, form_sender)
    result = {
        "row_index": idx, "website": url, "company_name": row["company_name"],
        "method": "", "status": "", "detail": "", "contact_page": "",
        "email_used": "", "sender": form_sender.get("name", ""),
        "sender_email": form_sender.get("email", ""),
        "screenshot_before": "", "screenshot_after": "",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }

    timeout = int(cfg.path("run", "page_timeout_ms", default=30000))
    retries = int(cfg.path("run", "nav_retries", default=2))
    # Opening the site has its own, shorter ceiling: a dead host otherwise burns
    # this twice over before we learn anything. Everything after it - waiting for
    # a field, a click, a confirmation - keeps the full timeout, because those
    # happen on pages that have already proved they load.
    nav_timeout = int(cfg.path("run", "nav_timeout_ms", default=0) or timeout)

    page, err = await open_page(context, url, nav_timeout, retries)
    if err:
        result.update(method="none", status="unreachable", detail=err)
        await page.close()
        return result

    # A name split off the domain runs the words together
    # ("secondliftingequipment"), which reads badly in a greeting. The site
    # itself nearly always states its real name, so ask the page first.
    if not row.get("company_from_sheet", False):
        scraped = await discovery.site_name(page)
        if scraped and presentable_company(scraped):
            ctx["company_name"] = scraped
            result["company_name"] = scraped
    ctx["greeting_name"] = greeting_for(ctx.get("company_name", ""),
                                        ctx.get("contact_first_name", ""))

    if cfg.path("llm", "enabled", default=False) and cfg.path("llm", "use_site_content", default=True):
        site_summary = await discovery.page_summary(page)
    else:
        site_summary = ""
    ctx["ai_hook"] = llm.build_hook(cfg, ctx, site_summary)

    subject = render_string(env, str(cfg.path("form", "subject_line", default="Enquiry")), ctx)

    contact_candidates: list[str] = []
    page, ranked, contact_url = await locate_form(page, cfg, timeout, retries, context,
                                                  contact_candidates)
    result["contact_page"] = contact_url

    top_score, top_form, top_frame = (ranked[0] if ranked else (-999, None, None))
    captcha = await discovery.has_captcha(page, top_frame)

    use_form = top_form is not None and top_score >= 40
    if use_form and captcha and cfg.path("form", "skip_if_captcha", default=True):
        use_form = False
        result["detail"] = f"CAPTCHA present ({captcha}) - falling back to email. "

    # A form with nowhere to put the message can only deliver a name and an
    # email address - the recipient gets an enquiry that says nothing. The
    # email fallback carries the full pitch, so prefer it. Set
    # form.require_message: false to submit such forms anyway.
    # A form we decline here is kept: if no email turns up, using it is better
    # than not contacting the business at all.
    spare_form_url = ""
    if use_form and cfg.path("form", "require_message", default=True):
        if "message" not in filler.classify(top_form["fields"]):
            use_form = False
            spare_form_url = contact_url or page.url
            result["detail"] += "form has no message field (pitch could not be included) - preferring email. "

    async def attempt_form(form, frame) -> bool:
        """Fill and submit one form. True if this row is finished with."""
        result["method"] = "form"
        if form.get("in_iframe"):
            result["detail"] += f"embedded form in iframe ({_embed_label(form)}). "
        report = await filler.fill_form(frame, form, cfg, ctx, env, subject)
        result["screenshot_before"] = await ev.shot(page, idx, url, "01_filled")

        if report["missing_required"]:
            result["detail"] += f"unmapped required fields: {report['missing_required']}. "

        if not cfg.live:
            result["status"] = "dry_run"
            result["detail"] += f"filled {report['roles']} - not submitted (dry run)"
            return True

        before_url = page.url
        click = await filler.submit_form(frame, form["form_key"], timeout)
        status, detail = await filler.verify_submission(page, frame, before_url, form["form_key"])
        result["screenshot_after"] = await ev.shot(page, idx, url, "02_after_submit")
        result["status"] = status
        result["detail"] += f"{click}; {detail}"
        return status != "failed"

    if use_form:
        if await attempt_form(top_form, top_frame):
            await page.close()
            return result
        result["detail"] += " | retrying via email"

    # ---- email fallback -------------------------------------------------
    host = urlparse(url).netloc
    sheet_email = str(row.get("email") or "").strip()
    email_on = bool(cfg.path("email", "enabled", default=True))
    if not email_on:
        # Forms only. Skip the address hunt entirely - it costs page loads and
        # would only produce an address we are not allowed to write to.
        scraped = []
    else:
        # Look past the current page: the address is usually on /contact-us/, and
        # a site with no reachable address at all is the one outcome that leaves
        # the agent with nothing to do.
        scraped = await discovery.harvest_emails(page, host, contact_candidates, timeout)
    address = sheet_email or (scraped[0] if scraped else "")

    if not address and spare_form_url:
        # Nothing to write to. The form set aside earlier is the only way in,
        # so take it - the message tiers are dropped, but every other field
        # still carries who we are and why we are writing.
        result["detail"] += "no email address found either - using the form anyway. "
        try:
            await page.goto(spare_form_url, wait_until="domcontentloaded", timeout=timeout)
            await discovery.settle(page)
            again = discovery.rank_forms(await discovery.forms_everywhere(page))
        except Exception:
            again = []
        if again and again[0][0] >= 40:
            score2, form2, frame2 = again[0]
            if await attempt_form(form2, frame2):
                await page.close()
                return result

    if not result["screenshot_before"]:
        result["screenshot_before"] = await ev.shot(page, idx, url, "01_page")
    await page.close()

    if not address:
        result["method"] = result["method"] or "none"
        if not email_on:
            # Say why plainly: this site may well have an address, we are simply
            # not using email at the moment. Re-runs will pick it up again.
            result["status"] = "skipped_no_email"
            result["detail"] += ("no usable contact form, and email is paused "
                                 "(email.enabled: false) - not contacted")
        else:
            result["status"] = "no_contact_found"
            result["detail"] += "no contact form and no email address discoverable"
        return result

    # One message per mailbox, however many sites point at it. Two domains
    # owned by one company - or a shared agency address - would otherwise each
    # earn the same person a separate copy.
    if state is not None and cfg.path("run", "skip_already_contacted", default=True):
        prior = state.contacted_address(address)
        if prior:
            result["method"] = "email"
            result["email_used"] = address
            result["status"] = "skipped_duplicate"
            result["detail"] += (f"{address} already contacted via "
                                 f"{prior.get('website', '?')} on {prior.get('timestamp', '?')[:10]}")
            return result

    email_sender = pick_sender(cfg, idx, "email_senders")
    email_ctx = {**ctx, "sender": email_sender}

    result["method"] = "email"
    result["email_used"] = address
    result["sender"] = email_sender.get("name", "")
    result["sender_email"] = email_sender.get("email", "")
    body = render_file(env, cfg, cfg.path("email", "body_template_path"), email_ctx)
    subj = render_string(env, str(cfg.path("email", "subject_template", default="Hello")), email_ctx)
    html_path = cfg.path("email", "html_template_path", default="")
    html = render_file(env, cfg, html_path, email_ctx) if html_path else ""

    status, detail = mailer.send(address, subj, body, html, email_sender)
    result["status"] = status
    result["detail"] += detail
    result["screenshot_after"] = ev.note(
        idx, url, "02_email", f"To: {address}\nSubject: {subj}\n\n{body}"
    )
    if status == "sent":
        await asyncio.sleep(jitter(cfg.path("email", "per_send_delay"), (20, 60)))
    return result


class RunLock:
    """A pid file for the duration of a run, so other processes can see it.

    Anything that mutates the history (clearing drafts, for one) has to know a
    run is in flight even when it was started from a terminal rather than the
    web UI.
    """

    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _dump_rows(cfg, rows, mode, total, done) -> None:
    """This sheet's rows as JSON, for the console to read.

    Purely additive - the dashboards, ledger and state file are untouched.
    Written next to the report so it shares its lifecycle.
    """
    path = cfg.resolve(cfg.path("paths", "rows_json_path", default="output/run_rows.json"))
    payload = {
        "mode": mode, "total": total, "done": done,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "rows": rows,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


async def main_async(args) -> int:
    load_env_file(Path(__file__).resolve().parent)
    cfg = Config.load(args.config)
    if args.live:
        cfg["run"]["mode"] = "live"
    if args.no_headless:
        cfg["run"]["headless"] = False

    logfile = cfg.resolve(cfg.path("paths", "log_path", default="output/run.log"))
    logfile.parent.mkdir(parents=True, exist_ok=True)

    # Which pitch this sheet gets. Applied before any template path is read,
    # and fatal if unknown - the wrong pitch reaching a targeted sheet cannot
    # be taken back, so a typo stops the run instead of silently using the
    # default one.
    try:
        script_key, script_label = apply_script(cfg, args.script)
    except ValueError as exc:
        log(f"REFUSING to run: {exc}", logfile)
        return 2
    log(f"script: {script_label} ({script_key})", logfile)

    placeholders = identity_placeholders(cfg)
    if placeholders:
        log("-" * 72, logfile)
        log(f"CONFIG: {len(placeholders)} placeholder identity value(s) still in "
            f"{Path(args.config).name} - these get typed into real contact forms:", logfile)
        for item in placeholders[:14]:
            log(f"    {item}", logfile)
        log(placeholder_advice(args.config), logfile)
        log("-" * 72, logfile)
        if cfg.live:
            log("REFUSING to send live with a placeholder identity. Nothing was sent.", logfile)
            return 2

    rows, skipped_rows = load_rows(args.input)
    if skipped_rows:
        bad = ", ".join(f"row {s['row_index']} ({s['website']!r})" for s in skipped_rows[:10])
        more = f" and {len(skipped_rows) - 10} more" if len(skipped_rows) > 10 else ""
        log(f"skipped {len(skipped_rows)} row(s) with no usable website: {bad}{more}", logfile)
    # The same site listed twice in one sheet - usually as www/non-www or with
    # a trailing slash - is one business, not two.
    scope = str(cfg.path("run", "dedupe_scope", default="host"))
    seen: dict[str, dict] = {}
    deduped, dupes = [], []
    for r in rows:
        key = norm_site(r["website"], scope)
        if key and key in seen:
            dupes.append((r, seen[key]))
            continue
        if key:
            seen[key] = r
        deduped.append(r)
    if dupes:
        log(f"sheet has {len(dupes)} duplicate site(s) - keeping the first of each:", logfile)
        for dup, first in dupes[:8]:
            log(f"    row {dup['row_index']} {dup['website']} == row {first['row_index']} {first['website']}",
                logfile)
        if len(dupes) > 8:
            log(f"    and {len(dupes) - 8} more", logfile)
    rows = deduped

    limit = args.limit or int(cfg.path("run", "max_sites", default=0))
    if limit:
        rows = rows[:limit]

    state_path = cfg.resolve(cfg.path("paths", "state_path", default="output/state.json"))
    # state.json is the only record of who has been contacted and cannot be
    # rebuilt. Existing backups fire only before destructive actions, so a run
    # that corrupts it mid-campaign has no recovery point. Snapshot first,
    # keep the last 20.
    if state_path.exists():
        snaps = state_path.parent / "state_snapshots"
        snaps.mkdir(parents=True, exist_ok=True)
        shutil.copy2(state_path, snaps / f"state_{datetime.now():%Y%m%d_%H%M%S}.json")
        old_snaps = sorted(snaps.glob("state_*.json"))[:-20]
        for stale in old_snaps:
            try:
                stale.unlink()
            except OSError:
                pass
        log(f"state snapshot taken ({len(sorted(snaps.glob('state_*.json')))} kept)", logfile)

    state = State(state_path,
                  dedupe_scope=str(cfg.path("run", "dedupe_scope", default="host")),
                  sheet=Path(args.input).name)
    # Re-uploading a sheet must not make already-approached rows disappear: they
    # are carried through as their own list, into the results file, the
    # dashboard and the ledger. Deliberately NOT written back into state - that
    # is keyed by URL, so recording a skip would overwrite the very record of
    # the original contact we are trying to preserve.
    dup_skips: list[dict] = []
    if cfg.path("run", "skip_already_contacted", default=True):
        fresh = []
        for r in rows:
            prior = state.contacted_site(r["website"])
            if not prior:
                fresh.append(r)
                continue
            when = str(prior.get("timestamp", ""))[:10]
            how = prior.get("method", "?")
            via = prior.get("email_used") or prior.get("contact_page") or prior.get("website", "")
            dup_skips.append({
                "row_index": r["row_index"], "website": r["website"],
                "company_name": r["company_name"],
                "method": how, "status": "skipped_duplicate",
                "detail": f"already approached by {how} ({prior.get('status', '?')}) on {when}"
                          + (f" via {via}" if via else ""),
                "email_used": prior.get("email_used", ""),
                "contact_page": prior.get("contact_page", ""),
                "sender": prior.get("sender", ""),
                "sender_email": prior.get("sender_email", ""),
                "first_contacted": prior.get("timestamp", ""),
                "screenshot_before": prior.get("screenshot_before", ""),
                "screenshot_after": prior.get("screenshot_after", ""),
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            })
        if dup_skips:
            log(f"{len(dup_skips)} site(s) already approached - listed as skipped_duplicate, not re-contacted:",
                logfile)
            for d in dup_skips[:8]:
                log(f"    {d['website']} - {d['detail']}", logfile)
            if len(dup_skips) > 8:
                log(f"    and {len(dup_skips) - 8} more", logfile)
        rows = fresh

    # Sites known to wedge the browser. Each one costs a watchdog kill and a
    # full process restart - every worker loses its place, not just the one
    # that hit it - so skipping them is far cheaper than attempting them. They
    # are recorded, not silently dropped, so the sheet still accounts for them.
    blocked: list[dict] = []
    skip_hosts = {norm_site(u, scope)
                  for u in (cfg.path("run", "skip_sites", default=[]) or [])}
    if skip_hosts:
        keep = []
        for r in rows:
            if norm_site(r["website"], scope) in skip_hosts:
                blocked.append({
                    "row_index": r["row_index"], "website": r["website"],
                    "company_name": r["company_name"],
                    "method": "none", "status": "skipped_blocked",
                    "detail": "on run.skip_sites - this site wedges the browser; "
                              "skipped so it cannot stall the batch",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                })
            else:
                keep.append(r)
        if blocked:
            log(f"{len(blocked)} site(s) skipped by run.skip_sites", logfile)
        rows = keep

    # Rows this sheet no longer has to visit because an earlier attempt already
    # covered them. They leave `rows`, so without carrying the number forward
    # the progress counter restarts from zero against a shrinking total on every
    # resume - which reads as work being lost when nothing has been.
    carried_over = 0

    if getattr(args, "continue_batch", False):
        # Picking up a batch that stopped: anything with a record was already
        # visited, whatever the outcome, so start from the first row that has
        # none. Matched on the normalised host so a www/https difference in the
        # sheet does not look like a new site.
        seen_hosts = {norm_site(u, scope) for u in state.data}
        pending = [r for r in rows if norm_site(r["website"], scope) not in seen_hosts]
        log(f"continuing batch: {len(rows) - len(pending)} row(s) already attempted, "
            f"{len(pending)} to go", logfile)
        carried_over = len(rows) - len(pending)
        rows = pending
    elif cfg.path("run", "resume", default=True) and not args.no_resume:
        pending = [r for r in rows if not state.is_done(r["website"])]
        if len(pending) < len(rows):
            log(f"resume: skipping {len(rows) - len(pending)} already-processed rows", logfile)
        carried_over = len(rows) - len(pending)
        rows = pending


    mailer = Mailer(cfg, cfg.live)
    if cfg.path("run", "skip_already_contacted", default=True):
        mailer.already_contacted = state.contacted_address
    warn = mailer.preflight()
    if warn:
        log(f"WARNING (email fallback): {warn}", logfile)

    env = make_env(cfg["_root"])
    ev = Evidence(cfg.resolve(cfg.path("paths", "evidence_dir", default="evidence")))

    mode = "LIVE - forms will be submitted and email sent" if cfg.live else "DRY RUN - nothing sent"
    log(f"{mode} | {len(rows)} sites | evidence -> {ev.dir}", logfile)
    if cfg.live and not args.yes:
        if input("Type 'yes' to proceed in live mode: ").strip().lower() != "yes":
            log("aborted", logfile)
            return 1

    run_lock = RunLock(cfg.resolve(cfg.path("paths", "lock_path", default="output/run.lock")))
    run_lock.__enter__()

    results: list[dict] = []
    daily_cap = int(cfg.path("email", "daily_limit", default=50))
    site_timeout = float(cfg.path("run", "site_timeout_s", default=180))
    today = datetime.now().date().isoformat()
    report_path = cfg.resolve(cfg.path("paths", "report_path", default="output/report.html"))
    report_mode = "live" if cfg.live else "dry_run"

    # A hung browser call cannot be broken from inside: asyncio.wait_for cancels
    # the task and then waits for a cancellation the driver never acknowledges,
    # so the per-site ceiling hangs with it. This heartbeat is the way out - if
    # no site has completed for long enough, end the process outright. The lock
    # file is left behind on purpose, which is the signal the supervisor uses to
    # restart and carry on from the next row.
    heartbeat = {"at": time.time()}

    async def watchdog():
        limit = max(120.0, site_timeout * 2)
        while True:
            await asyncio.sleep(15)
            idle = time.time() - heartbeat["at"]
            if idle > limit:
                log(f"no site has completed for {idle:.0f}s - the browser has stopped "
                    f"responding; ending the process so the run can be restarted", logfile)
                os._exit(3)

    async with async_playwright() as pw:
        watch = asyncio.create_task(watchdog())
        recycle_every = int(cfg.path("run", "recycle_browser_every", default=50))
        max_consecutive = int(cfg.path("run", "max_consecutive_errors", default=6))
        workers = max(1, int(cfg.path("run", "workers", default=1)))
        workers = min(workers, len(rows)) or 1

        # The queue is the only thing the workers share about *what* to do, so a
        # slow site holds up its own worker and nobody else.
        queue: asyncio.Queue = asyncio.Queue()
        for item in enumerate(rows, 1):
            queue.put_nowait(item)

        done_count = 0          # sites finished, for progress and the row dump
        consecutive_errors = 0  # reset by any success: this catches "everything
                                # is failing", not "these particular sites failed"
        stop = asyncio.Event()  # circuit breaker / daily cap, seen by every worker

        if workers > 1:
            log(f"running {workers} sites at a time", logfile)

        # Per-worker liveness. The whole-process watchdog above is a blunt
        # instrument - it kills every worker because one is stuck, and the run
        # loses its place four times over. This one kills just the wedged
        # worker's browser process, which makes its hung Playwright call raise
        # TargetClosedError (measured: within 3s), so that worker records the
        # site and carries on. The others never notice.
        beats: dict[int, dict] = {}

        async def worker_watchdog():
            # Normally 1.5x the per-site ceiling, so asyncio's own timeout gets
            # first refusal and this only acts when that fails to cancel - which
            # is the case it exists for. Configurable so it can be tested, and
            # tuned if a machine turns out to wedge differently.
            grace = float(cfg.path("run", "worker_stall_grace_s",
                                   default=0) or 0) or max(60.0, site_timeout * 1.5)
            while True:
                await asyncio.sleep(10)
                now = time.time()
                for wid, hb in list(beats.items()):
                    # A worker that has finished is not stuck. Everything else
                    # is fair game - including shutdown, which is where the
                    # integration test found it hanging on context.close() with
                    # no site set. No pid means we could not identify this
                    # browser's process, so leave it to the process watchdog.
                    if hb.get("done") or not hb.get("pid") or hb.get("killed"):
                        continue
                    stuck = now - hb["at"]
                    if stuck > grace:
                        where = hb.get("site") or "shutting down"
                        log(f"w{wid} has been stuck on {where} for {stuck:.0f}s - killing "
                            f"its browser (pid {hb['pid']}); the other workers carry on",
                            logfile)
                        hb["killed"] = True
                        try:
                            os.kill(hb["pid"], signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass

        beat_watch = asyncio.create_task(worker_watchdog())

        async def run_worker(wid: int):
            nonlocal done_count, consecutive_errors
            browser = context = None
            for attempt in range(1, 4):
                try:
                    browser, context, bpid = await open_browser(pw, cfg)
                    break
                except Exception as exc:  # noqa: BLE001
                    log(f"w{wid} could not start a browser ({type(exc).__name__}) - "
                        f"attempt {attempt}/3", logfile)
                    await asyncio.sleep(5)
            if browser is None:
                log(f"w{wid} gave up starting a browser - running without this worker", logfile)
                return
            beats[wid] = {"at": time.time(), "site": "", "pid": bpid, "killed": False}
            since_recycle = 0
            tag = f"w{wid} " if workers > 1 else ""
            try:
                while not stop.is_set():
                    try:
                        n, row = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    if state.sent_today(today) >= daily_cap:
                        log(f"daily email cap ({daily_cap}) reached - stopping", logfile)
                        stop.set()
                        break

                    # Long batches leak browser memory until Chromium falls over,
                    # so retire it on a schedule rather than waiting for the crash.
                    if recycle_every and since_recycle >= recycle_every:
                        log(f"{tag}recycling browser after {since_recycle} sites", logfile)
                        browser, context, bpid = await open_browser(pw, cfg, browser)
                        beats[wid].update(pid=bpid, killed=False)
                        since_recycle = 0

                    log(f"{tag}[{n}/{len(rows)}] {row['website']}", logfile)
                    beats[wid].update(at=time.time(), site=row["website"])
                    res = None
                    for attempt in (1, 2):
                        if not browser.is_connected():
                            log(f"{tag}    browser is not connected - relaunching", logfile)
                            browser, context, bpid = await open_browser(pw, cfg, None)
                            beats[wid].update(pid=bpid, killed=False)
                        try:
                            # No single site may hold up the batch. Bounded work
                            # can still add up past this (nav retries x
                            # contact-page candidates), and a wedged page can
                            # block in ways the browser timeout does not cover,
                            # so the whole per-site pipeline gets one ceiling.
                            res = await asyncio.wait_for(
                                process(row, context, cfg, env, mailer, ev, args, logfile, state),
                                timeout=site_timeout,
                            )
                            break
                        except asyncio.TimeoutError:
                            res = _row_result(row, "timeout",
                                              f"site exceeded run.site_timeout_s ({site_timeout:.0f}s) - skipped")
                            await _close_stray_pages(context)
                            break
                        except Exception as exc:  # noqa: BLE001
                            if beats[wid].get("killed"):
                                # Our own watchdog killed this browser because this
                                # site wedged it. Retrying would wedge the fresh one
                                # too, so record it and move on - that is the whole
                                # point of killing one worker instead of the run.
                                res = _row_result(row, "timeout",
                                                  "this site wedged the browser; it was killed "
                                                  "and the site skipped so the batch could go on")
                                browser, context, bpid = await open_browser(pw, cfg, None)
                                beats[wid].update(at=time.time(), site="", pid=bpid, killed=False)
                                since_recycle = 0
                                break
                            # A dead browser is worth one relaunch and one retry -
                            # the site itself was never really attempted. Anything
                            # else is this site's own failure.
                            if attempt == 1 and not browser.is_connected():
                                log(f"{tag}    browser died ({type(exc).__name__}) - relaunching, "
                                    f"retrying this site", logfile)
                                browser, context, bpid = await open_browser(pw, cfg, None)
                                beats[wid].update(pid=bpid, killed=False)
                                since_recycle = 0
                                continue
                            res = _row_result(row, "error", f"{type(exc).__name__}: {str(exc)[:200]}")
                            break

                    # Everything from here to the end of the loop body runs
                    # without an await, so another worker cannot interleave
                    # part-way through and corrupt the state file or the counts.
                    heartbeat["at"] = time.time()
                    beats[wid].update(at=time.time(), site="")
                    results.append(res)
                    state.record(row["website"], res)
                    since_recycle += 1
                    done_count += 1
                    log(f"{tag}    -> {res['method'] or '-'} / {res['status']} "
                        f":: {res['detail'][:110]}", logfile)
                    # This sheet's own numbers - not the running total across every
                    # sheet ever uploaded, which is what the overall dashboard is for.
                    build_report(dup_skips + blocked + results, report_mode, report_path)
                    # Rebuilt here rather than only at the end: every run since the
                    # 27th was stopped or wedged before the final build, leaving the
                    # overall dashboard two days stale.
                    build_report(list(state.data.values()), report_mode,
                                 cfg.resolve(cfg.path("paths", "report_all_path",
                                                      default="output/report_all.html")))
                    _dump_rows(cfg, dup_skips + blocked + results, report_mode,
                               len(dup_skips) + len(blocked) + carried_over + len(rows),
                               len(dup_skips) + len(blocked) + carried_over + done_count)

                    # Burning through the rest of the sheet recording failures is
                    # worse than stopping: the rows look attempted when they never
                    # were. Any success clears it, so this fires on a broken run,
                    # not on a patch of broken sites.
                    consecutive_errors = consecutive_errors + 1 if res["status"] == "error" else 0
                    if max_consecutive and consecutive_errors >= max_consecutive:
                        log(f"stopping - {consecutive_errors} sites failed in a row, something "
                            f"is wrong. Remaining rows left untouched so a re-run can retry them.",
                            logfile)
                        stop.set()
                        break

                    if not queue.empty():
                        await asyncio.sleep(jitter(cfg.path("run", "delay_between_sites"), (8, 20)))
                        beats[wid]["at"] = time.time()
            finally:
                hb = beats.get(wid)
                if hb is not None:
                    hb.update(at=time.time(), site="")
                try:
                    await asyncio.wait_for(context.close(), timeout=15)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(browser.close(), timeout=15)
                except Exception:
                    pass
                # only now is this worker genuinely finished; until this point
                # the watchdog is still entitled to kill its browser
                if hb is not None:
                    hb["done"] = True
                beats.pop(wid, None)

        outcomes_per_worker = await asyncio.gather(
            *(run_worker(i + 1) for i in range(workers)), return_exceptions=True)
        for i, outcome in enumerate(outcomes_per_worker, 1):
            if isinstance(outcome, BaseException):
                # Silently losing a worker means running at a fraction of the
                # configured rate for the rest of the batch with nothing in the
                # log to say so.
                log(f"w{i} ended on an unhandled {type(outcome).__name__}: "
                    f"{str(outcome)[:160]}", logfile)
        # results arrive in completion order; the sheet's order is what readers expect
        results.sort(key=lambda r: r.get("row_index", 0))
        watch.cancel()
        beat_watch.cancel()

    mailer.close()
    # The WHOLE sheet, not just this attempt's share of it. A run that was
    # restarted part-way (watchdog, crash) only holds the rows it personally
    # processed in `results` - writing those alone left results.xlsx showing
    # six rows for a 467-row sheet, which reads as catastrophic failure.
    sheet_name = Path(args.input).name
    sheet_rows = [r for r in state.data.values() if r.get("sheet") == sheet_name]
    seen = {norm_site(r.get("website", ""), scope) for r in sheet_rows}
    sheet_rows += [r for r in dup_skips + blocked + results
                   if norm_site(r.get("website", ""), scope) not in seen]
    out = write_results(sheet_rows, cfg.resolve(cfg.path("paths", "results_path")))
    log(f"done - {len(results)} rows this attempt, {len(sheet_rows)} in the sheet -> {out}",
        logfile)
    log(f"screenshots -> {ev.dir}", logfile)

    _dump_rows(cfg, dup_skips + blocked + results, report_mode,
               len(dup_skips) + len(blocked) + carried_over + len(rows),
               len(dup_skips) + len(blocked) + carried_over + len(results))
    report = build_report(dup_skips + blocked + results, report_mode, report_path)
    log(f"dashboard (this sheet) -> {report}", logfile)
    overall = build_report(list(state.data.values()), report_mode,
                           cfg.resolve(cfg.path("paths", "report_all_path",
                                                default="output/report_all.html")))
    log(f"dashboard (all sheets) -> {overall}", logfile)
    ledger = build_ledger(list(state.data.values()) + dup_skips + blocked,
                          cfg.resolve(cfg.path("paths", "ledger_path", default="output/contacted.xlsx")))
    log(f"contacted ledger -> {ledger}", logfile)
    # Kept per sheet rather than overwritten: report.html and results.xlsx are
    # rebuilt every run, so without this the outcome of sheet N is gone the
    # moment sheet N+1 starts.
    sheet_name = Path(args.input).stem
    outcomes = build_outcomes(list(state.data.values()) + dup_skips + blocked,
                              report_path.parent / f"outcomes_{sheet_name}.xlsx",
                              sheet_filter=Path(args.input).name)
    log(f"outcome sheet (this sheet) -> {outcomes}", logfile)

    run_lock.__exit__()

    tally: dict[str, int] = {}
    for r in dup_skips + blocked + results:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    log("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())), logfile)
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Contact-form and email outreach agent")
    p.add_argument("--input", required=True, help="Excel/CSV of leads")
    p.add_argument("--config", default=str(default_config_path(Path(__file__).parent)))
    p.add_argument("--live", action="store_true", help="actually submit forms and send email")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--no-headless", action="store_true", help="show the browser window")
    p.add_argument("--no-resume", action="store_true", help="reprocess rows already done")
    p.add_argument("--continue-batch", action="store_true",
                   help="carry on where a stopped run left off: skip every row already "
                        "attempted, dry runs included")
    p.add_argument("--script", default="",
                   help="pitch script to use, by key (see scripts: in the config). "
                        "Default: the config's default_script")
    p.add_argument("--yes", action="store_true", help="skip the live-mode confirmation prompt")
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main_async(parse_args())))
    except KeyboardInterrupt:
        print("\ninterrupted - progress saved in output/state.json, re-run to resume")
        sys.exit(130)
    finally:
        # A stale lock would block the UI from ever clearing drafts again.
        try:
            Path("output/run.lock").unlink(missing_ok=True)
        except OSError:
            pass
