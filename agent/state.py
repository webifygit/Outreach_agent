"""Crash-safe progress tracking so a re-run resumes instead of re-spamming."""
from __future__ import annotations

import json
from pathlib import Path


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def is_done(self, url: str) -> bool:
        entry = self.data.get(url)
        return bool(entry) and entry.get("status") not in {None, "", "error"}

    def get(self, url: str) -> dict | None:
        return self.data.get(url)

    def record(self, url: str, result: dict) -> None:
        self.data[url] = result
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
