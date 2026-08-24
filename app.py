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
import threading
from html import escape as html_escape
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session
from werkzeug.utils import secure_filename

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
STATE: dict = {"proc": None, "input": None, "live": False, "started_at": None}


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
        uploads = sorted(UPLOAD_DIR.glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for candidate in uploads:
        if candidate.suffix.lower() in ALLOWED_EXT:
            return candidate
    return None


def _dry_run_count() -> int:
    """Rows filled but never submitted - what "go live" would actually act on."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return sum(1 for r in data.values() if r.get("status") == "dry_run")


def _status() -> dict:
    proc = STATE["proc"]
    running = proc is not None and proc.poll() is None
    code = None if proc is None else proc.poll()
    return {
        "problem": _last_problem() if (code not in (0, None)) else "",
        "dry_runs": _dry_run_count(),
        "can_go_live": _last_input() is not None and not running,
        "running": running,
        "returncode": None if proc is None else proc.poll(),
        "input": STATE["input"],
        "live": STATE["live"],
        "started_at": STATE["started_at"],
        "report_url": _run_report_url(),
        "report_ready": REPORT_PATH.exists(),
    }


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
    return jsonify(path=str(dest), name=stamped)


@app.post("/start")
def start():
    with _lock:
        proc = STATE["proc"]
        if proc is not None and proc.poll() is None:
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

        return start_run(input_path, live, headless, limit)


def start_run(input_path: Path, live: bool, headless: bool, limit: int):
    """Launch run.py. Caller owns the "already running" check."""
    argv = [sys.executable, "run.py", "--input", str(input_path), "--yes"]
    if live:
        argv.append("--live")
    if not headless:
        argv.append("--no-headless")
    if limit:
        argv += ["--limit", str(limit)]

    LAUNCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(LAUNCH_LOG, "a", encoding="utf-8")
    log_fh.write(f"\n--- launched {datetime.now():%Y-%m-%d %H:%M:%S} :: {' '.join(argv)} ---\n")
    log_fh.flush()

    proc = subprocess.Popen(argv, cwd=str(ROOT), stdout=log_fh, stderr=subprocess.STDOUT)
    STATE.update(proc=proc, input=input_path.name, input_path=str(input_path),
                 live=live, started_at=datetime.now().isoformat(timespec="seconds"))
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
        if proc is not None and proc.poll() is None:
            return jsonify(error="a run is already in progress"), 409
        path = _last_input()
        if path is None:
            return jsonify(error="no previous run to promote - upload a sheet and run it first"), 400

    return start_run(path, live=True, headless=True, limit=0)


@app.post("/clear-drafts")
def clear_drafts():
    """Remove dry-run rows from the history. Never touches a real contact.

    state.json is the only record of who has actually been written to, so this
    filters by status rather than deleting the file, and takes a timestamped
    backup first. Anything sent, submitted or skipped as a duplicate stays.
    """
    with _lock:
        proc = STATE["proc"]
        if proc is not None and proc.poll() is None:
            return jsonify(error="a run is in progress - stop it before clearing drafts"), 409

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


@app.post("/stop")
def stop():
    with _lock:
        proc = STATE["proc"]
        if proc is not None and proc.poll() is None:
            proc.terminate()
        return jsonify(_status())


@app.get("/status")
def status():
    return jsonify(_status())


@app.get("/files/<path:relpath>")
def files(relpath):
    target = (ROOT / relpath).resolve()
    allowed_roots = (REPORT_PATH.resolve().parent, EVIDENCE_DIR.resolve())
    if not any(target == r or target.is_relative_to(r) for r in allowed_roots):
        return "forbidden", 403
    return send_from_directory(target.parent, target.name)


CONTACTED_CSS = """
  body { margin:0; background:#f8f9fa; color:#111827;
         font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }
  @media (prefers-color-scheme: dark) { body { background:#0f172a; color:#e2e8f0; } }
  .wrap { max-width:1100px; margin:0 auto; padding:28px 20px 64px; }
  a { color:#2563eb; }
  h1 { font-size:24px; margin:0 0 4px; }
  .sub { color:#6b7280; margin:0 0 22px; font-size:14px; }
  .tabs { display:flex; gap:6px; border-bottom:1px solid rgba(128,128,128,.28); flex-wrap:wrap; }
  .tab { padding:10px 16px; border:1px solid transparent; border-bottom:none; cursor:pointer;
         border-radius:8px 8px 0 0; font:500 14px system-ui,sans-serif; color:#6b7280; background:none; }
  .tab[aria-selected="true"] { background:#fff; color:#111827; border-color:rgba(128,128,128,.28); }
  @media (prefers-color-scheme: dark) { .tab[aria-selected="true"] { background:#1e293b; color:#f8fafc; } }
  .panel { background:#fff; border:1px solid rgba(128,128,128,.28); border-top:none;
           border-radius:0 0 10px 10px; overflow-x:auto; }
  @media (prefers-color-scheme: dark) { .panel { background:#1e293b; } }
  table { border-collapse:collapse; width:100%; font-size:13.5px; }
  th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
       color:#6b7280; padding:12px 14px; border-bottom:1px solid rgba(128,128,128,.22); white-space:nowrap; }
  td { padding:10px 14px; border-bottom:1px solid rgba(128,128,128,.13); vertical-align:top; }
  tr:last-child td { border-bottom:none; }
  .mono { font-variant-numeric:tabular-nums; color:#6b7280; white-space:nowrap; }
  .pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11.5px; font-weight:600; }
  .ok { background:#dcfce7; color:#166534; }
  .warn { background:#fef3c7; color:#92400e; }
  @media (prefers-color-scheme: dark) { .ok{background:#14532d;color:#bbf7d0;} .warn{background:#78350f;color:#fde68a;} }
  .empty { padding:36px 16px; text-align:center; color:#6b7280; }
  .count { font-weight:400; color:#9ca3af; }
  .bar { display:flex; justify-content:space-between; align-items:center; gap:12px;
         margin-bottom:18px; flex-wrap:wrap; }
  .btn { display:inline-block; padding:8px 14px; border:1px solid rgba(128,128,128,.3);
         border-radius:8px; text-decoration:none; font-size:13.5px; }
"""


def _contacted_rows():
    """Everything the agent actually reached, split by how, newest first."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return [], []
    reached = [r for r in data.values() if r.get("status") in ("sent", "success", "uncertain")]
    reached.sort(key=lambda r: str(r.get("timestamp", "")), reverse=True)
    return ([r for r in reached if r.get("method") == "email"],
            [r for r in reached if r.get("method") == "form"])


def _esc(value) -> str:
    return html_escape(str(value or ""))


def _when(row) -> str:
    return _esc(str(row.get("timestamp"))[:16].replace("T", " "))


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


@app.get("/contacted")
def contacted():
    mails, forms = _contacted_rows()

    if mails:
        mail_rows = "".join(
            '<tr><td><strong>{}</strong></td><td>{}<br><a href="{}" target="_blank" rel="noopener">{}</a></td>'
            '<td>{}</td><td><span class="pill ok">{}</span></td><td class="mono">{}</td></tr>'.format(
                _esc(r.get("email_used")), _esc(r.get("company_name")),
                _esc(r.get("website")), _esc(r.get("website")),
                _esc(r.get("sender_email")), _esc(r.get("status")), _when(r))
            for r in mails)
    else:
        mail_rows = '<tr><td colspan="5"><div class="empty">No emails sent yet.</div></td></tr>'

    if forms:
        form_rows = "".join(
            '<tr><td><strong>{}</strong><br><a href="{}" target="_blank" rel="noopener">{}</a></td>'
            '<td><a href="{}" target="_blank" rel="noopener">{}</a></td><td>{}</td>'
            '<td><span class="pill {}">{}</span></td><td class="mono">{}</td></tr>'.format(
                _esc(r.get("company_name")), _esc(r.get("website")), _esc(r.get("website")),
                _esc(r.get("contact_page") or r.get("website")),
                _esc(r.get("contact_page") or r.get("website")),
                _esc(r.get("sender")),
                "ok" if r.get("status") == "success" else "warn",
                _esc(r.get("status")), _when(r))
            for r in forms)
    else:
        form_rows = '<tr><td colspan="5"><div class="empty">No contact forms submitted yet.</div></td></tr>'

    # Plain substitution, not str.format: the page carries a <script> block
    # whose braces would be read as format fields.
    page = CONTACTED_HTML
    for token, value in (("{css}", CONTACTED_CSS), ("{n_mail}", str(len(mails))),
                         ("{n_form}", str(len(forms))), ("{mail_rows}", mail_rows),
                         ("{form_rows}", form_rows)):
        page = page.replace(token, value)
    return page


CONTACTED_HTML = """<!doctype html>
<meta charset="utf-8"><title>Already contacted - outreach agent</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}</style>
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
  </div>

  <div class="panel" id="p-mail">
    <table><thead><tr><th>Email address</th><th>Company / site</th><th>Sent from</th>
      <th>Status</th><th>When</th></tr></thead><tbody>{mail_rows}</tbody></table>
  </div>
  <div class="panel" id="p-form" hidden>
    <table><thead><tr><th>Company / site</th><th>Form page</th><th>Filled as</th>
      <th>Status</th><th>When</th></tr></thead><tbody>{form_rows}</tbody></table>
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


@app.get("/")
def index():
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
    <button id="cleardrafts" hidden style="margin-top:8px;width:100%;">Clear all drafts</button>
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
      stopBtn.hidden = false;
      startBtn.disabled = true;
      golive.hidden = true;
      cleardrafts.hidden = true;
    } else {
      stopBtn.hidden = true;
      startBtn.disabled = !uploadedPath;
      cleardrafts.hidden = !(s.dry_runs > 0);
      if (s.dry_runs > 0) {
        cleardrafts.dataset.count = s.dry_runs;
        cleardrafts.textContent = 'Clear ' + s.dry_runs + ' draft' + (s.dry_runs === 1 ? '' : 's');
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
    if (!confirm('Clear ' + n + ' draft row(s)?\n\n' +
                 'Only dry runs are removed. Everyone already emailed or ' +
                 'submitted to is kept, so they still will not be contacted twice.\n\n' +
                 'A backup of the history is saved first.')) return;
    cleardrafts.disabled = true;
    const res = await fetch('/clear-drafts', { method: 'POST' });
    const data = await res.json();
    cleardrafts.disabled = false;
    if (data.error) { alert(data.error); return; }
    alert('Cleared ' + data.removed + ' draft(s).\n' +
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
