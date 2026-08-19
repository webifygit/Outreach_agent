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
import secrets
import subprocess
import sys
import threading
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


def _status() -> dict:
    proc = STATE["proc"]
    running = proc is not None and proc.poll() is None
    code = None if proc is None else proc.poll()
    return {
        "problem": _last_problem() if (code not in (0, None)) else "",
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
        STATE.update(proc=proc, input=input_path.name, live=live, started_at=datetime.now().isoformat(timespec="seconds"))
        return jsonify(_status())


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
<p class="sub">Upload a leads spreadsheet and start a run - the dashboard on the right updates live.</p>

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
    } else {
      stopBtn.hidden = true;
      startBtn.disabled = !uploadedPath;
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

  poll(); // pick up an already-running process on page load
</script>
"""


if __name__ == "__main__":
    print(f"Outreach agent listening on http://{AGENT_HOST}:{AGENT_PORT}"
          + (" (password required)" if AGENT_PASSWORD else " (no password - localhost only)"))
    app.run(host=AGENT_HOST, port=AGENT_PORT, debug=False)
