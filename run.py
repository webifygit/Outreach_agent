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
import random
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
from agent.mailer import Mailer
from agent.report import build_report
from agent.senders import pick_sender
from agent.sheet import load_rows, write_results
from agent.state import State
from agent.templating import context_for, make_env, render_file, render_string

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


async def locate_form(page, cfg, timeout: int, retries: int, context):
    """Return (page, ranked, contact_url) for the best contact page we can find.

    ``ranked`` holds (score, form, frame) triples, best first. The frame is part
    of the result because a form may live inside an iframe (HubSpot, Jotform,
    Google Forms...) and every later step has to be scoped to its own document.
    """
    last_good_url = page.url  # most recent page that actually loaded (status < 400)
    best = (page, [], page.url)  # always a real page, even if no form is ever found
    ranked = discovery.rank_forms(await discovery.forms_everywhere(page))
    if ranked and ranked[0][0] >= 60:
        return page, ranked, page.url

    limit = int(cfg.path("form", "max_contact_pages_to_try", default=6))
    candidates = await discovery.find_contact_links(page, page.url, limit)
    for guess in discovery.guessed_paths(page.url):
        if guess not in candidates and len(candidates) < limit:
            candidates.append(guess)

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


def _embed_label(form: dict) -> str:
    """Host of the iframe a form came from - "hsforms.net", "jotform.com", ..."""
    host = urlparse(form.get("frame_url", "")).netloc
    return host or "same-origin iframe"


async def process(row, context, cfg, env, mailer, ev, args, logfile):
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

    page, err = await open_page(context, url, timeout, retries)
    if err:
        result.update(method="none", status="unreachable", detail=err)
        await page.close()
        return result

    if cfg.path("llm", "enabled", default=False) and cfg.path("llm", "use_site_content", default=True):
        site_summary = await discovery.page_summary(page)
    else:
        site_summary = ""
    ctx["ai_hook"] = llm.build_hook(cfg, ctx, site_summary)

    subject = render_string(env, str(cfg.path("form", "subject_line", default="Enquiry")), ctx)

    page, ranked, contact_url = await locate_form(page, cfg, timeout, retries, context)
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
    if use_form and cfg.path("form", "require_message", default=True):
        if "message" not in filler.classify(top_form["fields"]):
            use_form = False
            result["detail"] += "form has no message field (pitch could not be included) - falling back to email. "

    if use_form:
        result["method"] = "form"
        if top_form.get("in_iframe"):
            result["detail"] += f"embedded form in iframe ({_embed_label(top_form)}). "
        report = await filler.fill_form(top_frame, top_form, cfg, ctx, env, subject)
        result["screenshot_before"] = await ev.shot(page, idx, url, "01_filled")

        if report["missing_required"]:
            result["detail"] += f"unmapped required fields: {report['missing_required']}. "

        if not cfg.live:
            result["status"] = "dry_run"
            result["detail"] += f"filled {report['roles']} - not submitted (dry run)"
            await page.close()
            return result

        before_url = page.url
        click = await filler.submit_form(top_frame, top_form["form_key"], timeout)
        status, detail = await filler.verify_submission(
            page, top_frame, before_url, top_form["form_key"]
        )
        result["screenshot_after"] = await ev.shot(page, idx, url, "02_after_submit")
        result["status"] = status
        result["detail"] += f"{click}; {detail}"

        if status != "failed":
            await page.close()
            return result
        result["detail"] += " | retrying via email"

    # ---- email fallback -------------------------------------------------
    host = urlparse(url).netloc
    sheet_email = str(row.get("email") or "").strip()
    scraped = await discovery.collect_emails(page, host)
    address = sheet_email or (scraped[0] if scraped else "")

    if not result["screenshot_before"]:
        result["screenshot_before"] = await ev.shot(page, idx, url, "01_page")
    await page.close()

    if not address:
        result["method"] = result["method"] or "none"
        result["status"] = "no_contact_found"
        result["detail"] += "no contact form and no email address discoverable"
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


async def main_async(args) -> int:
    load_env_file(Path(__file__).resolve().parent)
    cfg = Config.load(args.config)
    if args.live:
        cfg["run"]["mode"] = "live"
    if args.no_headless:
        cfg["run"]["headless"] = False

    logfile = cfg.resolve(cfg.path("paths", "log_path", default="output/run.log"))
    logfile.parent.mkdir(parents=True, exist_ok=True)

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
    limit = args.limit or int(cfg.path("run", "max_sites", default=0))
    if limit:
        rows = rows[:limit]

    state = State(cfg.resolve(cfg.path("paths", "state_path", default="output/state.json")))
    if cfg.path("run", "resume", default=True) and not args.no_resume:
        pending = [r for r in rows if not state.is_done(r["website"])]
        if len(pending) < len(rows):
            log(f"resume: skipping {len(rows) - len(pending)} already-processed rows", logfile)
        rows = pending

    mailer = Mailer(cfg, cfg.live)
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

    results: list[dict] = []
    daily_cap = int(cfg.path("email", "daily_limit", default=50))
    site_timeout = float(cfg.path("run", "site_timeout_s", default=180))
    today = datetime.now().date().isoformat()
    report_path = cfg.resolve(cfg.path("paths", "report_path", default="output/report.html"))
    report_mode = "live" if cfg.live else "dry_run"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=bool(cfg.path("run", "headless", default=True)))
        context = await browser.new_context(
            user_agent=UA, viewport={"width": 1440, "height": 960},
            locale="en-US", ignore_https_errors=True,
        )
        context.set_default_timeout(int(cfg.path("run", "page_timeout_ms", default=30000)))

        for n, row in enumerate(rows, 1):
            if state.sent_today(today) >= daily_cap:
                log(f"daily email cap ({daily_cap}) reached - stopping", logfile)
                break
            log(f"[{n}/{len(rows)}] {row['website']}", logfile)
            try:
                # No single site may hold up the batch. Bounded work can still
                # add up past this (nav retries x contact-page candidates), and
                # a wedged page can block in ways the browser timeout does not
                # cover, so the whole per-site pipeline gets one ceiling.
                res = await asyncio.wait_for(
                    process(row, context, cfg, env, mailer, ev, args, logfile),
                    timeout=site_timeout,
                )
            except asyncio.TimeoutError:
                res = {
                    "row_index": row["row_index"], "website": row["website"],
                    "company_name": row["company_name"], "method": "none",
                    "status": "timeout",
                    "detail": f"site exceeded run.site_timeout_s ({site_timeout:.0f}s) - skipped",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
                # process() owns a page it never got to close; drop any left
                # behind or they accumulate across a long batch.
                for stray in list(context.pages):
                    try:
                        await stray.close()
                    except Exception:
                        pass
            except Exception as exc:  # noqa: BLE001
                res = {
                    "row_index": row["row_index"], "website": row["website"],
                    "company_name": row["company_name"], "method": "none",
                    "status": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
            results.append(res)
            state.record(row["website"], res)
            log(f"    -> {res['method'] or '-'} / {res['status']} :: {res['detail'][:110]}", logfile)
            build_report(list(state.data.values()), report_mode, report_path)
            if n < len(rows):
                await asyncio.sleep(jitter(cfg.path("run", "delay_between_sites"), (8, 20)))

        await context.close()
        await browser.close()

    mailer.close()
    out = write_results(results, cfg.resolve(cfg.path("paths", "results_path")))
    log(f"done - {len(results)} rows -> {out}", logfile)
    log(f"screenshots -> {ev.dir}", logfile)

    report = build_report(list(state.data.values()), report_mode, report_path)
    log(f"dashboard -> {report}", logfile)

    tally: dict[str, int] = {}
    for r in results:
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
    p.add_argument("--yes", action="store_true", help="skip the live-mode confirmation prompt")
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main_async(parse_args())))
    except KeyboardInterrupt:
        print("\ninterrupted - progress saved in output/state.json, re-run to resume")
        sys.exit(130)
