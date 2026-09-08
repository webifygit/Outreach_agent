"""Crash-safe progress tracking so a re-run resumes instead of re-spamming."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

# A site we actually reached. "uncertain" counts: the form was submitted, we
# just could not read a confirmation - resubmitting would be a second message.
CONTACTED_STATUSES = {"sent", "success", "uncertain"}

# What is kept per approach in an entry's "touches" list. Deliberately not the
# whole record: row_index and company_name belong to the sheet, not the touch.
TOUCH_FIELDS = ("timestamp", "sheet", "script", "follow_up", "method", "status",
                "detail", "contact_page", "email_used", "sender", "screenshot_after")


def norm_site(url: str, scope: str = "host") -> str:
    """Canonical key for a site, so trivial URL differences are not new sites.

    https://Example.com/, http://www.example.com and example.com/ all collapse
    to "example.com". Host, not registered domain: two businesses can share a
    directory domain (biz-a.thinklocal.co.za, biz-b.thinklocal.co.za) and must
    stay distinct.
    """
    text = str(url or "").strip().lower()
    if not text:
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text):
        text = "https://" + text
    parts = urlparse(text)
    host = parts.netloc.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if scope == "url":
        return host + parts.path.rstrip("/")
    return host


def split_touches(records) -> tuple[list[dict], list[dict]]:
    """(first contacts, follow-ups) from raw state records.

    One row per business in the first list, taken from its FIRST approach: a
    follow-up overwrites the top-level fields of the record it lands on, so
    reading those shows the second message in place of the first and counts the
    business again under a second heading.

    Approaches imported from a sheet of contacts made by hand are left out of
    both. They are kept in state so the agent never writes to those businesses
    cold, but they are not its own work and inflate any count of it.

    The two lists deliberately overlap by business: a followed-up business
    appears in each, once as a first contact and once per follow-up. Adding the
    totals is therefore wrong, which is why they are reported side by side.
    """
    firsts: list[dict] = []
    followups: list[dict] = []
    for rec in records:
        touches = rec.get("touches") or [rec]
        first = touches[0]
        if (first.get("status") in CONTACTED_STATUSES
                and not first.get("follow_up") and not first.get("manual")):
            row = dict(first)
            row.setdefault("website", rec.get("website"))
            row.setdefault("company_name", rec.get("company_name"))
            firsts.append(row)
        for touch in touches:
            if touch.get("follow_up") and touch.get("status") in CONTACTED_STATUSES:
                row = dict(touch)
                row.setdefault("website", rec.get("website"))
                row.setdefault("company_name", rec.get("company_name"))
                row["first_contacted"] = rec.get("first_contacted", "")
                followups.append(row)
    return firsts, followups


class State:
    def __init__(self, path: str | Path, dedupe_scope: str = "host", sheet: str = ""):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scope = dedupe_scope
        # Which sheet a row came from. Without it every batch collapses into one
        # undifferentiated pool and "how did batch 37 do?" has no answer.
        self.sheet = sheet
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}
        self._reindex()

    def _reindex(self) -> None:
        """Indexes of what has actually been contacted, by site and by address."""
        self._sites: dict[str, dict] = {}
        self._emails: dict[str, dict] = {}
        for url, entry in self.data.items():
            self._index(url, entry)

    def _index(self, url: str, entry: dict) -> None:
        # An earlier touch that did reach the business still counts, even when
        # the newest attempt did not. Without this a follow-up that came back
        # "skipped_no_email" would hide the successful first approach and the
        # site would read as never contacted - which is how it looks to both
        # the duplicate guard and the message-less form guard.
        if not self._ever_contacted(entry):
            return
        key = norm_site(url, self.scope)
        if key:
            self._sites[key] = entry
        addr = str(entry.get("email_used") or "").strip().lower()
        if addr:
            self._emails[addr] = entry

    @staticmethod
    def _ever_contacted(entry: dict) -> bool:
        if entry.get("status") in CONTACTED_STATUSES:
            return True
        return any(t.get("status") in CONTACTED_STATUSES
                   for t in (entry.get("touches") or []))

    def contacted_site(self, url: str) -> dict | None:
        """The earlier record for this site, if we already reached it."""
        return self._sites.get(norm_site(url, self.scope))

    def contacted_address(self, address: str) -> dict | None:
        """The earlier record for this email address, if we already wrote to it.

        Keyed on the address itself, so two different sites resolving to one
        mailbox - an agency, or one company with several domains - do not each
        get their own message.
        """
        return self._emails.get(str(address or "").strip().lower())

    def is_done(self, url: str) -> bool:
        # dry_run is a rehearsal, not a completed attempt - it must never
        # block a real (live) attempt at the same row from actually running.
        entry = self.data.get(url)
        # skipped_no_email is not a verdict about the site - email was simply
        # switched off at the time. Turning it back on must pick these up again.
        return bool(entry) and entry.get("status") not in {
            None, "", "error", "dry_run", "skipped_no_email"}

    def get(self, url: str) -> dict | None:
        return self.data.get(url)

    def record(self, url: str, result: dict) -> None:
        if self.sheet:
            result.setdefault("sheet", self.sheet)
        prior = self.data.get(url)
        if prior:
            # A second touch must not erase the first. This dict is keyed by
            # URL and used to be overwritten outright, so a follow-up run threw
            # away the date, the evidence and the form that the original
            # approach used - the very record proving we had earned the right
            # to follow up. Keep every attempt in "touches", oldest first, and
            # let the top-level fields stay the latest so nothing else has to
            # change.
            history = list(prior.get("touches") or [])
            if not history:
                history = [{k: prior.get(k, "") for k in TOUCH_FIELDS}]
            history.append({k: result.get(k, "") for k in TOUCH_FIELDS})
            result["touches"] = history
            result["touch_count"] = len(history)
            first = history[0].get("timestamp", "")
            if first:
                result["first_contacted"] = first
        self.data[url] = result
        self._index(url, result)
        self.flush()

    def touches(self, url: str) -> list[dict]:
        """Every recorded approach to this site, oldest first."""
        entry = self._sites.get(norm_site(url, self.scope))
        if not entry:
            return []
        return list(entry.get("touches") or [{k: entry.get(k, "") for k in TOUCH_FIELDS}])

    def flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def sent_today(self, day: str) -> int:
        return sum(
            1 for v in self.data.values()
            if v.get("method") == "email"
            and v.get("status") == "sent"
            and str(v.get("timestamp", "")).startswith(day)
        )
