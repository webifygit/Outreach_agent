"""Config loading + small helpers."""
from __future__ import annotations

import random
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


def default_config_path(root: str | Path) -> Path:
    """config.local.yaml (gitignored, real identities) if present, else config.yaml."""
    root = Path(root)
    local = root / "config.local.yaml"
    return local if local.exists() else root / "config.yaml"
