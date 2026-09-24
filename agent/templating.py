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


def presentable_company(name: str) -> bool:
    """Is this a name we can put in front of a stranger?

    Single-word brands are fine (Sodexo, Lombard). A long run-together string
    is a domain we split badly ("Secondliftingequipment"), and greeting someone
    by it looks automated - which it is, but it should not look it.
    """
    name = (name or "").strip()
    if not name or len(name) > 60:
        return False
    if " " in name:
        return True
    return len(name) <= 14


def greeting_for(company_name: str, contact_first_name: str = "") -> str:
    """Who the email says hello to."""
    first = (contact_first_name or "").strip()
    if first:
        return first
    company = (company_name or "").strip()
    if presentable_company(company):
        return f"{company} team"
    return "Team"
