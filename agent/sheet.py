"""Read the input spreadsheet and write the results workbook.

Expected input columns (case-insensitive, extras are preserved and passed to
the templates as variables):

    website        required  - e.g. https://example.com
    company_name   optional  - falls back to the domain name
    email          optional  - fallback address if no contact form is found
    contact_name   optional  - used to personalise the greeting
    notes          optional  - free text, available in templates as {{ notes }}
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

ALIASES = {
    # Plurals included: scraped exports (Google Maps, Apify and friends) label
    # the column WEBSITES, and the loader used to reject the whole sheet with
    # "No website column found" rather than take the obvious match.
    "website": {"website", "websites", "url", "urls", "site", "sites", "web", "domain",
                "domains", "website link", "website url", "link", "links", "weblink",
                "web address", "homepage"},
    "company_name": {"company_name", "company", "company name", "name", "organisation",
                     "organization", "business", "client", "client name"},
    "email": {"email", "e-mail", "email id", "email address", "mail", "contact email"},
    "contact_name": {"contact_name", "contact", "contact person", "person", "first name",
                     "contact name"},
    "notes": {"notes", "note", "remark", "remarks", "comment"},
}


def _canonical(col: str) -> str:
    key = str(col).strip().lower()
    for canon, names in ALIASES.items():
        if key in names:
            return canon
    return key.replace(" ", "_")


HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", re.I
)


def normalise_url(raw: str) -> str:
    """Return a usable https:// URL, or "" if raw doesn't look like a real domain.

    Guards against spreadsheet link cells where only the display text (e.g.
    "Home", "Click here") came through instead of the actual URL - without
    this, that text gets turned into a bogus "https://Home" that Playwright
    can't navigate to.
    """
    raw = str(raw or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    host = urlparse(raw).netloc.split(":")[0]
    if not HOSTNAME_RE.match(host):
        return ""
    return raw


def company_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    host = host.replace("www.", "")
    core = host.split(".")[0] if host else "there"
    return core.replace("-", " ").title()


def load_rows(path: str | Path) -> tuple[list[dict], list[dict]]:
    """Return (rows, skipped) - skipped rows had a missing/unusable website value."""
    path = Path(path)
    if path.suffix.lower() in {".csv", ".tsv"}:
        df = pd.read_csv(path, sep="\t" if path.suffix.lower() == ".tsv" else ",")
    else:
        df = pd.read_excel(path)

    df.columns = [_canonical(c) for c in df.columns]
    if "website" not in df.columns:
        raise ValueError(
            f"No website column found. Columns present: {list(df.columns)}. "
            "Rename the URL column to 'website'."
        )

    rows: list[dict] = []
    skipped: list[dict] = []
    for i, rec in enumerate(df.to_dict(orient="records")):
        rec = {k: ("" if pd.isna(v) else v) for k, v in rec.items()}
        raw_website = rec.get("website", "")
        url = normalise_url(raw_website)
        if not url:
            skipped.append({"row_index": i + 2, "website": str(raw_website)})
            continue
        rec["website"] = url
        given = str(rec.get("company_name") or "").strip()
        # A domain-derived name is a guess, not the company's own name -
        # flagged so the page can be asked for something better.
        rec["company_from_sheet"] = bool(given)
        rec["company_name"] = given or company_from_url(url)
        rec["row_index"] = i + 2  # spreadsheet row number, header is row 1
        contact = str(rec.get("contact_name") or "").strip()
        rec["contact_first_name"] = contact.split()[0] if contact else ""
        rows.append(rec)
    return rows, skipped


RESULT_COLUMNS = [
    "row_index", "website", "company_name", "sender", "sender_email", "method", "status", "detail",
    "contact_page", "email_used", "screenshot_before", "screenshot_after",
    "timestamp",
]


def write_results(results: list[dict], out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(results)
    for col in RESULT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[RESULT_COLUMNS + [c for c in df.columns if c not in RESULT_COLUMNS]]
    if out_path.suffix.lower() == ".csv":
        df.to_csv(out_path, index=False)
    else:
        df.to_excel(out_path, index=False)
    return out_path
