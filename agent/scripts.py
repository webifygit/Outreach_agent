"""Pitch scripts: one campaign's wording, chosen per run.

A sheet of pest control companies wants the pest control pitch; a mixed sheet
wants the general one. Both use the same senders, the same duplicate guard and
the same delivery settings - only the words change. So a script carries the
words and nothing else:

    scripts:
      - key: pest_control
        label: "Pest control"
        blurb: "Pest Stop Control case study"
        email: {subject_template: ..., body_template_path: ..., html_template_path: ...}
        form:  {subject_line: ..., message_tiers: {...}}

Anything else a script declares is ignored on purpose. Delivery lives in the
top-level `email:`/`form:` blocks - whether email is enabled at all, the daily
limit, SMTP - so picking a different pitch in the UI can never quietly resume
paused sending or lift a limit someone set deliberately.
"""
from __future__ import annotations

# The only keys a script may set, per config block.
WORDING_KEYS = {
    "email": ("subject_template", "body_template_path", "html_template_path"),
    "form": ("subject_line", "message_tiers"),
}

# Used when the config has no `scripts:` block at all - the wording sitting in
# email:/form: is then the one script there is.
FALLBACK_KEY = "config"
FALLBACK_LABEL = "Config default"
FALLBACK_BLURB = "The wording already in config email: / form:"


def configured(cfg) -> list[dict]:
    """Every usable entry from `scripts:`, in config order."""
    out: list[dict] = []
    for item in (cfg.get("scripts") or []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        out.append({
            "key": key,
            "label": str(item.get("label") or key.replace("_", " ").title()),
            "blurb": str(item.get("blurb") or ""),
            "email": item.get("email") or {},
            "form": item.get("form") or {},
        })
    return out


def catalogue(cfg) -> list[dict]:
    """What the console offers. Never empty: a run with no `scripts:` block is
    still perfectly able to go out, so the picker must not imply otherwise."""
    found = configured(cfg)
    if found:
        return [{"key": s["key"], "label": s["label"], "blurb": s["blurb"]} for s in found]
    return [{"key": FALLBACK_KEY, "label": FALLBACK_LABEL, "blurb": FALLBACK_BLURB}]


def default_key(cfg) -> str:
    """The script a run gets when nobody picked one."""
    explicit = str(cfg.get("default_script") or "").strip()
    if explicit:
        return explicit
    found = configured(cfg)
    return found[0]["key"] if found else FALLBACK_KEY


def label_for(cfg, key: str) -> str:
    for entry in catalogue(cfg):
        if entry["key"] == key:
            return entry["label"]
    return key


def apply(cfg, key: str | None) -> tuple[str, str]:
    """Overlay one script's wording onto cfg, in place. Returns (key, label).

    An unrecognised key raises rather than falling back to the default: a sheet
    of pest control companies quietly receiving the general pitch is a worse
    outcome than a run that refuses to start.
    """
    wanted = (key or "").strip() or default_key(cfg)
    found = configured(cfg)

    if not found:
        if wanted == FALLBACK_KEY:
            return FALLBACK_KEY, FALLBACK_LABEL
        raise ValueError(
            f"no scripts are configured, so {wanted!r} cannot be selected - "
            "add a scripts: block to the config"
        )

    chosen = next((s for s in found if s["key"] == wanted), None)
    if chosen is None:
        raise ValueError(
            f"unknown script {wanted!r} - configured: "
            + ", ".join(s["key"] for s in found)
        )

    for block, allowed in WORDING_KEYS.items():
        section = cfg.get(block)
        if not isinstance(section, dict):
            section = {}
            cfg[block] = section
        for name in allowed:
            if name in (chosen.get(block) or {}):
                section[name] = chosen[block][name]

    return chosen["key"], chosen["label"]
