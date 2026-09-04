"""Per-sheet outcome workbook: what happened to every row, and why.

The status field alone does not answer "why was this one not approached?".
A CAPTCHA site falls back to email (run.py), and with email disabled that
lands as ``skipped_no_email`` - the same status as a site with no form at
all. The reason survives only in the free-text ``detail``, so the reason
column here is recovered from both.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

CAPTCHA_RE = re.compile(r"captcha|turnstile|challenges\.cloudflare", re.I)
NO_MESSAGE_RE = re.compile(r"no message|message field|require_message", re.I)

COLS = ["website", "company_name", "reason", "status", "method", "sheet",
        "contact_page", "sender", "sender_email", "email_used",
        "detail", "screenshot_after", "timestamp"]

# (bucket label, predicate) - order matters, first match wins.
BUCKETS: list[tuple[str, object]] = [
    ("Approached - confirmed",
     lambda r, d: r.get("status") == "success"),
    ("Approached - sent, no confirmation",
     lambda r, d: r.get("status") == "uncertain"),
    ("Approached - by email",
     lambda r, d: r.get("status") == "sent"),
    ("Blocked - CAPTCHA",
     lambda r, d: bool(CAPTCHA_RE.search(d))),
    ("Not approached - no form found",
     lambda r, d: r.get("status") == "no_contact_found"),
    ("Not approached - no usable form",
     lambda r, d: r.get("status") == "skipped_no_email"),
    ("Not approached - unreachable",
     lambda r, d: r.get("status") == "unreachable"),
    ("Not approached - timed out",
     lambda r, d: r.get("status") == "timeout"),
    ("Failed",
     lambda r, d: r.get("status") in ("failed", "error")),
    ("Not approached - blocklisted (wedges browser)",
     lambda r, d: r.get("status") == "skipped_blocked"),
    ("Already approached earlier",
     lambda r, d: r.get("status") == "skipped_duplicate"),
    ("Rehearsal only (dry run)",
     lambda r, d: r.get("status") == "dry_run"),
]


# Cross-cutting views. A row can appear in one of these AND in its bucket
# above: "every URL with a CAPTCHA" is a question about the site, not about
# how the run ended, and the two answers diverge the moment email is enabled
# (a CAPTCHA site that falls back to email is filed under "Approached - by
# email" and vanishes from the CAPTCHA count). These tabs stay authoritative.
CROSSCUTS: list[tuple[str, object]] = [
    ("ALL with CAPTCHA",
     lambda r, d: bool(CAPTCHA_RE.search(d))),
    ("ALL with no usable form",
     lambda r, d: r.get("status") in ("no_contact_found", "skipped_no_email")),
    ("ALL never reached",
     lambda r, d: r.get("status") in ("unreachable", "timeout", "failed", "error")),
]


def classify(rec: dict) -> str:
    """The human reason this row ended where it did."""
    detail = str(rec.get("detail") or "")
    for label, pred in BUCKETS:
        if pred(rec, detail):
            return label
    return f"Other ({rec.get('status', 'unknown')})"


def _frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame(rows)
    for c in COLS:
        if c not in df:
            df[c] = ""
    return df[COLS].sort_values("timestamp")


def _tab(label: str, n: int) -> str:
    # Excel: 31 chars max, and none of : \ / ? * [ ]
    safe = re.sub(r"[:\\/?*\[\]]", "-", label)
    return f"{safe} ({n})"[:31]


def _slug(label: str) -> str:
    """A filename-safe version of a bucket label."""
    return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()[:60]


def build_split_files(records: list[dict], out_dir: str | Path,
                      sheet_filter: str = "", fmt: str = "xlsx") -> list[Path]:
    """One standalone file per outcome, beside the combined workbook.

    The workbook already carries a tab per outcome, but a tab is awkward to
    hand to someone, mail, or feed to another tool. These are the same rows,
    one file each, so "the unreachable ones" is a file you can send on its own.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob(f"*.{fmt}"):
        stale.unlink()                      # a rebuild must not leave last run's files

    rows = list(records)
    if sheet_filter:
        rows = [r for r in rows if str(r.get("sheet", "")) == sheet_filter]
    for r in rows:
        r["reason"] = classify(r)

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["reason"], []).append(r)
    for label, pred in CROSSCUTS:
        block = [r for r in rows if pred(r, str(r.get("detail") or ""))]
        if block:
            grouped[label] = block

    written = []
    order = [l for l, _ in BUCKETS] + [l for l, _ in CROSSCUTS]
    ranked = sorted(grouped, key=lambda k: order.index(k) if k in order else 99)
    for i, label in enumerate(ranked, 1):
        block = grouped[label]
        target = out_dir / f"{i:02d}_{_slug(label)}_{len(block)}.{fmt}"
        frame = _frame(block)
        frame.to_csv(target, index=False) if fmt == "csv" else frame.to_excel(target, index=False)
        written.append(target)
    return written


def build_outcomes(records: list[dict], path: str | Path,
                   sheet_filter: str = "") -> Path:
    """Write the outcome workbook. ``sheet_filter`` limits it to one input sheet."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = list(records)
    if sheet_filter:
        rows = [r for r in rows if str(r.get("sheet", "")) == sheet_filter]
    for r in rows:
        r["reason"] = classify(r)

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["reason"], []).append(r)

    order = [label for label, _ in BUCKETS] + sorted(
        k for k in grouped if k not in {label for label, _ in BUCKETS})

    summary = pd.DataFrame(
        [{"outcome": k, "sites": len(grouped.get(k, []))} for k in order if grouped.get(k)]
        or [{"outcome": "no rows", "sites": 0}])
    summary.loc[len(summary)] = {"outcome": "TOTAL", "sites": len(rows)}

    # buckets are mutually exclusive, so these sum to the total; the
    # cross-cuts below deliberately overlap them and are counted separately
    cross = [(label, [r for r in rows if pred(r, str(r.get("detail") or ""))])
             for label, pred in CROSSCUTS]

    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        summary.to_excel(xl, sheet_name="Summary", index=False)
        _frame(rows).to_excel(xl, sheet_name=_tab("All rows", len(rows)), index=False)
        for label in order:
            block = grouped.get(label)
            if block:
                _frame(block).to_excel(xl, sheet_name=_tab(label, len(block)), index=False)
        for label, block in cross:
            if block:
                _frame(block).to_excel(xl, sheet_name=_tab(label, len(block)), index=False)
    return path
