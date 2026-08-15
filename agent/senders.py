"""Round-robin sender-identity rotation.

Two independent pools: `form_senders` (typed into contact form fields) and
`email_senders` (which Gmail account actually sends the fallback email).
Each row gets one identity per pool by row_index % count, so the same site
always gets the same identity across resumed/re-run batches. Falls back to
a single flat `sender:` block for configs that don't use either pool.
"""
from __future__ import annotations


def pick_sender(cfg, row_index: int, pool: str = "form_senders") -> dict:
    people = cfg.get(pool) or []
    if not people:
        return dict(cfg.get("sender", {}) or {})

    org = dict(cfg.get("organization", {}) or {})
    person = dict(people[row_index % len(people)])
    merged = {**org, **person}
    merged["name"] = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
    return merged
