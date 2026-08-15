"""Static HTML dashboard for a run - reads the in-memory results (or
output/results.xlsx) and writes a single self-contained report.html next to
it. No server, no external assets - open the file in any browser."""
from __future__ import annotations

import html
import os
import webbrowser
from datetime import datetime
from pathlib import Path
from string import Template

STATUS_META = {
    # key: (label, role)  - role picks the color from the fixed status palette
    "success": ("Success", "good"),
    "sent": ("Sent", "good"),
    "uncertain": ("Uncertain", "warning"),
    "failed": ("Failed", "serious"),
    "no_contact_found": ("No contact found", "serious"),
    "unreachable": ("Unreachable", "critical"),
    "error": ("Error", "critical"),
    "dry_run": ("Dry run", "neutral"),
}
STATUS_ORDER = list(STATUS_META)

ROLE_COLOR = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
    "neutral": "#898781",
}

# Fixed-order categorical slots for the sender breakdown - identity, not
# severity, so it draws from the categorical palette, never the status one.
CATEGORICAL_SLOTS = 8


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def _compact(n: int) -> str:
    return f"{n / 1000:.1f}K" if n >= 10_000 else str(n)


def _rel_link(path: str, report_dir: Path) -> str:
    if not path:
        return ""
    try:
        return os.path.relpath(path, start=report_dir)
    except ValueError:
        return path  # different drive on Windows - fall back to absolute


def _status_meta(status: str) -> tuple[str, str]:
    return STATUS_META.get(status, (status.replace("_", " ").title() or "Unknown", "neutral"))


def _badge(status: str) -> str:
    label, role = _status_meta(status)
    color = ROLE_COLOR[role]
    return (
        f'<span class="badge" style="--dot:{color}">'
        f'<span class="dot"></span>{_esc(label)}</span>'
    )


def _stat_tile(label: str, value: int, role: str = "") -> str:
    style = f' style="--accent:{ROLE_COLOR[role]}"' if role else ""
    return (
        f'<div class="tile"{style}><div class="tile-value">{_compact(value)}</div>'
        f'<div class="tile-label">{_esc(label)}</div></div>'
    )


def _bar_row(label: str, count: int, max_count: int, color: str) -> str:
    pct = max(2, round(100 * count / max_count)) if max_count else 0
    return f"""
    <div class="bar-row">
      <div class="bar-label">{_esc(label)}</div>
      <div class="bar-track"><div class="bar-fill" data-width="{pct}" style="background:{color}"></div></div>
      <div class="bar-value">{count}</div>
    </div>"""


def _table_row(r: dict, report_dir: Path) -> str:
    status = str(r.get("status") or "")
    sender = str(r.get("sender") or "")
    sender_email = str(r.get("sender_email") or "")
    method = str(r.get("method") or "")
    timestamp = str(r.get("timestamp") or "")
    before = _rel_link(r.get("screenshot_before", ""), report_dir)
    after = _rel_link(r.get("screenshot_after", ""), report_dir)
    shots = " &middot; ".join(
        f'<a href="{_esc(p)}" target="_blank" rel="noopener">{label}</a>'
        for p, label in ((before, "before"), (after, "after")) if p
    ) or '<span class="muted">&mdash;</span>'
    detail = str(r.get("detail") or "")
    search = " ".join(str(r.get(k, "")) for k in ("website", "company_name", "detail", "email_used")).lower()
    return f"""
    <tr data-status="{_esc(status)}" data-search="{_esc(search)}"
        data-company="{_esc(r.get('company_name',''))}" data-sender="{_esc(sender)}"
        data-method="{_esc(method)}" data-time="{_esc(timestamp)}">
      <td><a href="{_esc(r.get('website',''))}" target="_blank" rel="noopener">{_esc(r.get('company_name',''))}</a>
        <div class="muted small">{_esc(r.get('website',''))}</div></td>
      <td title="{_esc(sender_email)}">{_esc(sender) or '—'}</td>
      <td>{_esc(method.title()) or '—'}</td>
      <td>{_badge(status)}</td>
      <td class="detail" title="{_esc(detail)}">{_esc(detail[:140] + ('…' if len(detail) > 140 else ''))}</td>
      <td>{shots}</td>
      <td class="muted small">{_esc(timestamp)}</td>
    </tr>"""


PAGE = Template(r"""<!doctype html>
<meta charset="utf-8">
<title>$title</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    color-scheme: light;
    --surface-1:  #ffffff;
    --surface-2:  #f1f5f9;
    --plane:      #f8f9fa;
    --text-1:     #0f172a;
    --text-2:     #475569;
    --muted:      #94a3b8;
    --grid:       #e2e8f0;
    --border:     rgba(15,23,42,0.08);
    --accent:     #2563eb;
    --accent-2:   #0d9488;
    --critical:   #d03b3b;
    --blob-1: #2563eb; --blob-2: #0d9488; --blob-3: #2563eb; --blob-4: #0d9488;
    --blob-opacity: .10;
    --cat-1: #2563eb; --cat-2: #0d9488; --cat-3: #60a5fa; --cat-4: #2dd4bf;
    --cat-5: #1d4ed8; --cat-6: #0f766e; --cat-7: #93c5fd; --cat-8: #5eead4;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface-1:  #1e293b;
      --surface-2:  #334155;
      --plane:      #0f172a;
      --text-1:     #f8fafc;
      --text-2:     #cbd5e1;
      --muted:      #94a3b8;
      --grid:       #334155;
      --border:     rgba(255,255,255,0.08);
      --accent:     #3b82f6;
      --accent-2:   #14b8a6;
      --critical:   #e66767;
      --blob-1: #3b82f6; --blob-2: #14b8a6; --blob-3: #3b82f6; --blob-4: #14b8a6;
      --blob-opacity: .16;
      --cat-1: #3b82f6; --cat-2: #14b8a6; --cat-3: #93c5fd; --cat-4: #5eead4;
      --cat-5: #60a5fa; --cat-6: #2dd4bf; --cat-7: #bfdbfe; --cat-8: #99f6e4;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1:  #1e293b;
    --surface-2:  #334155;
    --plane:      #0f172a;
    --text-1:     #f8fafc;
    --text-2:     #cbd5e1;
    --muted:      #94a3b8;
    --grid:       #334155;
    --border:     rgba(255,255,255,0.08);
    --accent:     #3b82f6;
    --accent-2:   #14b8a6;
    --critical:   #e66767;
    --blob-1: #3b82f6; --blob-2: #14b8a6; --blob-3: #3b82f6; --blob-4: #14b8a6;
    --blob-opacity: .16;
    --cat-1: #3b82f6; --cat-2: #14b8a6; --cat-3: #93c5fd; --cat-4: #5eead4;
    --cat-5: #60a5fa; --cat-6: #2dd4bf; --cat-7: #bfdbfe; --cat-8: #99f6e4;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--plane); color: var(--text-1);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 32px clamp(16px, 4vw, 48px) 64px;
  }

  .bg-blobs { position: fixed; inset: 0; overflow: hidden; z-index: -1; pointer-events: none; }
  .bg-blobs span { position: absolute; border-radius: 50%; filter: blur(64px); opacity: var(--blob-opacity); }
  .bg-blobs span:nth-child(1) { width: 40vw; height: 40vw; background: var(--blob-1); top: -16%; left: -10%; }
  .bg-blobs span:nth-child(2) { width: 34vw; height: 34vw; background: var(--blob-2); bottom: -16%; right: -8%; }
  .bg-blobs span:nth-child(3) { width: 26vw; height: 26vw; background: var(--blob-3); top: 32%; right: 20%; }
  .bg-blobs span:nth-child(4) { width: 20vw; height: 20vw; background: var(--blob-4); bottom: 12%; left: 10%; }
  @media (prefers-reduced-motion: no-preference) {
    .bg-blobs span:nth-child(1) { animation: drift1 30s ease-in-out infinite; }
    .bg-blobs span:nth-child(2) { animation: drift2 36s ease-in-out infinite; }
    .bg-blobs span:nth-child(3) { animation: drift3 24s ease-in-out infinite; }
    .bg-blobs span:nth-child(4) { animation: drift2 32s ease-in-out infinite reverse; }
    @keyframes drift1 { 0%, 100% { transform: translate(0,0); } 50% { transform: translate(5vw,7vh) scale(1.05); } }
    @keyframes drift2 { 0%, 100% { transform: translate(0,0); } 50% { transform: translate(-6vw,-5vh) scale(1.06); } }
    @keyframes drift3 { 0%, 100% { transform: translate(0,0); } 50% { transform: translate(-4vw,5vh) scale(1.1); } }
  }

  h1 { font-size: 22px; margin: 0 0 2px; font-weight: 700; display: inline-block;
    background: linear-gradient(90deg, var(--accent), var(--accent-2));
    background-size: 200% auto; -webkit-background-clip: text; background-clip: text; color: transparent; }
  @media (prefers-reduced-motion: no-preference) {
    h1 { animation: hueflow 7s ease-in-out infinite; }
    @keyframes hueflow { 0%, 100% { background-position: 0% center; } 50% { background-position: 100% center; } }
  }
  .sub { color: var(--text-2); font-size: 13px; margin: 0 0 28px; }
  .badge.mode { display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px;
    border-radius: 999px; font-size: 12px; font-weight: 600; border: 1px solid var(--border);
    margin-left: 10px; vertical-align: 2px; }
  .mode-live { color: var(--critical); border-color: var(--critical); }
  .mode-dry { color: var(--text-2); }
  @media (prefers-reduced-motion: no-preference) {
    .mode-live { animation: livepulse 1.8s ease-in-out infinite; }
    @keyframes livepulse { 0%, 100% { box-shadow: 0 0 0 rgba(208,59,59,0); } 50% { box-shadow: 0 0 10px color-mix(in srgb, var(--critical) 55%, transparent); } }
  }

  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 2px;
    background: var(--border); border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
    margin-bottom: 28px; }
  .tile { background: var(--surface-1); padding: 16px 18px; border-top: 3px solid var(--accent, transparent);
    transition: transform .15s ease; }
  .tile:hover { transform: translateY(-2px); }
  .tile-value { font-size: 26px; font-weight: 600; }
  .tile-label { font-size: 12.5px; color: var(--text-2); margin-top: 2px; }

  .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px 22px; margin-bottom: 24px; box-shadow: 0 1px 24px rgba(0,0,0,.04); }
  .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-2);
    margin: 0 0 16px; font-weight: 600; }
  @media (prefers-reduced-motion: no-preference) {
    .tiles, .card { animation: rise .5s ease both; }
    .card:nth-of-type(2) { animation-delay: .05s; }
    .card:nth-of-type(3) { animation-delay: .1s; }
    @keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  }

  .bar-row { display: grid; grid-template-columns: 170px 1fr 34px; align-items: center; gap: 12px;
    margin-bottom: 10px; }
  .bar-row:last-child { margin-bottom: 0; }
  .bar-label { font-size: 13px; color: var(--text-2); }
  .bar-track { background: var(--grid); border-radius: 4px; height: 16px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 0 4px 4px 0; min-width: 4px; width: 0;
    transition: width .8s cubic-bezier(.22,1,.36,1); }
  .bar-value { text-align: right; font-variant-numeric: tabular-nums; font-size: 13px; color: var(--text-2); }

  .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 16px; }
  .toolbar input[type=search] { flex: 1 1 200px; padding: 8px 12px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--plane); color: var(--text-1); font-size: 13px; }
  .chip { padding: 5px 12px; border-radius: 999px; border: 1px solid var(--border); background: var(--plane);
    color: var(--text-2); font-size: 12.5px; cursor: pointer; user-select: none; transition: transform .12s, background .15s, color .15s; }
  .chip:hover { transform: translateY(-1px); border-color: var(--accent); }
  .chip.active { background: linear-gradient(135deg, var(--accent), var(--accent-2)); border-color: transparent; color: #fff; }

  table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  th { text-align: left; font-size: 11.5px; text-transform: uppercase; letter-spacing: .03em;
    color: var(--muted); font-weight: 600; padding: 0 10px 8px; border-bottom: 1px solid var(--grid); }
  th[data-key] { cursor: pointer; user-select: none; }
  th[data-key]:hover { color: var(--text-1); }
  th[data-key]::after { content: ''; display: inline-block; width: 10px; }
  th.sort-asc::after { content: '▲'; font-size: 9px; margin-left: 4px; color: var(--accent); }
  th.sort-desc::after { content: '▼'; font-size: 9px; margin-left: 4px; color: var(--accent); }
  td { padding: 10px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .muted { color: var(--muted); }
  .small { font-size: 12px; }
  .detail { max-width: 320px; }

  .badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--text-1);
    white-space: nowrap; }
  .badge .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dot); flex: none; }

  .empty { text-align: center; color: var(--muted); padding: 32px; }

  a:focus-visible, .chip:focus-visible, input:focus-visible, th[data-key]:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }
</style>

<div class="bg-blobs" aria-hidden="true"><span></span><span></span><span></span><span></span></div>

<h1>$title<span class="badge mode $mode_class">$mode_label</span></h1>
<p class="sub">Generated $generated_at &middot; $total sites</p>

<div class="tiles">
  $stat_tiles
</div>

<div class="card">
  <h2>Outcome breakdown</h2>
  $bars
</div>

<div class="card">
  <h2>Sender breakdown</h2>
  $sender_bars
</div>

<div class="card">
  <div class="toolbar">
    <input type="search" id="q" placeholder="Filter by company, website or detail&hellip;">
    <span class="chip active" data-status="" role="button" tabindex="0">All</span>
    $chips
  </div>
  <table id="tbl">
    <thead><tr>
      <th data-key="company">Company</th>
      <th data-key="sender">Sender</th>
      <th data-key="method">Method</th>
      <th data-key="status">Status</th>
      <th>Detail</th>
      <th>Screenshots</th>
      <th data-key="time">Time</th>
    </tr></thead>
    <tbody>
      $table_rows
    </tbody>
  </table>
  <div class="empty" id="empty" hidden>No rows match the current filter.</div>
</div>

<script>
  const chips = Array.from(document.querySelectorAll('.chip'));
  const q = document.getElementById('q');
  const rows = Array.from(document.querySelectorAll('#tbl tbody tr'));
  const empty = document.getElementById('empty');
  let active = '';

  function apply() {
    const term = q.value.trim().toLowerCase();
    let shown = 0;
    for (const r of rows) {
      const okStatus = !active || r.dataset.status === active;
      const okSearch = !term || r.dataset.search.includes(term);
      const visible = okStatus && okSearch;
      r.style.display = visible ? '' : 'none';
      if (visible) shown++;
    }
    empty.hidden = shown !== 0;
  }

  function selectChip(c) {
    chips.forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    active = c.dataset.status || '';
    apply();
  }
  chips.forEach(c => {
    c.addEventListener('click', () => selectChip(c));
    c.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectChip(c); }
    });
  });
  q.addEventListener('input', apply);

  const sortableTh = Array.from(document.querySelectorAll('#tbl thead th[data-key]'));
  let sortKey = null, sortDir = 1;
  function sortBy(key) {
    sortDir = (sortKey === key) ? sortDir * -1 : 1;
    sortKey = key;
    sortableTh.forEach(th => th.classList.remove('sort-asc', 'sort-desc'));
    const activeTh = sortableTh.find(th => th.dataset.key === key);
    if (activeTh) activeTh.classList.add(sortDir === 1 ? 'sort-asc' : 'sort-desc');
    const tbody = document.querySelector('#tbl tbody');
    rows.slice().sort((a, b) => {
      const av = a.dataset[key] || '';
      const bv = b.dataset[key] || '';
      return av.localeCompare(bv, undefined, { numeric: true }) * sortDir;
    }).forEach(r => tbody.appendChild(r));
  }
  sortableTh.forEach(th => {
    th.setAttribute('tabindex', '0');
    th.setAttribute('role', 'button');
    th.addEventListener('click', () => sortBy(th.dataset.key));
    th.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sortBy(th.dataset.key); }
    });
  });

  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.querySelectorAll('.bar-fill').forEach(el => { el.style.width = el.dataset.width + '%'; });
  }));
</script>
""")


def build_report(results: list[dict], mode: str, report_path: str | Path) -> Path:
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_dir = report_path.resolve().parent

    counts: dict[str, int] = {}
    for r in results:
        status = str(r.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    ordered_statuses = [s for s in STATUS_ORDER if s in counts]
    ordered_statuses += [s for s in counts if s not in STATUS_ORDER]
    max_count = max(counts.values(), default=0)

    tiles = [_stat_tile("Total sites", len(results))]
    for s in ordered_statuses:
        label, role = _status_meta(s)
        tiles.append(_stat_tile(label, counts[s], role))

    bars = "".join(
        _bar_row(_status_meta(s)[0], counts[s], max_count, ROLE_COLOR[_status_meta(s)[1]])
        for s in ordered_statuses
    ) or '<p class="muted">No results yet.</p>'

    chips = "".join(
        f'<span class="chip" data-status="{_esc(s)}" role="button" tabindex="0">'
        f'{_esc(_status_meta(s)[0])} ({counts[s]})</span>'
        for s in ordered_statuses
    )

    sender_counts: dict[tuple[str, str], int] = {}
    sender_order: list[tuple[str, str]] = []
    for r in results:
        key = (str(r.get("sender") or "Unknown"), str(r.get("sender_email") or ""))
        if key not in sender_counts:
            sender_order.append(key)
        sender_counts[key] = sender_counts.get(key, 0) + 1
    max_sender = max(sender_counts.values(), default=0)
    sender_bars = "".join(
        _bar_row(
            f"{name} ({email.rsplit('@', 1)[-1]})" if email else name,
            sender_counts[(name, email)], max_sender,
            f"var(--cat-{(i % CATEGORICAL_SLOTS) + 1})",
        )
        for i, (name, email) in enumerate(sender_order)
    ) or '<p class="muted">No results yet.</p>'

    rows_html = "".join(_table_row(r, report_dir) for r in results) or (
        '<tr><td colspan="7" class="empty">No results yet - run the agent first.</td></tr>'
    )

    html_out = PAGE.substitute(
        title="Outreach run report",
        mode_class="mode-live" if mode == "live" else "mode-dry",
        mode_label="LIVE" if mode == "live" else "DRY RUN",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        total=len(results),
        stat_tiles="".join(tiles),
        bars=bars,
        sender_bars=sender_bars,
        chips=chips,
        table_rows=rows_html,
    )
    report_path.write_text(html_out, encoding="utf-8")
    return report_path


def open_report(path: Path) -> None:
    webbrowser.open(path.resolve().as_uri())


if __name__ == "__main__":
    import argparse

    import pandas as pd

    p = argparse.ArgumentParser(description="Regenerate the HTML dashboard from results.xlsx")
    p.add_argument("--results", default="output/results.xlsx")
    p.add_argument("--out", default="output/report.html")
    p.add_argument("--mode", default="dry_run")
    p.add_argument("--open", action="store_true")
    args = p.parse_args()

    df = pd.read_csv(args.results) if args.results.endswith(".csv") else pd.read_excel(args.results)
    rows = df.fillna("").to_dict(orient="records")
    out = build_report(rows, args.mode, args.out)
    print(f"wrote {out}")
    if args.open:
        open_report(out)
