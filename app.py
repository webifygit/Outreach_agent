#!/usr/bin/env python3
"""Local web UI: upload a leads spreadsheet, start a run, watch the live
dashboard - no command line needed after the first `python app.py`.

    python app.py
    open http://127.0.0.1:5000

By default this only listens on localhost. To let others on your network
reach it (AGENT_HOST=0.0.0.0), set AGENT_PASSWORD first - every request then
requires a password (a login page, cookie-based - no username). Without
AGENT_PASSWORD set, binding to 0.0.0.0 is refused, so this can't accidentally
go passwordless on the network.

Starting a run just launches `run.py` as a background subprocess (the same
script the CLI uses) and streams progress via the same report.html the CLI
already writes after every site.
"""
from __future__ import annotations

import os
import json
import secrets
import subprocess
import sys
import re
import threading
import time
from html import escape as html_escape
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session
from werkzeug.utils import secure_filename

from agent import scripts as pitch_scripts
from agent.config import Config, default_config_path, load_env_file

ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "uploads"
ALLOWED_EXT = {".xlsx", ".xls", ".csv", ".tsv"}

load_env_file(ROOT)  # before anything below reads the environment

AGENT_HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "5000"))
AGENT_PASSWORD = os.environ.get("AGENT_PASSWORD", "")

if AGENT_HOST != "127.0.0.1" and not AGENT_PASSWORD:
    raise SystemExit(
        "AGENT_HOST is set to listen beyond localhost, but AGENT_PASSWORD is not set. "
        "Set AGENT_PASSWORD before exposing this to your network - see .env.local.example."
    )

cfg = Config.load(default_config_path(ROOT))
REPORT_PATH = cfg.resolve(cfg.path("paths", "report_path", default="output/report.html"))
STATE_FILE = cfg.resolve(cfg.path("paths", "state_path", default="output/state.json"))
EVIDENCE_DIR = cfg.resolve(cfg.path("paths", "evidence_dir", default="evidence"))
LAUNCH_LOG = cfg.resolve(cfg.path("paths", "log_path", default="output/run.log")).with_name("webapp_launch.log")
RUN_LOG = cfg.resolve(cfg.path("paths", "log_path", default="output/run.log"))
MODE_FILE = STATE_FILE.with_name("last_run_mode.json")

app = Flask(__name__)

# Session signing key - generated once, persisted locally so logins survive
# a service restart. Never committed (gitignored, and it's a local run artifact).
_SECRET_PATH = ROOT / ".flask_secret"
if _SECRET_PATH.exists():
    app.secret_key = _SECRET_PATH.read_text().strip()
else:
    app.secret_key = secrets.token_hex(32)
    _SECRET_PATH.write_text(app.secret_key)

# Explicit, not relying on defaults: this serves plain HTTP on purpose (a
# local LAN tool), so Secure=True would silently make every cookie useless.
app.config.update(
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

LOGIN_HTML = """<!doctype html>
<meta charset="utf-8"><title>Outreach agent - sign in</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0;
    background: #f8f9fa; font: 15px system-ui, -apple-system, sans-serif; }
  @media (prefers-color-scheme: dark) { body { background: #0f172a; color: #f8fafc; } }
  form { background: #fff; border: 1px solid rgba(15,23,42,.08); border-radius: 12px; padding: 32px;
    width: 280px; box-shadow: 0 1px 24px rgba(0,0,0,.06); }
  @media (prefers-color-scheme: dark) { form { background: #1e293b; border-color: rgba(255,255,255,.08); } }
  h1 { font-size: 17px; margin: 0 0 18px; }
  input { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid rgba(15,23,42,.15);
    font-size: 14px; box-sizing: border-box; margin-bottom: 12px; }
  button { width: 100%; padding: 10px; border-radius: 8px; border: 0; background: #2563eb; color: #fff;
    font-weight: 600; cursor: pointer; font-size: 14px; }
  .err { color: #d03b3b; font-size: 13px; margin: -4px 0 12px; }
</style>
<form method="post">
  <h1>Outreach agent</h1>
  __ERROR__
  <input type="password" name="password" placeholder="Password" autofocus required>
  <button type="submit">Sign in</button>
</form>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == AGENT_PASSWORD:
            session["authed"] = True
            session.permanent = True
            return redirect(request.args.get("next") or "/")
        error = '<div class="err">Incorrect password</div>'
    return LOGIN_HTML.replace("__ERROR__", error)


@app.before_request
def _require_auth():
    if not AGENT_PASSWORD or request.path == "/login":
        return None
    if session.get("authed"):
        return None
    if request.path.startswith("/files/") or request.path == "/status":
        return Response("Authentication required", 401)
    return redirect(f"/login?next={request.path}")

_lock = threading.Lock()
STATE: dict = {"proc": None, "input": None, "input_path": None, "live": False,
               "preview": None, "preview_path": None, "script": None,
               "started_at": None, "stopped_by_user": False, "restarts": 0}


CONFIG_PATH = default_config_path(ROOT)


def _script_catalogue() -> tuple[list[dict], str]:
    """The pitch scripts on offer, read fresh from the config file.

    Re-read rather than taken from the `cfg` loaded at import: adding a script
    to the config should show up in the picker on the next page load, not only
    after someone remembers to restart the service.
    """
    try:
        live_cfg = Config.load(CONFIG_PATH)
    except Exception:          # noqa: BLE001 - a broken config must not blank the console
        live_cfg = cfg
    return pitch_scripts.catalogue(live_cfg), pitch_scripts.default_key(live_cfg)


def _last_script() -> str:
    """The pitch the last run used. Survives a restart of this service.

    Resume and "submit dry runs live" continue a batch that was already
    written with one pitch - carrying on with a different one would put two
    different letters through the same sheet.
    """
    key = STATE.get("script")
    if key:
        return str(key)
    try:
        return str(json.loads(MODE_FILE.read_text(encoding="utf-8")).get("script") or "")
    except Exception:  # noqa: BLE001
        return ""


def _script_label(key: str) -> str:
    for entry in _script_catalogue()[0]:
        if entry["key"] == key:
            return entry["label"]
    return key or ""


def _run_report_url() -> str:
    rel = REPORT_PATH.resolve().relative_to(ROOT).as_posix()
    return f"/files/{rel}"


def _last_problem() -> str:
    """The run's own explanation of why it stopped, for the status pill.

    A refused run exits before writing any report, so without this the UI can
    only say "exit 2" and the reason sits unread in a log file.
    """
    try:
        tail = LAUNCH_LOG.read_text(encoding="utf-8", errors="replace")[-6000:]
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        for marker in ("REFUSING", "CONFIG:", "WARNING"):
            if marker in line:
                return line.split("] ", 1)[-1].strip()[:200]
    return ""


def _last_input() -> Path | None:
    """The sheet a "go live" would re-run.

    Falls back to the newest upload on disk: the in-memory record is lost when
    the service restarts, and the button should not disappear just because the
    UI was restarted between the dry run and the decision to send.
    """
    path = STATE.get("input_path")
    if path and Path(path).exists():
        return Path(path)
    try:
        # top level only - uploads/coverage/ holds sheets that were merely checked
        uploads = sorted((p for p in UPLOAD_DIR.glob("*.*") if p.is_file()),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for candidate in uploads:
        if candidate.suffix.lower() in ALLOWED_EXT:
            return candidate
    return None


LOCK_FILE = cfg.resolve(cfg.path("paths", "lock_path", default="output/run.lock"))


def _external_run_active() -> bool:
    """Is a run in flight that this app did not start?

    run.py leaves a pid file while it works. A file whose process is gone is
    stale - from a crash or a kill - and must not block the UI forever.
    """
    try:
        pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if _pid_alive(pid):
        return True
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def _pid_alive(pid: int) -> bool:
    """Is this process still running? Never touches it.

    os.kill(pid, 0) is the usual idiom, but on Windows it is the wrong tool:
    os.kill maps to TerminateProcess for any signal other than the console
    events, and against a pid that has already gone it raises in a way that
    escapes an `except OSError` - which took the whole status endpoint down
    with a 500. OpenProcess only asks a question.
    """
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    return True


_REMAIN_CACHE: dict = {"key": None, "value": 0}


def _remaining_count() -> int:
    """Rows of the last sheet with no record yet - what Resume would work on.

    Cached against the sheet and the history file so the status poll is not
    re-parsing a spreadsheet every few seconds.
    """
    path = _last_input()
    if path is None:
        return 0
    try:
        key = (str(path), path.stat().st_mtime, STATE_FILE.stat().st_mtime
               if STATE_FILE.exists() else 0)
    except OSError:
        return 0
    if _REMAIN_CACHE["key"] == key:
        return _REMAIN_CACHE["value"]

    try:
        from agent.sheet import load_rows
        from agent.state import norm_site
        rows, _ = load_rows(path)
        data = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
        scope = str(cfg.path("run", "dedupe_scope", default="host"))
        seen = {norm_site(u, scope) for u in data}
        value = sum(1 for r in rows if norm_site(r["website"], scope) not in seen)
    except Exception:
        value = 0
    _REMAIN_CACHE.update(key=key, value=value)
    return value


def _dry_run_count() -> int:
    """Rows filled but never submitted - what "go live" would actually act on."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return sum(1 for r in data.values() if r.get("status") == "dry_run")


def _status() -> dict:
    proc = STATE["proc"]
    running = (proc is not None and proc.poll() is None) or _external_run_active()
    code = None if proc is None else proc.poll()
    return {
        "problem": _last_problem() if (code not in (0, None)) else "",
        "dry_runs": _dry_run_count(),
        "remaining": _remaining_count(),
        "bounce": dict(BOUNCE),
        "restarts": STATE.get("restarts", 0),
        "can_go_live": _last_input() is not None and not running,
        "running": running,
        "returncode": None if proc is None else proc.poll(),
        "input": STATE["input"],
        "live": STATE["live"],
        "script": _last_script(),
        "script_label": _script_label(_last_script()),
        "started_at": STATE["started_at"],
        "report_url": _run_report_url(),
        "report_ready": REPORT_PATH.exists(),
    }


@app.get("/scripts")
def scripts_list():
    """Which pitch a run can be sent with, for the console's picker."""
    catalogue, default_key = _script_catalogue()
    last = _last_script()
    known = {s["key"] for s in catalogue}
    return jsonify(scripts=catalogue, default=default_key,
                   selected=last if last in known else default_key)


@app.post("/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
       return jsonify(error="no file received"), 400
    name = secure_filename(f.filename)
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        return jsonify(error=f"unsupported file type {ext!r} - use .xlsx/.xls/.csv"), 400
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamped = f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
    dest = UPLOAD_DIR / stamped
    f.save(dest)

    preview, already = _preview_rows(dest)
    STATE["preview"] = preview
    STATE["preview_path"] = str(dest)
    return jsonify(path=str(dest), name=stamped,
                   rows=len(preview), already=already)


def _preview_rows(path) -> tuple[list[dict], int]:
    """The uploaded sheet as dashboard rows, before anything has been run.

    A row already in the history is shown with the outcome it will get -
    skipped - so the count of what is actually pending is honest.
    """
    try:
        from agent.sheet import load_rows
        from agent.state import norm_site
        rows, _ = load_rows(path)
        data = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    except Exception:
        return [], 0

    scope = str(cfg.path("run", "dedupe_scope", default="host"))
    contacted = {}
    for url, rec in data.items():
        if rec.get("status") in ("sent", "success", "uncertain"):
            contacted[norm_site(url, scope)] = rec

    out = []
    for r in rows:
        prior = contacted.get(norm_site(r["website"], scope))
        out.append({
            "website": r["website"], "company": r["company_name"],
            "method": "",
            "status": "pending",
            "detail": (f"already contacted by {prior.get('method')} on "
                       f"{str(prior.get('timestamp'))[:10]} - will be skipped"
                       if prior else "not processed yet"),
            "email_used": (prior or {}).get("email_used", ""),
            "contact_page": (prior or {}).get("contact_page", ""),
            "sender": (prior or {}).get("sender", ""),
            "timestamp": str((prior or {}).get("timestamp", "")),
            "shot_before": "", "shot_after": "",
        })
    already = sum(1 for r in rows
                  if contacted.get(norm_site(r["website"], scope)) is not None)
    return out, already


@app.post("/start")
def start():
    with _lock:
        proc = STATE["proc"]
        if (proc is not None and proc.poll() is None) or _external_run_active():
            return jsonify(error="a run is already in progress"), 409

        data = request.get_json(force=True, silent=True) or {}
        input_path = Path(str(data.get("input", "")))
        try:
            input_path = input_path.resolve()
            input_path.relative_to(UPLOAD_DIR.resolve())
        except (ValueError, OSError):
            return jsonify(error="input file must be one uploaded via /upload"), 400
        if not input_path.exists():
            return jsonify(error="uploaded file no longer exists"), 400

        live = bool(data.get("live"))
        headless = bool(data.get("headless", True))
        limit = int(data.get("limit") or 0)

        # Checked here as well as in run.py: a rejected pitch should come back
        # as a message in the console, not as a subprocess that exits 2 and
        # leaves the dashboard looking like the run merely failed.
        catalogue, default_key = _script_catalogue()
        script = str(data.get("script") or "").strip() or default_key
        if script not in {s["key"] for s in catalogue}:
            return jsonify(error=f"unknown pitch script {script!r}"), 400

        return start_run(input_path, live, headless, limit, script=script)


# A ceiling, not an expectation - but it has to scale with the sheet. Batch1
# needed 4 restarts for 467 rows (~1 per 120), so a 3000-row sheet needs ~26
# and would have died just short of finishing under the old fixed 25. The
# startup-failure guard in _supervise stops a hot loop, so a high ceiling is
# safe: this exists to catch "restarting is not helping", not to cap a long run.
MAX_RESTARTS = int(cfg.path("run", "max_restarts", default=200))


def _spawn(argv, log_fh):
    log_fh.write(f"\n--- launched {datetime.now():%Y-%m-%d %H:%M:%S} :: {' '.join(argv)} ---\n")
    log_fh.flush()
    return subprocess.Popen(argv, cwd=str(ROOT), stdout=log_fh, stderr=subprocess.STDOUT)


def _last_progress_at() -> float:
    """When the run last wrote anything. 0 if it never has."""
    newest = 0.0
    rows_path = cfg.resolve(cfg.path("paths", "rows_json_path", default="output/run_rows.json"))
    for path in (STATE_FILE, rows_path, LAUNCH_LOG):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


CURRENT_SITE_RE = re.compile(r"\[\d+/\d+\]\s+(\S+)\s*$")


def _site_in_flight() -> str:
    """The site the run announced most recently - the one it is stuck on."""
    try:
        tail = RUN_LOG.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        m = CURRENT_SITE_RE.search(line)
        if m:
            return m.group(1)
    return ""


WORKER_LINE_RE = re.compile(
    r"^\[[\d:]+\]\s+(w\d+)\s+(?:\[\d+/\d+\]\s+(\S+)|\s*->)")


def _sites_in_flight() -> list[str]:
    """Every site a worker announced but never reported a result for.

    With one worker that is just the last site announced. With several, each
    can be wedged on a different page, and every one of them has to be marked
    or the resume walks straight back into them. A worker whose most recent
    line is an announcement is still on that site; one whose most recent line
    is a result has finished it.
    """
    try:
        tail = RUN_LOG.read_text(encoding="utf-8", errors="replace")[-200_000:]
    except OSError:
        return []
    latest: dict[str, str] = {}      # worker -> the site it is on, "" once done
    for line in tail.splitlines():
        m = WORKER_LINE_RE.match(line)
        if m:
            latest[m.group(1)] = m.group(2) or ""
    urls = [u for u in latest.values() if u]
    # a single-worker run carries no "wN" tag at all, so fall back to the
    # last site the log announced
    return urls or [u for u in (_site_in_flight(),) if u]


def _last_input_name() -> str:
    """Filename of the sheet currently being run, "" if that is not knowable."""
    try:
        path = STATE.get("input_path") or _last_input()
        return Path(str(path)).name if path else ""
    except Exception:
        return ""


def _record_stalled_site(url: str) -> None:
    """Mark a site that wedged the browser so a resume steps past it.

    Without this the restart lands on the same site, wedges again, and the
    supervisor loops until it hits the restart ceiling - the run never moves.
    """
    if not url:
        return
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
        if state.get(url, {}).get("status") in ("sent", "success", "uncertain"):
            return                      # already reached; leave that record alone
        prior = state.get(url, {})
        state[url] = {
            "row_index": prior.get("row_index", 0),
            "website": url,
            "company_name": prior.get("company_name", ""),
            "method": "none", "status": "timeout",
            "detail": "the browser wedged here and the run had to be restarted - skipped",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            # Carried from the row's own earlier record. Without it a wedged
            # site drops out of every per-sheet report - the sheet it came from
            # is the one field this hand-built entry cannot reconstruct.
            "sheet": prior.get("sheet", _last_input_name()),
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        pass


def _wait_or_stall(proc, log_fh, stall_after: float) -> bool:
    """Wait for the run to end. True if it was killed for going quiet.

    A site can legitimately take minutes - a slow page, retries, the pause
    between sites - so this is deliberately patient. It is here for the case
    where nothing is happening at all.
    """
    while proc.poll() is None:
        time.sleep(10)
        if STATE.get("stopped_by_user"):
            return False
        last = _last_progress_at()
        if last and (time.time() - last) > stall_after:
            mins = stall_after / 60
            stuck_on = _site_in_flight()
            log_fh.write(f"--- no progress for {mins:.0f} minutes; wedged on "
                         f"{stuck_on or 'an unknown site'}, killing it so it can resume ---\n")
            log_fh.flush()
            _record_stalled_site(stuck_on)
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait()
            return True
    return False


def _supervise(argv, log_fh):
    """Keep the run going until it finishes or is stopped.

    Two failure modes to cover: the process dying outright (the lock file is
    left behind), and the process wedging with nothing to report (caught by
    the stall watch above). Either way it is restarted and continues.
    """
    restarts = 0
    last_spawn = time.time()
    stall_after = float(cfg.path("run", "stall_timeout_s", default=600))
    # a restart should carry on, not redo the rows already attempted
    if "--continue-batch" not in argv:
        argv = argv + ["--continue-batch"]
    try:
        while True:
            proc = STATE["proc"]
            stalled = _wait_or_stall(proc, log_fh, stall_after)
            if stalled:
                try:
                    LOCK_FILE.unlink(missing_ok=True)
                except OSError:
                    pass

            if STATE.get("stopped_by_user"):
                log_fh.write("--- stopped by user; not restarting ---\n")
                break
            # The exit code is the verdict here, not the lock file. run.py
            # leaves the lock behind on a watchdog kill precisely so this loop
            # can tell a crash from a finish - but /status calls
            # _external_run_active() every few seconds and unlinks it as stale
            # first, so the signal was usually gone before this loop next woke.
            # Whether a wedged run resumed by itself came down to which of the
            # two woke first, which is why it kept needing a restart by hand.
            code = proc.returncode
            if not stalled and code == 0:
                log_fh.write("--- run finished cleanly ---\n")
                break

            # A run that dies within seconds having done nothing is broken in a
            # way restarting cannot fix - a bad config, a missing input file.
            # Restarting it 25 times just fills the log.
            if not stalled and time.time() - last_spawn < 30:
                log_fh.write(f"--- exited ({code}) after "
                             f"{time.time() - last_spawn:.0f}s without starting "
                             f"work; that is a startup failure, not a wedge - "
                             f"not restarting ---\n")
                break

            # A watchdog kill (exit 3) leaves every worker's current site
            # unrecorded, so a plain resume runs back into the same pages and
            # wedges on them again. Mark all of them before respawning.
            if stalled or code == 3:
                for url in _sites_in_flight():
                    _record_stalled_site(url)

            if restarts >= MAX_RESTARTS:
                log_fh.write(f"--- died again; restart limit ({MAX_RESTARTS}) reached, giving up ---\n")
                break

            restarts += 1
            try:
                LOCK_FILE.unlink(missing_ok=True)   # the dead run's lock
            except OSError:
                pass
            log_fh.write(f"--- {'wedged' if stalled else 'died unexpectedly'}; "
                         f"restart {restarts}/{MAX_RESTARTS}, resuming from state.json ---\n")
            log_fh.flush()
            with _lock:
                STATE["proc"] = _spawn(argv, log_fh)
                STATE["restarts"] = restarts
                last_spawn = time.time()
    finally:
        try:
            log_fh.flush()
        except Exception:
            pass


def start_run(input_path: Path, live: bool, headless: bool, limit: int,
              continue_batch: bool = False, script: str = ""):
    """Launch run.py under supervision. Caller owns the "already running" check."""
    argv = [sys.executable, "run.py", "--input", str(input_path), "--yes"]
    if script:
        argv += ["--script", script]
    if continue_batch:
        argv.append("--continue-batch")
    if live:
        argv.append("--live")
    if not headless:
        argv.append("--no-headless")
    if limit:
        argv += ["--limit", str(limit)]

    LAUNCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(LAUNCH_LOG, "a", encoding="utf-8")
    try:
        LOCK_FILE.unlink(missing_ok=True)     # clear any lock left by an earlier crash
    except OSError:
        pass

    proc = _spawn(argv, log_fh)
    # Persist the mode: STATE lives in memory, so after a service restart a
    # Resume was defaulting to a dry run and silently sending nothing.
    try:
        MODE_FILE.write_text(json.dumps({"live": bool(live), "input": str(input_path),
                                         "script": script or ""}),
                             encoding="utf-8")
    except OSError:
        pass
    STATE["preview"] = None          # live results take over from here
    STATE.update(proc=proc, input=input_path.name, input_path=str(input_path),
                 live=live, script=script or None,
                 started_at=datetime.now().isoformat(timespec="seconds"),
                 stopped_by_user=False, restarts=0)
    threading.Thread(target=_supervise, args=(argv, log_fh), daemon=True).start()
    return jsonify(_status())


@app.post("/go-live")
def go_live():
    """Re-run the sheet from the last run with live mode on.

    Dry-run rows are not treated as completed attempts, so they are picked up
    again; anything already sent or submitted is skipped by the duplicate
    check, so this cannot re-contact someone.
    """
    with _lock:
        proc = STATE["proc"]
        if (proc is not None and proc.poll() is None) or _external_run_active():
            return jsonify(error="a run is already in progress"), 409
        path = _last_input()
        if path is None:
            return jsonify(error="no previous run to promote - upload a sheet and run it first"), 400

    return start_run(path, live=True, headless=True, limit=0, script=_last_script())


@app.post("/clear-drafts")
def clear_drafts():
    """Remove dry-run rows from the history. Never touches a real contact.

    state.json is the only record of who has actually been written to, so this
    filters by status rather than deleting the file, and takes a timestamped
    backup first. Anything sent, submitted or skipped as a duplicate stays.
    """
    with _lock:
        proc = STATE["proc"]
        if (proc is not None and proc.poll() is None) or _external_run_active():
            return jsonify(error="a run is in progress - wait for it to finish before clearing drafts"), 409

    if not STATE_FILE.exists():
        return jsonify(removed=0, kept=0, backup="", note="nothing to clear")

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=f"could not read the history: {exc}"), 500

    drafts = {u: r for u, r in data.items() if r.get("status") == "dry_run"}
    kept = {u: r for u, r in data.items() if r.get("status") != "dry_run"}
    if not drafts:
        return jsonify(removed=0, kept=len(kept), backup="", note="no drafts to clear")

    backup = STATE_FILE.with_name(f"state-backup-{datetime.now():%Y%m%d_%H%M%S}.json")
    try:
        backup.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(kept, indent=2, default=str), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        return jsonify(error=f"could not write the history: {exc}"), 500

    # The dashboards and ledger are derived from state, so rebuild them.
    try:
        from agent.ledger import build_ledger
        from agent.report import build_report
        rows = list(kept.values())
        build_report(rows, "dry_run", REPORT_PATH)
        build_report(rows, "dry_run",
                     cfg.resolve(cfg.path("paths", "report_all_path", default="output/report_all.html")))
        build_ledger(rows, cfg.resolve(cfg.path("paths", "ledger_path", default="output/contacted.xlsx")))
    except Exception:
        pass   # the history is already saved; a stale view is not worth a 500

    contacted = sum(1 for r in kept.values() if r.get("status") in ("sent", "success", "uncertain"))
    return jsonify(removed=len(drafts), kept=len(kept), contacted=contacted, backup=backup.name)


BOUNCE: dict = {"running": False, "done_at": None, "checked": 0,
                "bounced": 0, "error": "", "addresses": []}


def _bounce_worker() -> None:
    """Find undeliverable addresses and mark those rows failed.

    Same rules as the command line: read-only on the mailboxes, only addresses
    this agent sent to, hard failures only. A backup of the history is written
    before anything changes.
    """
    from agent.bounces import check_all
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        by_address: dict[str, list[str]] = {}
        for url, row in state.items():
            if row.get("status") == "sent" and row.get("email_used"):
                by_address.setdefault(str(row["email_used"]).lower(), []).append(url)

        BOUNCE["checked"] = len(by_address)
        if not by_address:
            BOUNCE.update(running=False, bounced=0, addresses=[],
                          done_at=datetime.now().isoformat(timespec="seconds"))
            return

        bounced, problems = check_all(cfg, set(by_address))
        if bounced:
            backup = STATE_FILE.with_name(
                f"state-backup-bounces-{datetime.now():%Y%m%d_%H%M%S}.json")
            backup.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
            for addr, why in bounced.items():
                for url in by_address[addr]:
                    row = state[url]
                    row["status"] = "failed"
                    row["detail"] = (f"email to {addr} bounced - undeliverable "
                                     f"({why[:60]}). " + str(row.get("detail", "")))[:600]
                    row["bounced_at"] = datetime.now().isoformat(timespec="seconds")
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
            tmp.replace(STATE_FILE)

        BOUNCE.update(running=False, bounced=len(bounced),
                      addresses=sorted(bounced)[:40],
                      error="; ".join(problems)[:200],
                      done_at=datetime.now().isoformat(timespec="seconds"))
    except Exception as exc:  # noqa: BLE001
        BOUNCE.update(running=False, error=f"{type(exc).__name__}: {exc}"[:200],
                      done_at=datetime.now().isoformat(timespec="seconds"))


@app.post("/check-bounces")
def check_bounces():
    """Start the bounce check. Returns immediately; poll /status for the result."""
    with _lock:
        proc = STATE["proc"]
        if (proc is not None and proc.poll() is None) or _external_run_active():
            return jsonify(error="a run is in progress - it is writing the history"), 409
        if BOUNCE["running"]:
            return jsonify(error="a bounce check is already running"), 409
        BOUNCE.update(running=True, bounced=0, checked=0, error="", addresses=[], done_at=None)
    threading.Thread(target=_bounce_worker, daemon=True).start()
    return jsonify(started=True)


@app.post("/resume")
def resume():
    """Carry on with the last sheet, skipping every row already attempted.

    Live mode is taken from the run being continued, so resuming a live batch
    stays live and resuming a dry run stays a dry run.
    """
    with _lock:
        proc = STATE["proc"]
        if (proc is not None and proc.poll() is None) or _external_run_active():
            return jsonify(error="a run is already in progress"), 409
        path = _last_input()
        if path is None:
            return jsonify(error="nothing to resume - upload a sheet and run it first"), 400
        live = STATE.get("live")
        if live is None or not STATE.get("input_path"):
            # not this process's run - recover the mode from disk
            try:
                live = bool(json.loads(MODE_FILE.read_text(encoding="utf-8")).get("live"))
            except Exception:
                live = False
        live = bool(live)
        script = _last_script()

    return start_run(path, live=live, headless=True, limit=0, continue_batch=True,
                     script=script)


@app.post("/stop")
def stop():
    STATE["stopped_by_user"] = True
    with _lock:
        proc = STATE["proc"]
        if proc is not None and proc.poll() is None:
            proc.terminate()
        return jsonify(_status())


@app.get("/status")
def status():
    return jsonify(_status())


@app.get("/outcomes.xlsx")
def outcomes_xlsx():
    """Every row and why it ended where it did, as a downloadable workbook.

    ?sheet=<filename> limits it to one input sheet; omitted, it covers every
    sheet ever run. Rebuilt on request from state.json, so it is never stale.
    """
    from agent.outcomes import build_outcomes
    sheet = (request.args.get("sheet") or "").strip()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return "no history yet", 404
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", sheet).rsplit(".", 1)[0] if sheet else "all_sheets"
    out = REPORT_PATH.parent / f"outcomes_{label}_{stamp}.xlsx"
    build_outcomes(list(data.values()), out, sheet_filter=sheet)
    return send_from_directory(out.parent, out.name, as_attachment=True)


@app.get("/files/<path:relpath>")
def files(relpath):
    target = (ROOT / relpath).resolve()
    allowed_roots = (REPORT_PATH.resolve().parent, EVIDENCE_DIR.resolve())
    if not any(target == r or target.is_relative_to(r) for r in allowed_roots):
        return "forbidden", 403
    return send_from_directory(target.parent, target.name)


CONTACTED_CSS = """
  /* Dark by default, matching the console. The console's toggle wins over the
     operating system, so this keys off data-theme rather than a media query. */
  :root {
    --bg: #05070F; --surface: rgba(17,22,41,.72); --raised: rgba(23,29,52,.6);
    --ink: #E8ECF8; --ink-2: #97A2C0; --ink-3: #5F6B8C;
    --edge: rgba(129,140,248,.16); --accent: #818CF8;
    --ok-bg: rgba(16,185,129,.14); --ok-fg: #6EE7B7; --ok-br: rgba(16,185,129,.34);
    --warn-bg: rgba(245,158,11,.14); --warn-fg: #FCD34D; --warn-br: rgba(245,158,11,.34);
  }
  :root[data-theme="light"] {
    --bg: #F4F6FC; --surface: #FFFFFF; --raised: #F7F9FE;
    --ink: #0F1730; --ink-2: #4A5578; --ink-3: #8A93B0;
    --edge: rgba(15,23,42,.10); --accent: #4F46E5;
    --ok-bg: #D1FAE5; --ok-fg: #065F46; --ok-br: #6EE7B7;
    --warn-bg: #FEF3C7; --warn-fg: #92400E; --warn-br: #FCD34D;
  }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.55 Inter, system-ui, -apple-system, "Segoe UI", sans-serif; }
  .wrap { max-width:1100px; margin:0 auto; padding:28px 20px 64px; }
  a { color:var(--accent); }
  h1 { font-size:24px; margin:0 0 4px; }
  .sub { color:var(--ink-3); margin:0 0 22px; font-size:14px; }
  .tabs { display:flex; gap:6px; border-bottom:1px solid var(--edge); flex-wrap:wrap; }
  .tab { padding:10px 16px; border:1px solid transparent; border-bottom:none; cursor:pointer;
         border-radius:8px 8px 0 0; font:500 14px Inter, system-ui, sans-serif;
         color:var(--ink-3); background:none; transition:color .2s, background .2s; }
  .tab:hover { color:var(--ink); }
  .tab[aria-selected="true"] { background:var(--surface); color:var(--ink); border-color:var(--edge); }
  .panel { background:var(--surface); border:1px solid var(--edge); border-top:none;
           border-radius:0 0 12px 12px; overflow-x:auto; backdrop-filter:blur(12px); }
  table { border-collapse:collapse; width:100%; font-size:13.5px; }
  th { text-align:left; font-size:11px; letter-spacing:.06em; text-transform:uppercase;
       color:var(--ink-3); padding:12px 14px; border-bottom:1px solid var(--edge); white-space:nowrap; }
  td { padding:10px 14px; border-bottom:1px solid var(--edge); vertical-align:top; }
  tr:last-child td { border-bottom:none; }
  tbody tr { transition:background .2s ease; }
  tbody tr:hover { background:rgba(129,140,248,.06); }
  .mono { font-variant-numeric:tabular-nums; color:var(--ink-3); white-space:nowrap; }
  .pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11.5px; font-weight:600;
          border:1px solid transparent; }
  .ok { background:var(--ok-bg); color:var(--ok-fg); border-color:var(--ok-br); }
  .warn { background:var(--warn-bg); color:var(--warn-fg); border-color:var(--warn-br); }
  .empty { padding:36px 16px; text-align:center; color:var(--ink-3); }
  .count { font-weight:400; color:var(--ink-3); }
  .bar { display:flex; justify-content:space-between; align-items:center; gap:12px;
         margin-bottom:18px; flex-wrap:wrap; }
  .btn { display:inline-block; padding:8px 14px; border:1px solid var(--edge); background:var(--surface);
         border-radius:9px; text-decoration:none; font-size:13.5px; color:var(--ink);
         transition:transform .16s ease, border-color .16s ease; }
  .btn:hover { transform:translateY(-1px); border-color:var(--accent); }
  .thumbs { display:flex; gap:8px; }
  .thumb { display:block; width:104px; border:1px solid var(--edge); border-radius:8px;
           overflow:hidden; text-decoration:none; background:var(--raised);
           transition:transform .18s ease, border-color .18s ease, box-shadow .18s ease; }
  .thumb:hover { transform:scale(1.03); border-color:var(--accent);
                 box-shadow:0 8px 22px rgba(79,70,229,.22); }
  .thumb img { display:block; width:100%; height:64px; object-fit:cover; object-position:top; }
  .thumb span { display:block; padding:3px 6px; font-size:10px; letter-spacing:.05em;
                text-transform:uppercase; color:var(--ink-3); }
  .thumb.txt { display:grid; place-items:center; height:64px; }
"""


def _contacted_rows():
    """Everything the agent actually reached, split by how, newest first."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return [], [], []
    reached = [r for r in data.values() if r.get("status") in ("sent", "success", "uncertain")]
    reached.sort(key=lambda r: str(r.get("timestamp", "")), reverse=True)
    missed = [r for r in data.values()
              if r.get("status") in ("no_contact_found", "unreachable",
                                     "failed", "error", "timeout")]
    missed.sort(key=lambda r: str(r.get("timestamp", "")), reverse=True)
    return ([r for r in reached if r.get("method") == "email"],
            [r for r in reached if r.get("method") == "form"],
            missed)


def _esc(value) -> str:
    return html_escape(str(value or ""))


def _shot_url(path) -> str:
    """/files/ URL for an evidence file, or "" if it is gone.

    Runs are re-run and evidence folders get cleaned, so a recorded path is not
    a promise the file still exists.
    """
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        rel = p.resolve().relative_to(ROOT.resolve())
    except (ValueError, OSError):
        return ""
    return "/files/" + str(rel).replace("\\", "/")


def _thumbs(row) -> str:
    """Before/after thumbnails for one row."""
    pairs = [("filled", _shot_url(row.get("screenshot_before"))),
             ("submitted", _shot_url(row.get("screenshot_after")))]
    cells = []
    for label, url in pairs:
        if not url:
            continue
        if url.lower().endswith(".png"):
            cells.append(
                f'<a class="thumb" href="{_esc(url)}" target="_blank" rel="noopener" '
                f'title="{_esc(label)}"><img src="{_esc(url)}" alt="{_esc(label)}" loading="lazy">'
                f'<span>{_esc(label)}</span></a>')
        else:
            cells.append(f'<a class="thumb txt" href="{_esc(url)}" target="_blank" '
                         f'rel="noopener"><span>{_esc(label)} (text)</span></a>')
    if not cells:
        return '<span class="mono">-</span>'
    return '<div class="thumbs">' + "".join(cells) + '</div>'



def _when(row) -> str:
    return _esc(str(row.get("timestamp"))[:16].replace("T", " "))


@app.get("/assets/<path:name>")
def assets(name):
    """Static files for the console (the logo). Confined to ui/assets."""
    root = (ROOT / "ui" / "assets").resolve()
    target = (root / name).resolve()
    if not (target == root or target.is_relative_to(root)) or not target.exists():
        return "not found", 404
    return send_from_directory(target.parent, target.name)


@app.get("/api/rows")
def api_rows():
    """Read-only feed for the console: this sheet's rows plus lifetime totals.

    Additive - nothing here writes, and every number comes from the files the
    agent already produces.
    """
    rows, meta = [], {}
    preview = STATE.get("preview")
    if preview:
        # a sheet has been uploaded but not run: show it rather than the last run
        rows = preview
        # nothing has run yet, so every counter starts at zero
        meta = {"mode": "preview", "total": len(preview), "done": 0, "generated": ""}
    else:
        rows_path = cfg.resolve(cfg.path("paths", "rows_json_path", default="output/run_rows.json"))
        try:
            payload = json.loads(rows_path.read_text(encoding="utf-8"))
            rows = payload.get("rows", [])
            meta = {k: payload.get(k) for k in ("mode", "total", "done", "generated")}
        except Exception:
            pass

    overall = {"sites": 0, "emails": 0, "forms": 0, "by_status": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        overall["sites"] = len(data)
        for r in data.values():
            st = str(r.get("status") or "unknown")
            overall["by_status"][st] = overall["by_status"].get(st, 0) + 1
            if r.get("status") in ("sent", "success", "uncertain"):
                if r.get("method") == "email":
                    overall["emails"] += 1
                elif r.get("method") == "form":
                    overall["forms"] += 1
    except Exception:
        pass

    def shot(path):
        """Turn an absolute evidence path into something the browser can fetch."""
        if not path:
            return ""
        try:
            rel = Path(path).resolve().relative_to(ROOT.resolve())
        except (ValueError, OSError):
            return ""
        return "/files/" + str(rel).replace("\\", "/")

    slim = []
    for r in rows:
        slim.append({
            "website": r.get("website", ""), "company": r.get("company_name", ""),
            "method": r.get("method", ""), "status": r.get("status", ""),
            "detail": r.get("detail", ""), "email_used": r.get("email_used", ""),
            "contact_page": r.get("contact_page", ""), "sender": r.get("sender", ""),
            "timestamp": str(r.get("timestamp", "")),
            "shot_before": shot(r.get("screenshot_before", "")),
            "shot_after": shot(r.get("screenshot_after", "")),
        })
    return jsonify(rows=slim, meta=meta, overall=overall)


REACHED_STATUSES = ("sent", "success", "uncertain")
TRIED_STATUSES = ("failed", "no_contact_found", "timeout", "error", "dry_run")


def _coverage(path):
    """Classify every row of a sheet against the contact history."""
    from agent.sheet import load_rows
    from agent.state import norm_site

    scope = str(cfg.path("run", "dedupe_scope", default="host"))
    rows, skipped = load_rows(path)
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    by_host: dict[str, dict] = {}
    for url, rec in state.items():
        key = norm_site(url, scope)
        prior = by_host.get(key)
        # a real contact always wins over an earlier attempt at the same site
        if prior is None or (rec.get("status") in REACHED_STATUSES
                             and prior.get("status") not in REACHED_STATUSES):
            by_host[key] = rec

    out = []
    for r in rows:
        rec = by_host.get(norm_site(r["website"], scope))
        status = (rec or {}).get("status")
        method = (rec or {}).get("method", "")
        if status in REACHED_STATUSES or status == "skipped_duplicate":
            # skipped_duplicate means it had already been reached elsewhere
            bucket = "form" if method == "form" else "email"
        elif status in ("failed", "error", "timeout"):
            bucket = "failed"
        elif status in ("no_contact_found", "unreachable"):
            bucket = "noroute"
        elif rec:
            bucket = "never"           # rehearsed only - never actually sent
        else:
            bucket = "never"
        out.append({
            "website": r["website"], "company": r["company_name"],
            "bucket": bucket, "reached": bucket in ("form", "email"),
            "method": method, "status": status or "never attempted",
            "why": _why_not(rec) if rec and bucket in ("failed", "noroute") else "",
            "via": (rec or {}).get("email_used") or (rec or {}).get("contact_page") or "",
            "sender": (rec or {}).get("sender_email") or (rec or {}).get("sender") or "",
            "when": str((rec or {}).get("timestamp", ""))[:16].replace("T", " "),
        })
    return out, skipped


@app.post("/coverage")
def coverage_check():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="no file received"), 400
    name = secure_filename(f.filename)
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        return jsonify(error=f"unsupported file type - use {', '.join(sorted(ALLOWED_EXT))}"), 400
    # Deliberately NOT in UPLOAD_DIR: Resume and "submit live" target the newest
    # upload, and a sheet dropped here is being checked, not queued for sending.
    check_dir = UPLOAD_DIR / "coverage"
    check_dir.mkdir(parents=True, exist_ok=True)
    dest = check_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
    f.save(dest)

    try:
        rows, skipped = _coverage(dest)
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=f"could not read that sheet: {exc}"), 400

    # the unreached ones, ready to upload and run
    unreached = [r for r in rows if not r["reached"]]
    out_path = cfg.resolve("output") / f"unreached_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    try:
        import pandas as pd
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"Website": r["website"], "Company Name": r["company"],
                       "Why not reached": r["why"] or r["status"]}
                      for r in unreached]).to_excel(out_path, index=False)
        STATE["coverage_download"] = str(out_path)
    except Exception:
        STATE["coverage_download"] = ""

    counts = {b: sum(r["bucket"] == b for r in rows)
              for b in ("form", "email", "failed", "noroute", "never")}
    counts["reached"] = counts["form"] + counts["email"]
    return jsonify(rows=rows, counts=counts, total=len(rows),
                   skipped=len(skipped), sheet=name,
                   download=bool(STATE.get("coverage_download")))


@app.get("/coverage/download")
def coverage_download():
    path = STATE.get("coverage_download")
    if not path or not Path(path).exists():
        return "nothing to download - run a check first", 404
    p = Path(path)
    return send_from_directory(p.parent, p.name, as_attachment=True)


@app.get("/coverage")
def coverage_page():
    return COVERAGE_HTML.replace("{css}", CONTACTED_CSS)


COVERAGE_HTML = """<!doctype html>
<meta charset="utf-8"><title>Coverage check - outreach agent</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}
  .drop { border:1.5px dashed var(--edge); border-radius:14px; padding:34px 18px;
          text-align:center; cursor:pointer; transition:border-color .2s, background .2s; }
  .drop:hover, .drop.drag { border-color:var(--accent); background:rgba(99,102,241,.06); }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
           gap:12px; margin:22px 0 18px; }
  .tile { border:1px solid var(--edge); border-radius:12px; padding:14px 16px; background:var(--surface);
          text-align:left; cursor:pointer; font:inherit; color:inherit;
          transition:transform .16s ease, border-color .16s ease, box-shadow .16s ease; }
  .tile:hover { transform:translateY(-2px); border-color:var(--accent); }
  .tile[aria-pressed="true"] { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent) inset; }
  .v-ok { color:#10B981; } .v-tot { color:#06B6D4; }
  .v-bad { color:#F43F5E; } .v-warn { color:#F59E0B; }
  .tile .k { font-size:10.5px; letter-spacing:.09em; text-transform:uppercase; color:var(--ink-3); }
  .tile .v { font-size:27px; font-weight:700; margin-top:4px; font-variant-numeric:tabular-nums; }
  .v-reached { color:#10B981; } .v-tried { color:#F59E0B; } .v-never { color:#F43F5E; }
  .filters { display:flex; gap:7px; margin-bottom:12px; flex-wrap:wrap; }
  .filters button { padding:7px 13px; border-radius:999px; font-size:12.5px; cursor:pointer;
                    border:1px solid var(--edge); background:var(--surface); color:var(--ink-2); }
  .filters button[aria-pressed="true"] { background:var(--accent); color:#fff; border-color:transparent; }
  .hidden { display:none; }
</style>
<script>
  (function () {
    try {
      var t = localStorage.getItem('webifyTheme');
      document.documentElement.setAttribute('data-theme', t === 'light' ? 'light' : 'dark');
    } catch (e) { document.documentElement.setAttribute('data-theme', 'dark'); }
  })();
</script>
<div class="wrap">
  <div class="bar">
    <div>
      <h1>Coverage check</h1>
      <p class="sub">Upload a sheet to see which of its sites were actually reached - by form or by email - and which were not.</p>
    </div>
    <div>
      <a class="btn" href="/contacted">Already contacted</a>
      <a class="btn" href="/">&larr; Back to runs</a>
    </div>
  </div>

  <div class="drop" id="drop" tabindex="0" role="button">
    <strong>Drop a spreadsheet here</strong> or click to choose
    <div class="sub" style="margin:6px 0 0">.xlsx, .xls or .csv &middot; nothing is contacted, this only reads</div>
  </div>
  <input type="file" id="file" accept=".xlsx,.xls,.csv,.tsv" hidden>

  <div id="out" class="hidden">
    <div class="tiles">
      <button class="tile pick" data-f="form"><div class="k">Reached by form</div><div class="v v-ok" id="c-form">0</div></button>
      <button class="tile pick" data-f="email"><div class="k">Reached by email</div><div class="v v-ok" id="c-email">0</div></button>
      <button class="tile pick" data-f="reached"><div class="k">Reached total</div><div class="v v-tot" id="c-reached">0</div></button>
      <button class="tile pick" data-f="failed"><div class="k">Failed or bounced</div><div class="v v-bad" id="c-failed">0</div></button>
      <button class="tile pick" data-f="noroute"><div class="k">No contact route</div><div class="v v-warn" id="c-noroute">0</div></button>
      <button class="tile pick" data-f="never"><div class="k">Never attempted</div><div class="v v-warn" id="c-never">0</div></button>
      <button class="tile pick" data-f="all" aria-pressed="true"><div class="k">All rows</div><div class="v" id="c-total">0</div></button>
    </div>

    <div class="filters">
      <span id="showing" class="sub" style="margin:0"></span>
      <span style="flex:1 1 auto"></span>
      <a class="btn" id="dl" href="/coverage/download">Download the ones not reached</a>
    </div>

    <div class="panel" style="border-top:1px solid var(--edge); border-radius:12px;">
      <table><thead><tr><th>Website</th><th>Company</th><th>Outcome</th>
        <th>How</th><th>Address / form page used</th><th>When</th></tr></thead>
        <tbody id="rows"></tbody></table>
    </div>
  </div>
</div>
<script>
  const $ = (id) => document.getElementById(id);
  const drop = $('drop'), file = $('file');
  let all = [];

  drop.addEventListener('click', () => file.click());
  drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') file.click(); });
  ['dragover','dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('drag'); }));
  ['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('drag'); }));
  drop.addEventListener('drop', e => { if (e.dataTransfer.files[0]) send(e.dataTransfer.files[0]); });
  file.addEventListener('change', () => { if (file.files[0]) send(file.files[0]); });

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"]/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  }

  async function send(f) {
    drop.innerHTML = '<strong>Checking ' + esc(f.name) + '...</strong>';
    const fd = new FormData(); fd.append('file', f);
    const res = await fetch('/coverage', { method: 'POST', body: fd });
    const d = await res.json();
    if (d.error) { drop.innerHTML = '<strong>' + esc(d.error) + '</strong>'; return; }
    drop.innerHTML = '<strong>' + esc(d.sheet) + '</strong> - ' + d.total +
                     ' rows checked. Drop another to check again.';
    all = d.rows;
    $('c-total').textContent = d.total;
    $('c-form').textContent = d.counts.form;
    $('c-email').textContent = d.counts.email;
    $('c-reached').textContent = d.counts.reached;
    $('c-failed').textContent = d.counts.failed;
    $('c-noroute').textContent = d.counts.noroute;
    $('c-never').textContent = d.counts.never;
    document.querySelectorAll('.tile.pick').forEach(x =>
      x.setAttribute('aria-pressed', x.dataset.f === 'all'));
    $('dl').style.display = d.download ? '' : 'none';
    $('out').classList.remove('hidden');
    render('all');
  }

  const LABEL = { form: 'reached by form', email: 'reached by email',
                  reached: 'reached', failed: 'failed or bounced',
                  noroute: 'no contact route', never: 'never attempted', all: 'all rows' };

  function render(filter) {
    const rows = filter === 'all' ? all
               : filter === 'reached' ? all.filter(r => r.reached)
               : all.filter(r => r.bucket === filter);
    $('showing').textContent = rows.length + ' of ' + all.length + ' - ' + LABEL[filter];
    $('rows').innerHTML = rows.length ? rows.map(r => {
      const pill = r.reached ? 'ok' : 'warn';
      return '<tr><td><a href="' + esc(r.website) + '" target="_blank" rel="noopener">' +
        esc(r.website.replace(/^https?:\/\//, '')) + '</a></td>' +
        '<td>' + esc(r.company) + '</td>' +
        '<td><span class="pill ' + pill + '">' + esc(r.status) + '</span>' +
          (r.why ? '<div class="sub" style="margin:4px 0 0">' + esc(r.why) + '</div>' : '') + '</td>' +
        '<td>' + esc(r.method || '-') + '</td>' +
        '<td class="mono">' + esc((r.via || '').replace(/^https?:\/\//, '').slice(0, 44)) + '</td>' +
        '<td class="mono">' + esc(r.when) + '</td></tr>';
    }).join('') : '<tr><td colspan="6"><div class="empty">Nothing in this group.</div></td></tr>';
  }

  document.querySelectorAll('.tile.pick').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.tile.pick').forEach(x => x.setAttribute('aria-pressed', x === b));
      render(b.dataset.f);
    });
  });
</script>
"""


@app.get("/overall")
def overall():
    """Totals across every sheet ever run, kept apart from the per-sheet view."""
    path = cfg.resolve(cfg.path("paths", "report_all_path", default="output/report_all.html"))
    if not path.exists():
        body = ('<div class="empty" style="padding:60px 20px;text-align:center;color:#6b7280;">'
                'No overall dashboard yet - it is written when a run finishes.</div>')
    else:
        body = ('<iframe src="/files/output/report_all.html" '
                'style="width:100%;height:calc(100vh - 150px);border:1px solid rgba(128,128,128,.28);'
                'border-radius:10px;background:#fff;"></iframe>')
    return OVERALL_HTML.replace("{css}", CONTACTED_CSS).replace("{body}", body)


OVERALL_HTML = """<!doctype html>
<meta charset="utf-8"><title>Overall totals - outreach agent</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}</style>
<script>
  /* The console stores the chosen theme; every page here follows it. Runs
     before paint so there is no flash of the wrong palette. */
  (function () {
    try {
      var t = localStorage.getItem('webifyTheme');
      document.documentElement.setAttribute('data-theme', t === 'light' ? 'light' : 'dark');
    } catch (e) {
      document.documentElement.setAttribute('data-theme', 'dark');
    }
  })();
</script>
<div class="wrap" style="max-width:1240px;">
  <div class="bar">
    <div>
      <h1>Overall totals</h1>
      <p class="sub">Every sheet ever run, combined. The dashboard on the run page shows only the sheet you just uploaded.</p>
    </div>
    <div>
      <a class="btn" href="/contacted">Already contacted</a>
      <a class="btn" href="/">&larr; Back to runs</a>
    </div>
  </div>
  {body}
</div>
"""


WHY_NOT = {
    "no_contact_found": "no form and no address published",
    "unreachable": "site would not load",
    "timeout": "site took too long",
    "error": "something went wrong",
}


def _why_not(row) -> str:
    status = row.get("status")
    if status == "failed":
        detail = str(row.get("detail", ""))
        if "bounced" in detail.lower():
            return "email bounced - address does not exist"
        if "smtp" in detail.lower():
            return "the mail server refused it"
        return "form submission was rejected"
    return WHY_NOT.get(status, status or "")


@app.get("/contacted")
def contacted():
    mails, forms, missed = _contacted_rows()

    if mails:
        mail_rows = "".join(
            '<tr><td><strong>{}</strong></td><td>{}<br><a href="{}" target="_blank" rel="noopener">{}</a></td>'
            '<td>{}</td><td><span class="pill ok">{}</span></td><td>{}</td><td class="mono">{}</td></tr>'.format(
                _esc(r.get("email_used")), _esc(r.get("company_name")),
                _esc(r.get("website")), _esc(r.get("website")),
                _esc(r.get("sender_email")), _esc(r.get("status")), _thumbs(r), _when(r))
            for r in mails)
    else:
        mail_rows = '<tr><td colspan="5"><div class="empty">No emails sent yet.</div></td></tr>'

    if forms:
        form_rows = "".join(
            '<tr><td><strong>{}</strong><br><a href="{}" target="_blank" rel="noopener">{}</a></td>'
            '<td><a href="{}" target="_blank" rel="noopener">{}</a></td><td>{}</td>'
            '<td><span class="pill {}">{}</span></td><td>{}</td><td class="mono">{}</td></tr>'.format(
                _esc(r.get("company_name")), _esc(r.get("website")), _esc(r.get("website")),
                _esc(r.get("contact_page") or r.get("website")),
                _esc(r.get("contact_page") or r.get("website")),
                _esc(r.get("sender")),
                "ok" if r.get("status") == "success" else "warn",
                _esc(r.get("status")), _thumbs(r), _when(r))
            for r in forms)
    else:
        form_rows = '<tr><td colspan="5"><div class="empty">No contact forms submitted yet.</div></td></tr>'

    # Plain substitution, not str.format: the page carries a <script> block
    # whose braces would be read as format fields.
    if missed:
        missed_rows = "".join(
            '<tr><td><strong>{}</strong><br><a href="{}" target="_blank" rel="noopener">{}</a></td>'
            '<td><span class="pill warn">{}</span></td><td>{}</td>'
            '<td class="mono">{}</td></tr>'.format(
                _esc(r.get("company_name")), _esc(r.get("website")), _esc(r.get("website")),
                _esc(r.get("status")), _esc(_why_not(r)), _when(r))
            for r in missed)
    else:
        missed_rows = ('<tr><td colspan="4"><div class="empty">'
                       'Every site with a contact route was reached.</div></td></tr>')

    page = CONTACTED_HTML
    for token, value in (("{css}", CONTACTED_CSS), ("{n_mail}", str(len(mails))),
                         ("{n_form}", str(len(forms))), ("{n_missed}", str(len(missed))),
                         ("{mail_rows}", mail_rows), ("{form_rows}", form_rows),
                         ("{missed_rows}", missed_rows)):
        page = page.replace(token, value)
    return page


CONTACTED_HTML = """<!doctype html>
<meta charset="utf-8"><title>Already contacted - outreach agent</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}</style>
<script>
  /* The console stores the chosen theme; every page here follows it. Runs
     before paint so there is no flash of the wrong palette. */
  (function () {
    try {
      var t = localStorage.getItem('webifyTheme');
      document.documentElement.setAttribute('data-theme', t === 'light' ? 'light' : 'dark');
    } catch (e) {
      document.documentElement.setAttribute('data-theme', 'dark');
    }
  })();
</script>
<div class="wrap">
  <div class="bar">
    <div>
      <h1>Already contacted</h1>
      <p class="sub">Everyone the agent has reached. These are skipped automatically on future runs.</p>
    </div>
    <a class="btn" href="/">&larr; Back to runs</a>
  </div>

  <div class="tabs" role="tablist">
    <button class="tab" role="tab" id="t-mail" aria-selected="true" onclick="pick('mail')">
      Email addresses <span class="count">({n_mail})</span></button>
    <button class="tab" role="tab" id="t-form" aria-selected="false" onclick="pick('form')">
      Contact forms <span class="count">({n_form})</span></button>
    <button class="tab" role="tab" id="t-missed" aria-selected="false" onclick="pick('missed')">
      Not reached <span class="count">({n_missed})</span></button>
  </div>

  <div class="panel" id="p-mail">
    <table><thead><tr><th>Email address</th><th>Company / site</th><th>Sent from</th>
      <th>Status</th><th>Evidence</th><th>When</th></tr></thead><tbody>{mail_rows}</tbody></table>
  </div>
  <div class="panel" id="p-form" hidden>
    <table><thead><tr><th>Company / site</th><th>Form page</th><th>Filled as</th>
      <th>Status</th><th>Evidence</th><th>When</th></tr></thead><tbody>{form_rows}</tbody></table>
  </div>
  <div class="panel" id="p-missed" hidden>
    <table><thead><tr><th>Company / site</th><th>Status</th><th>Why not reached</th>
      <th>Last tried</th></tr></thead><tbody>{missed_rows}</tbody></table>
  </div>
</div>
<script>
  function pick(which) {
    for (const key of ['mail', 'form']) {
      const on = key === which;
      document.getElementById('p-' + key).hidden = !on;
      document.getElementById('t-' + key).setAttribute('aria-selected', on);
    }
    try { localStorage.setItem('contactedTab', which); } catch (e) {}
  }
  try { const t = localStorage.getItem('contactedTab'); if (t) pick(t); } catch (e) {}
</script>
"""


CONSOLE_FILE = ROOT / "ui" / "console.html"


@app.get("/")
def index():
    """The console. Served from ui/console.html so the markup stays editable;
    falls back to the built-in page if that file is ever missing."""
    try:
        return CONSOLE_FILE.read_text(encoding="utf-8")
    except OSError:
        return INDEX_HTML


INDEX_HTML = r"""<!doctype html>
<meta charset="utf-8">
<title>Outreach agent</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  @property --spin-angle { syntax: '<angle>'; inherits: false; initial-value: 0deg; }

  :root {
    color-scheme: light;
    --surface-1: #f1f5f9; --surface-2: #e2e8f0; --plane: #ffffff; --text-1: #0f172a; --text-2: #475569;
    --muted: #94a3b8; --grid: #e2e8f0; --border: rgba(15,23,42,0.08); --accent: #2563eb;
    --accent-2: #0d9488;
    --blob-1: #2563eb; --blob-2: #0d9488; --blob-3: #2563eb; --blob-4: #0d9488;
    --blob-opacity: .10;
    --good: #0ca30c; --critical: #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface-1: #1e293b; --surface-2: #334155; --plane: #0f172a; --text-1: #f8fafc; --text-2: #cbd5e1;
      --muted: #94a3b8; --grid: #334155; --border: rgba(255,255,255,0.08); --accent: #3b82f6;
      --accent-2: #14b8a6;
      --blob-1: #3b82f6; --blob-2: #14b8a6; --blob-3: #3b82f6; --blob-4: #14b8a6;
      --blob-opacity: .16;
      --good: #0ca30c; --critical: #e66767;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1: #1e293b; --surface-2: #334155; --plane: #0f172a; --text-1: #f8fafc; --text-2: #cbd5e1;
    --muted: #94a3b8; --grid: #334155; --border: rgba(255,255,255,0.08); --accent: #3b82f6;
    --accent-2: #14b8a6;
    --blob-1: #3b82f6; --blob-2: #14b8a6; --blob-3: #3b82f6; --blob-4: #14b8a6;
    --blob-opacity: .16;
    --good: #0ca30c; --critical: #e66767;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--plane); color: var(--text-1);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 32px clamp(16px, 4vw, 48px) 64px; position: relative; }

  .bg-blobs { position: fixed; inset: 0; overflow: hidden; z-index: -1; pointer-events: none; }
  .bg-blobs span { position: absolute; border-radius: 50%; filter: blur(64px); opacity: var(--blob-opacity); }
  .bg-blobs span:nth-child(1) { width: 46vw; height: 46vw; background: var(--blob-1); top: -14%; left: -10%; }
  .bg-blobs span:nth-child(2) { width: 38vw; height: 38vw; background: var(--blob-2); bottom: -16%; right: -8%; }
  .bg-blobs span:nth-child(3) { width: 30vw; height: 30vw; background: var(--blob-3); top: 28%; right: 14%; }
  .bg-blobs span:nth-child(4) { width: 24vw; height: 24vw; background: var(--blob-4); bottom: 10%; left: 8%; }
  @media (prefers-reduced-motion: no-preference) {
    .bg-blobs span:nth-child(1) { animation: drift1 28s ease-in-out infinite; }
    .bg-blobs span:nth-child(2) { animation: drift2 34s ease-in-out infinite; }
    .bg-blobs span:nth-child(3) { animation: drift3 24s ease-in-out infinite; }
    .bg-blobs span:nth-child(4) { animation: drift2 30s ease-in-out infinite reverse; }
    @keyframes drift1 { 0%, 100% { transform: translate(0, 0); } 50% { transform: translate(6vw, 8vh) scale(1.05); } }
    @keyframes drift2 { 0%, 100% { transform: translate(0, 0); } 50% { transform: translate(-7vw, -5vh) scale(1.06); } }
    @keyframes drift3 { 0%, 100% { transform: translate(0, 0); } 50% { transform: translate(-5vw, 6vh) scale(1.1); } }
  }

  h1 { font-size: 24px; margin: 0 0 2px; font-weight: 700; display: inline-block;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    background-size: 200% auto; -webkit-background-clip: text; background-clip: text; color: transparent; }
  @media (prefers-reduced-motion: no-preference) {
    h1 { animation: hueflow 7s ease-in-out infinite; }
    @keyframes hueflow { 0%, 100% { background-position: 0% center; } 50% { background-position: 100% center; } }
  }
  .sub { color: var(--text-2); font-size: 13px; margin: 0 0 28px; }

  .live-banner { display: flex; align-items: center; justify-content: space-between; gap: 16px;
    background: var(--surface-1); border: 1.5px solid var(--border); border-radius: 12px;
    padding: 14px 20px; margin-bottom: 20px; transition: background .2s, border-color .2s; }
  .live-banner-label { font-size: 14px; font-weight: 600; }
  .live-banner.is-live { background: color-mix(in srgb, var(--critical) 10%, var(--surface-1));
    border-color: var(--critical); }
  .switch-lg { width: 50px; height: 28px; }
  .switch-lg .knob { width: 24px; height: 24px; }
  .switch-lg input:checked + .track + .knob { transform: translateX(22px); }
  .layout { display: grid; grid-template-columns: minmax(280px, 340px) 1fr; gap: 20px; align-items: start; }
  @media (max-width: 860px) { .layout { grid-template-columns: 1fr; } }
  .card { background: var(--surface-1); border: 1px solid color-mix(in srgb, var(--text-1) 12%, transparent);
    border-radius: 12px; padding: 20px 22px;
    box-shadow: 0 2px 10px rgba(15,23,42,.07), 0 1px 2px rgba(15,23,42,.05); }
  .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-2);
    margin: 0 0 16px; font-weight: 600; }
  @media (prefers-reduced-motion: no-preference) {
    .card, .dash-wrap { animation: rise .5s ease both; }
    .dash-wrap { animation-delay: .08s; }
    @keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  }

  .drop { border: 1.5px dashed var(--border); border-radius: 10px; padding: 24px 16px; text-align: center;
    color: var(--text-2); font-size: 13.5px; cursor: pointer; transition: border-color .15s, background .15s, transform .15s; }
  .drop:hover { transform: translateY(-1px); border-color: var(--accent); }
  .drop.drag { border-color: transparent; background-image:
      linear-gradient(var(--surface-1), var(--surface-1)),
      conic-gradient(from var(--spin-angle, 0deg), var(--accent), var(--accent-2), var(--accent));
    background-origin: border-box; background-clip: padding-box, border-box; }
  @media (prefers-reduced-motion: no-preference) {
    .drop.drag { animation: spin-border 2.4s linear infinite; }
    @keyframes spin-border { to { --spin-angle: 360deg; } }
  }
  .drop strong { color: var(--text-1); }
  .file-chip { margin-top: 10px; font-size: 13px; padding: 6px 10px; background: var(--plane);
    border: 1px solid var(--accent-2); color: var(--accent-2); border-radius: 8px; display: none;
    align-items: center; gap: 8px; font-weight: 600; }
  .file-chip.show { display: flex; }

  fieldset { border: none; padding: 0; margin: 18px 0 0; display: flex; flex-direction: column; gap: 12px; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 13.5px; }
  .row .hint { color: var(--muted); font-size: 12px; }
  input[type=number] { width: 72px; padding: 6px 8px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--plane); color: var(--text-1); font-size: 13px; }
  .switch { position: relative; width: 38px; height: 22px; flex: none; }
  .switch input { opacity: 0; width: 100%; height: 100%; margin: 0; position: absolute; cursor: pointer; }
  .switch .track { position: absolute; inset: 0; background: var(--grid); border: 1px solid var(--muted);
    border-radius: 999px; transition: background .2s, border-color .2s; }
  .switch .knob { position: absolute; top: 2px; left: 2px; width: 18px; height: 18px; border-radius: 50%;
    background: var(--surface-1); box-shadow: 0 1px 2px var(--border); transition: transform .2s cubic-bezier(.34,1.56,.64,1); }
  .switch input:checked + .track { background: var(--accent); border-color: transparent; }
  .switch input:checked + .track + .knob { transform: translateX(16px); }
  #live:checked + .track { background: var(--critical); }
  #headless:checked + .track { background: var(--accent-2); }
  .switch input:focus-visible + .track { outline: 2px solid var(--accent); outline-offset: 2px; }

  button { font: inherit; cursor: pointer; border-radius: 8px; border: 1px solid transparent; padding: 10px 16px;
    font-size: 13.5px; font-weight: 600; }
  button:disabled { cursor: not-allowed; opacity: .5; }
  .btn-primary { background: linear-gradient(135deg, var(--accent), var(--accent-2)); background-size: 160% 160%;
    color: #fff; width: 100%; margin-top: 18px;
    box-shadow: 0 4px 14px color-mix(in srgb, var(--accent) 40%, transparent);
    transition: transform .15s ease, box-shadow .15s ease, background-position .5s ease; }
  .btn-primary:hover:not(:disabled) { transform: translateY(-1px); background-position: 100% 0;
    box-shadow: 0 6px 20px color-mix(in srgb, var(--accent-2) 45%, transparent); }
  .btn-primary:active:not(:disabled) { transform: translateY(0); }
  .btn-stop { background: var(--critical); border-color: transparent; color: #fff; width: 100%; margin-top: 10px;
    box-shadow: 0 4px 14px color-mix(in srgb, var(--critical) 35%, transparent);
    transition: transform .15s ease, box-shadow .15s ease, filter .15s ease; }
  .btn-stop:hover:not(:disabled) { filter: brightness(1.08); transform: translateY(-1px); }
  .btn-stop:active:not(:disabled) { transform: translateY(0); }
  button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .status-pill { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; padding: 4px 10px;
    border-radius: 999px; border: 1px solid var(--border); color: var(--text-2); transition: color .2s; }
  .status-pill .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); position: relative; }
  .status-pill.running { color: var(--accent); }
  .status-pill.running .dot { background: var(--accent); animation: pulse 1.2s ease-in-out infinite; }
  .status-pill.running .dot::after { content: ''; position: absolute; inset: -4px; border-radius: 50%;
    border: 2px solid var(--accent); }
  .status-pill.done { color: var(--good); }
  .status-pill.done .dot { background: var(--good); }
  .status-pill.error { color: var(--critical); }
  .status-pill.error .dot { background: var(--critical); }
  @media (prefers-reduced-motion: no-preference) {
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .3; } }
    .status-pill.running .dot::after { animation: ring 1.4s ease-out infinite; }
    @keyframes ring { 0% { transform: scale(.6); opacity: .7; } 100% { transform: scale(2.4); opacity: 0; } }
    .status-pill.done.flash { animation: pop .5s ease; }
    @keyframes pop { 0% { transform: scale(1); } 40% { transform: scale(1.15); } 100% { transform: scale(1); } }
  }

  .error-msg { color: var(--critical); font-size: 12.5px; margin-top: 8px; display: none; }
  .error-msg.show { display: block; }

  .dash-wrap { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; background: var(--surface-1);
    min-height: 480px; display: flex; align-items: center; justify-content: center; }
  .dash-wrap iframe { width: 100%; height: 78vh; border: 0; display: block; }
  .placeholder { color: var(--muted); font-size: 13.5px; text-align: center; padding: 40px; }

  /* ================================================================
     Console shell + colour, appended so it wins over the rules above.
     ================================================================ */

  :root {
    --ink:        #0B1020;
    --indigo:     #4F46E5;
    --violet:     #7C3AED;
    --cyan:       #06B6D4;
    --emerald:    #059669;
    --amber:      #D97706;
    --rose:       #E11D48;
    --slate:      #64748B;
    --edge:       rgba(15,23,42,.10);
    --lift:       0 1px 2px rgba(11,16,32,.05), 0 10px 30px rgba(11,16,32,.07);
    --lift-hover: 0 2px 6px rgba(11,16,32,.08), 0 18px 44px rgba(11,16,32,.13);
    --accent:     #4F46E5;
    --accent-2:   #06B6D4;
    --blob-1: #4F46E5; --blob-2: #06B6D4; --blob-3: #7C3AED; --blob-4: #059669;
    --blob-opacity: .13;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --ink:        #E6EAF6;
      --edge:       rgba(148,163,184,.20);
      --lift:       0 1px 2px rgba(0,0,0,.45), 0 10px 30px rgba(0,0,0,.35);
      --lift-hover: 0 2px 6px rgba(0,0,0,.5), 0 18px 44px rgba(0,0,0,.45);
      --indigo:     #818CF8;
      --violet:     #A78BFA;
      --cyan:       #22D3EE;
      --emerald:    #34D399;
      --amber:      #FBBF24;
      --rose:       #FB7185;
      --accent:     #818CF8;
      --accent-2:   #22D3EE;
      --blob-opacity: .17;
    }
  }

  /* ---- the shell: window-height, panels scroll on their own ---------- */
  @media (min-width: 861px) {
    html { height: 100%; }
    body {
      height: 100vh;
      overflow: hidden;              /* the page itself never scrolls */
      display: flex;
      flex-direction: column;
    }
    .layout {
      flex: 1 1 auto;
      min-height: 0;                 /* without this the children cannot shrink */
      align-items: stretch;          /* was start - that is what capped the height */
      gap: 22px;
    }
    .card {
      overflow-y: auto;
      max-height: 100%;
      scrollbar-width: thin;
    }
    .dash-wrap {
      height: 100%;
      display: flex;
      flex-direction: column;
      min-height: 0;
    }
    .dash-wrap iframe { flex: 1 1 auto; height: auto; min-height: 0; }
    .placeholder { flex: 1 1 auto; }
  }

  /* ---- surfaces ------------------------------------------------------ */
  .card, .dash-wrap {
    border: 1px solid var(--edge);
    border-radius: 16px;
    box-shadow: var(--lift);
    transition: box-shadow .28s ease, transform .28s ease;
  }
  .card:hover, .dash-wrap:hover { box-shadow: var(--lift-hover); }

  /* a coloured seam along the top of each panel */
  .card { position: relative; overflow-x: hidden; }
  .card::before, .dash-wrap::before {
    content: ""; position: absolute; inset: 0 0 auto 0; height: 3px; z-index: 2;
    background: linear-gradient(90deg, var(--indigo), var(--violet), var(--cyan), var(--emerald));
    background-size: 300% 100%;
    animation: seam 14s linear infinite;
  }
  .dash-wrap { position: relative; }
  @keyframes seam { to { background-position: 300% 0; } }

  /* ---- headline ------------------------------------------------------ */
  h1 {
    background: linear-gradient(100deg, var(--indigo), var(--cyan) 55%, var(--violet));
    -webkit-background-clip: text; background-clip: text;
    color: transparent; -webkit-text-fill-color: transparent;
    letter-spacing: -.02em;
  }

  /* ---- navigation chips instead of inline links ---------------------- */
  .sub a {
    display: inline-block; text-decoration: none;
    padding: 5px 12px; margin-left: 6px; border-radius: 999px;
    border: 1px solid var(--edge); background: var(--plane);
    font-size: 13px; font-weight: 600;
    transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
  }
  .sub a:hover {
    transform: translateY(-1px);
    border-color: var(--accent);
    box-shadow: 0 6px 18px rgba(79,70,229,.18);
  }

  /* ---- controls ------------------------------------------------------ */
  button, .btn-primary, .btn-stop {
    transition: transform .16s ease, box-shadow .16s ease, filter .16s ease;
  }
  button:hover:not(:disabled) { transform: translateY(-1px); }
  button:active:not(:disabled) { transform: translateY(0) scale(.995); }
  .btn-primary:not(:disabled) {
    background: linear-gradient(135deg, var(--indigo), var(--violet));
    border-color: transparent; color: #fff;
    box-shadow: 0 8px 22px rgba(79,70,229,.30);
  }
  .btn-primary:not(:disabled):hover { box-shadow: 0 12px 30px rgba(79,70,229,.42); }
  #golive:not(:disabled) {
    background: linear-gradient(135deg, var(--emerald), #0d9488);
    color: #fff; border-color: transparent; font-weight: 600;
    box-shadow: 0 8px 22px rgba(5,150,105,.28);
  }
  #cleardrafts:not(:disabled) {
    border-color: color-mix(in srgb, var(--amber) 45%, transparent);
    color: var(--amber); font-weight: 600;
  }
  #cleardrafts:not(:disabled):hover { background: color-mix(in srgb, var(--amber) 12%, transparent); }
  .btn-stop { background: linear-gradient(135deg, var(--rose), #be123c); color: #fff; border-color: transparent; }

  /* ---- drop zone ----------------------------------------------------- */
  .drop { transition: border-color .2s ease, background .2s ease, transform .2s ease; }
  .drop:hover { border-color: var(--accent); transform: translateY(-1px); }
  .drop.drag {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 8%, transparent);
    transform: scale(1.01);
  }

  /* ---- status pill --------------------------------------------------- */
  .status-pill { transition: color .25s ease; }
  .status-pill.running .dot { background: var(--cyan); }
  .status-pill.done .dot    { background: var(--emerald); }
  .status-pill.error .dot   { background: var(--rose); }

  /* while a run is going, the dashboard edge breathes */
  .dash-wrap.is-running::before { animation: seam 3s linear infinite; }

  /* ---- entrance ------------------------------------------------------ */
  @keyframes riseIn {
    from { opacity: 0; transform: translateY(14px); }
    to   { opacity: 1; transform: none; }
  }
  .live-banner { animation: riseIn .45s ease both; }
  h1           { animation: riseIn .45s ease .04s both; }
  .sub         { animation: riseIn .45s ease .08s both; }
  .card        { animation: riseIn .5s ease .12s both; }
  .dash-wrap   { animation: riseIn .5s ease .18s both; }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: .001ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: .001ms !important;
    }
  }
</style>

<div class="bg-blobs" aria-hidden="true"><span></span><span></span><span></span><span></span></div>

<div class="live-banner" id="liveBanner">
  <span class="live-banner-label">Live mode <span class="hint">(off = dry run, nothing sent &middot; on = real submissions/emails)</span></span>
  <label class="switch switch-lg"><input type="checkbox" id="live"><span class="track"></span><span class="knob"></span></label>
</div>

<h1>Outreach agent</h1>
<p class="sub">Upload a leads spreadsheet and start a run - the dashboard on the right updates live.
  &nbsp;<a href="/contacted" style="color:var(--accent);font-weight:500;">Already contacted &rarr;</a>
  &nbsp;&middot;&nbsp;<a href="/overall" style="color:var(--accent);font-weight:500;">Overall totals &rarr;</a></p>

<div class="layout">
  <div class="card">
    <h2>New run</h2>

    <div class="drop" id="drop" tabindex="0" role="button" aria-label="Choose or drop a spreadsheet">
      <div><strong>Drop file here</strong> or click to choose</div>
      <div class="hint">.xlsx, .xls or .csv - see input_sample.xlsx for column layout</div>
    </div>
    <input type="file" id="fileInput" accept=".xlsx,.xls,.csv,.tsv" hidden>
    <div class="file-chip" id="fileChip"></div>
    <div class="error-msg" id="err"></div>

    <fieldset>
      <div class="row">
        <span>Show browser window</span>
        <label class="switch"><input type="checkbox" id="headless" checked><span class="track"></span><span class="knob"></span></label>
      </div>
      <div class="row">
        <span>Limit rows <span class="hint">(0 = all)</span></span>
        <input type="number" id="limit" min="0" value="0">
      </div>
    </fieldset>

    <button class="btn-primary" id="startBtn" disabled>Start run</button>
    <button id="golive" hidden style="margin-top:10px;width:100%;">Submit dry runs live</button>
    <button id="cleardrafts" hidden style="margin-top:8px;width:100%;">Clear all dry runs</button>
    <button class="btn-stop" id="stopBtn" hidden>Stop run</button>

    <div style="margin-top:16px;">
      <span class="status-pill" id="pill"><span class="dot"></span><span id="pillText">Idle</span></span>
    </div>
  </div>

  <div class="dash-wrap" id="dashWrap">
    <div class="placeholder" id="placeholder">Dashboard will appear here once a run starts.</div>
  </div>
</div>

<script>
  const drop = document.getElementById('drop');
  const fileInput = document.getElementById('fileInput');
  const fileChip = document.getElementById('fileChip');
  const errBox = document.getElementById('err');
  const startBtn = document.getElementById('startBtn');
  const stopBtn = document.getElementById('stopBtn');
  const pill = document.getElementById('pill');
  const pillText = document.getElementById('pillText');
  const dashWrap = document.getElementById('dashWrap');
  const placeholder = document.getElementById('placeholder');
  const liveToggle = document.getElementById('live');
  const liveBanner = document.getElementById('liveBanner');

  liveToggle.addEventListener('change', () => {
    liveBanner.classList.toggle('is-live', liveToggle.checked);
  });

  let uploadedPath = null;
  let iframe = null;
  let poller = null;

  function showError(msg) { errBox.textContent = msg; errBox.classList.add('show'); }
  function clearError() { errBox.classList.remove('show'); }

  async function uploadFile(file) {
    clearError();
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch('/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) { showError(data.error || 'upload failed'); startBtn.disabled = true; return; }
    uploadedPath = data.path;
    fileChip.textContent = data.name;
    fileChip.classList.add('show');
    startBtn.disabled = false;
  }

  drop.addEventListener('click', () => fileInput.click());
  drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') fileInput.click(); });
  fileInput.addEventListener('change', () => { if (fileInput.files[0]) uploadFile(fileInput.files[0]); });
  ['dragover', 'dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('drag'); }));
  drop.addEventListener('drop', e => { if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]); });

  function ensureIframe() {
    if (iframe) return iframe;
    placeholder.remove();
    iframe = document.createElement('iframe');
    iframe.title = 'Run dashboard';
    dashWrap.appendChild(iframe);
    return iframe;
  }

  async function startRun() {
    clearError();
    startBtn.disabled = true;
    const body = {
      input: uploadedPath,
      live: document.getElementById('live').checked,
      headless: document.getElementById('headless').checked,
      limit: parseInt(document.getElementById('limit').value || '0', 10),
    };
    const res = await fetch('/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok) { showError(data.error || 'could not start run'); startBtn.disabled = false; return; }
    stopBtn.hidden = false;
    startPolling();
  }
  startBtn.addEventListener('click', startRun);

  async function stopRun() {
    stopBtn.disabled = true;
    await fetch('/stop', { method: 'POST' });
    stopBtn.disabled = false;
  }
  stopBtn.addEventListener('click', stopRun);

  let lastPillState = '';
  function setPill(state, text) {
    pill.className = 'status-pill ' + state;
    pillText.textContent = text;
    if (state === 'done' && lastPillState !== 'done') {
      pill.classList.add('flash');
      setTimeout(() => pill.classList.remove('flash'), 500);
    }
    lastPillState = state;
  }

  function checkFrameAuth(frame) {
    // Same-origin, so the iframe's own document is readable directly. If the
    // session cookie ever goes stale mid-visit (seen intermittently on some
    // browsers with bare-IP addresses), the iframe silently shows the raw
    // "Authentication required" response instead of the dashboard - recover
    // by sending the whole page to a fresh login rather than leaving it dead.
    try {
      const text = frame.contentDocument && frame.contentDocument.body
        ? frame.contentDocument.body.innerText.trim() : '';
      if (text === 'Authentication required') {
        window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
      }
    } catch (e) { /* cross-origin or not loaded yet - ignore */ }
  }

  async function poll() {
    const res = await fetch('/status');
    if (res.status === 401) {
      window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
      return;
    }
    const s = await res.json();
    if (s.report_ready) {
      const frame = ensureIframe();
      const wanted = s.report_url + '?t=' + Math.floor(Date.now() / 3000);
      if (!frame.dataset.base || frame.dataset.base !== s.report_url) {
        frame.dataset.base = s.report_url;
        frame.addEventListener('load', () => checkFrameAuth(frame));
        frame.src = wanted;
      } else if (s.running) {
        frame.src = wanted; // periodic refresh while a run is active
      }
    }
    if (s.running) {
      setPill('running', 'Running' + (s.input ? ' - ' + s.input : ''));
      document.getElementById('dashWrap').classList.add('is-running');
      stopBtn.hidden = false;
      startBtn.disabled = true;
      golive.hidden = true;
      cleardrafts.hidden = true;
    } else {
      stopBtn.hidden = true;
      document.getElementById('dashWrap').classList.remove('is-running');
      startBtn.disabled = !uploadedPath;
      cleardrafts.hidden = !(s.dry_runs > 0);
      if (s.dry_runs > 0) {
        cleardrafts.dataset.count = s.dry_runs;
        cleardrafts.textContent = 'Clear ' + s.dry_runs + ' saved dry run' + (s.dry_runs === 1 ? '' : 's');
      }
      if (s.can_go_live && s.dry_runs > 0) {
        golive.hidden = false;
        golive.disabled = false;
        golive.dataset.count = s.dry_runs;
        golive.textContent = 'Submit ' + s.dry_runs + ' dry run' + (s.dry_runs === 1 ? '' : 's') + ' live';
      } else {
        golive.hidden = true;
      }
      if (s.returncode === 0) setPill('done', 'Finished');
      else if (s.returncode) {
        setPill('error', s.problem || ('Stopped (exit ' + s.returncode + ')'));
        pill.title = s.problem || '';
      }
      else setPill('', 'Idle');
      if (poller) { clearInterval(poller); poller = null; }
    }
  }

  function startPolling() {
    if (poller) clearInterval(poller);
    poller = setInterval(poll, 3000);
    poll();
  }

  const golive = document.getElementById('golive');
  golive.addEventListener('click', async () => {
    const n = golive.dataset.count || '0';
    if (!confirm('Submit ' + n + ' dry-run site(s) for real?\n\n' +
                 'Contact forms will be submitted and emails sent. Anyone already ' +
                 'contacted is skipped automatically. This cannot be undone.')) return;
    golive.disabled = true;
    const res = await fetch('/go-live', { method: 'POST' });
    const data = await res.json();
    if (data.error) { alert(data.error); golive.disabled = false; return; }
    startPolling();
  });

  const cleardrafts = document.getElementById('cleardrafts');
  cleardrafts.addEventListener('click', async () => {
    const n = cleardrafts.dataset.count || '0';
    if (!confirm('Clear ' + n + ' saved dry-run row(s)?\n\n' +
                 'Only saved dry runs are removed - sites filled in but never submitted or sent. Everyone actually emailed or ' +
                 'submitted to is kept, so they still will not be contacted twice.\n\n' +
                 'A backup of the history is saved first.')) return;
    cleardrafts.disabled = true;
    const res = await fetch('/clear-drafts', { method: 'POST' });
    const data = await res.json();
    cleardrafts.disabled = false;
    if (data.error) { alert(data.error); return; }
    alert('Cleared ' + data.removed + ' dry run(s).\n' +
          data.contacted + ' real contact(s) kept.' +
          (data.backup ? '\nBackup: ' + data.backup : ''));
    poll();
    const frame = document.querySelector('#reportFrame, iframe');
    if (frame && frame.dataset.base) frame.src = frame.dataset.base + '?t=' + Date.now();
  });

  poll(); // pick up an already-running process on page load
</script>
"""


if __name__ == "__main__":
    print(f"Outreach agent listening on http://{AGENT_HOST}:{AGENT_PORT}"
          + (" (password required)" if AGENT_PASSWORD else " (no password - localhost only)"))
    app.run(host=AGENT_HOST, port=AGENT_PORT, debug=False)
