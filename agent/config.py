"""Config loading + small helpers."""
from __future__ import annotations

import os
import random
import re
from pathlib import Path

import yaml


class Config(dict):
    """dict with attribute-ish access via .get_path()."""

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        cfg = cls(data)
        cfg["_root"] = Path(path).resolve().parent
        return cfg

    def path(self, *keys, default=None):
        node = self
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self["_root"] / p

    @property
    def live(self) -> bool:
        return str(self.path("run", "mode", default="dry_run")).lower() == "live"


def jitter(pair, default=(5, 10)) -> float:
    lo, hi = (pair or default)
    return random.uniform(float(lo), float(hi))


def load_env_file(root: str | Path, name: str = ".env.local") -> int:
    """Populate os.environ from .env.local. Returns how many vars were set.

    The repo shipped this loading only inside start_agent.sh, so a plain
    `python run.py` - or anything on Windows, where that bash script cannot
    run at all - saw none of the SMTP passwords and silently fell back to
    "no password set". Doing it here means every entry point behaves the same.

    A variable already present in the environment always wins, so an explicit
    `export`/`$env:` for a one-off run still overrides the file.
    """
    path = Path(root) / name
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def default_config_path(root: str | Path) -> Path:
    """config.local.yaml (gitignored, real identities) if present, else config.yaml."""
    root = Path(root)
    local = root / "config.local.yaml"
    return local if local.exists() else root / "config.yaml"


# ---------------------------------------------------------------------------
# Placeholder identity guard
#
# config.yaml ships as a public template full of stand-in values. Running with
# it untouched types "First Person" from "Your Company Pvt Ltd" into real
# prospects' contact forms, which is worse than not running at all - so a run
# says so loudly, and a LIVE run refuses outright.
# ---------------------------------------------------------------------------

PLACEHOLDER_VALUE_RE = re.compile(
    r"^(first|person|job title|your (company|city|state|country)|"
    r"your company pvt ltd|\+1 000 000 0000|"
    r"one line describing what you offer.*|"
    r"todo|changeme|change me|fill ?me|xxx+)$",
    re.I,
)

PLACEHOLDER_SUBSTR_RE = re.compile(
    r"yourcompany\.com|person\.[ab]@|you@gmail\.com|example\.com", re.I
)

# Only these carry into a contact form or an email - a placeholder anywhere
# else (city, state) is untidy but harmless, so it isn't worth blocking a run.
CHECKED_FIELDS = ("company", "website", "landing_page", "pitch",
                  "first_name", "last_name", "email", "phone", "designation")


def _is_placeholder(value) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(PLACEHOLDER_VALUE_RE.match(text) or PLACEHOLDER_SUBSTR_RE.search(text))


def identity_placeholders(cfg) -> list[str]:
    """Every still-unconfigured identity value, as "where = what" strings."""
    found: list[str] = []

    for key, value in (cfg.get("organization") or {}).items():
        if key in CHECKED_FIELDS and _is_placeholder(value):
            found.append(f"organization.{key} = {value!r}")

    for pool in ("form_senders", "email_senders"):
        for i, person in enumerate(cfg.get(pool) or []):
            if not isinstance(person, dict):
                continue
            for key, value in person.items():
                if key in CHECKED_FIELDS and _is_placeholder(value):
                    found.append(f"{pool}[{i}].{key} = {value!r}")
    return found


def placeholder_advice(config_path) -> str:
    name = Path(config_path).name
    return (
        f"{name} still holds the template's placeholder identity. Copy it to "
        "config.local.yaml (gitignored, loaded in preference) and put your real "
        "company, name, email and phone in there before running."
    )
