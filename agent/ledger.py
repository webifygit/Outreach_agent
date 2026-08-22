"""A standing record of every site we actually reached, split by how.

Rebuilt from state.json on every run, so it is always the whole history rather
than the last batch: two sheets, one for forms submitted and one for emails
sent, plus a Skipped sheet showing what was held back as a duplicate and why.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .state import CONTACTED_STATUSES

FORM_COLS = ["website", "company_name", "contact_page", "status", "sender",
             "sender_email", "detail", "screenshot_after", "timestamp"]
MAIL_COLS = ["website", "company_name", "email_used", "status", "sender",
             "sender_email", "detail", "screenshot_after", "timestamp"]
SKIP_COLS = ["website", "company_name", "method", "email_used", "contact_page",
             "first_contacted", "detail", "timestamp"]


def _frame(rows: list[dict], cols: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df:
            df[c] = ""
    if df.empty:
        return pd.DataFrame(columns=cols)
    return df[cols].sort_values("timestamp")


def _sheet_name(base: str, n: int) -> str:
    return f"{base} ({n})"[:31]   # Excel caps sheet names at 31 chars


def build_ledger(records: list[dict], path: str | Path) -> Path:
    """Write the contacted ledger. Returns the path written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    reached = [r for r in records if r.get("status") in CONTACTED_STATUSES]
    forms = [r for r in reached if r.get("method") == "form"]
    mails = [r for r in reached if r.get("method") == "email"]
    skipped = [r for r in records if r.get("status") == "skipped_duplicate"]

    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        _frame(forms, FORM_COLS).to_excel(
            xl, sheet_name=_sheet_name("Forms submitted", len(forms)), index=False)
        _frame(mails, MAIL_COLS).to_excel(
            xl, sheet_name=_sheet_name("Emails sent", len(mails)), index=False)
        _frame(skipped, SKIP_COLS).to_excel(
            xl, sheet_name=_sheet_name("Already approached", len(skipped)), index=False)
    return path
