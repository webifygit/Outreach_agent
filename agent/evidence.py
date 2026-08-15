"""Screenshot capture. One folder per run, predictable filenames."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def slug(url: str) -> str:
    host = urlparse(url).netloc.replace("www.", "") or "unknown"
    return re.sub(r"[^a-z0-9.\-]", "_", host.lower())[:48]


class Evidence:
    def __init__(self, root: str | Path, run_id: str | None = None):
        self.run_id = run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S")
        self.dir = Path(root) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)

    async def shot(self, page, row_index: int, url: str, stage: str) -> str:
        name = f"{row_index:04d}_{slug(url)}_{stage}.png"
        path = self.dir / name
        try:
            await page.screenshot(path=str(path), full_page=True)
        except Exception:
            try:
                await page.screenshot(path=str(path))   # viewport only if full page fails
            except Exception:
                return ""
        return str(path)

    def note(self, row_index: int, url: str, stage: str, text: str) -> str:
        path = self.dir / f"{row_index:04d}_{slug(url)}_{stage}.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)
