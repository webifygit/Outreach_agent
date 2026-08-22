"""Crash-safe progress tracking so a re-run resumes instead of re-spamming."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

# A site we actually reached. "uncertain" counts: the form was submitted, we
# just could not read a confirmation - resubmitting would be a second message.
CONTACTED_STATUSES = {"sent", "success", "uncertain"}


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


class State:
    def __init__(self, path: str | Path, dedupe_scope: str = "host"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scope = dedupe_scope
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
        if entry.get("status") not in CONTACTED_STATUSES:
            return
        key = norm_site(url, self.scope)
        if key:
            self._sites[key] = entry
        addr = str(entry.get("email_used") or "").strip().lower()
        if addr:
            self._emails[addr] = entry

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
        return bool(entry) and entry.get("status") not in {None, "", "error", "dry_run"}

    def get(self, url: str) -> dict | None:
        return self.data.get(url)

    def record(self, url: str, result: dict) -> None:
        self.data[url] = result
        self._index(url, result)
        self.flush()

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
