"""Jinja2 rendering for the form message, email subject and email body."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Undefined


class Blank(Undefined):
    """Unknown variables render as empty string instead of exploding mid-run."""

    def __str__(self) -> str:
        return ""

    def __bool__(self) -> bool:
        return False


def make_env(root: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(root)),
        undefined=Blank,
        trim_blocks=False,
        lstrip_blocks=False,
        autoescape=False,
    )


def context_for(cfg, row: dict, sender: dict | None = None) -> dict:
    """Every spreadsheet column is available as a template variable."""
    ctx = dict(row)
    ctx.setdefault("contact_first_name", "")
    ctx["sender"] = sender if sender is not None else cfg.get("sender", {})
    ctx["today"] = __import__("datetime").date.today().isoformat()
    return ctx


def render_file(env: Environment, cfg, rel_path: str, ctx: dict) -> str:
    rel = str(Path(rel_path).as_posix())
    return env.get_template(rel).render(**ctx).strip()


def render_string(env: Environment, text: str, ctx: dict) -> str:
    return env.from_string(text).render(**ctx).strip()
