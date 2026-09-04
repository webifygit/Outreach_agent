"""Map form fields to roles, fill them, submit, and verify the result.

Everything here takes the *frame* that owns the form rather than the page.
For a form in the main document that frame is ``page.main_frame``; for an
embedded one it is the iframe's frame. Playwright locators never cross a
frame boundary, so scoping to the owner is what makes both cases identical.
"""
from __future__ import annotations

import re

from .templating import render_file

# Largest-first: the first one that fits the field's maxlength (if any) wins.
DEFAULT_MESSAGE_TIERS = {
    "full": "templates/form_message_full.txt.j2",
    "medium": "templates/form_message_medium.txt.j2",
    "short": "templates/form_message_700.txt.j2",
    "shortest": "templates/form_message_500.txt.j2",
}

# Ordered: the first pattern that matches a field wins, so put specific before generic.
ROLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email",     re.compile(r"\be-?mail\b|email|correo|courriel")),
    ("phone",     re.compile(r"phone|mobile|tel(ephone)?\b|contact ?(no|number)|whatsapp")),
    ("company",   re.compile(r"company|organi[sz]ation|business|firm|employer|brand")),
    ("website",   re.compile(r"website|web ?site|\burl\b|domain|company site")),
    ("first_name", re.compile(r"first[ _-]?name|fname|given[ _-]?name|\bfirst\b")),
    ("last_name", re.compile(r"last[ _-]?name|lname|surname|family[ _-]?name|\blast\b")),
    ("subject",   re.compile(r"subject|regarding|topic|reason|enquiry ?type|inquiry ?type|title")),
    ("message",   re.compile(r"message|comment|enquir|inquir|detail|describe|question|"
                             r"requirement|project|how can we help|tell us|body|content|brief")),
    ("name",      re.compile(r"\bname\b|full[ _-]?name|your name|contact person")),
    ("postcode",  re.compile(r"zip|postal|post ?code|postcode|pin ?code|eircode")),
    ("city",      re.compile(r"\bcity\b|town|location")),
    ("country",   re.compile(r"country|nation")),
    ("budget",    re.compile(r"budget|price range|investment")),
]

SUBMIT_SELECTORS = [
    "button[type=submit]",
    "input[type=submit]",
    "button:has-text('Send')",
    "button:has-text('Submit')",
    "button:has-text('Send Message')",
    "button:has-text('Get in touch')",
    "button:has-text('Enquire')",
    "button:has-text('Contact')",
    "a:has-text('Submit')",
    "button",
]

SUCCESS_TEXT_RE = re.compile(
    r"thank(s| you)|we('| ha)?ve received|message (has been )?sent|successfully|"
    r"submission received|we'?ll (get back|be in touch)|form submitted|"
    r"your (message|enquiry|inquiry|request) has been",
    re.I,
)

ERROR_TEXT_RE = re.compile(
    r"(is )?required|please (enter|fill|select|complete)|invalid|"
    r"something went wrong|could not be sent|try again",
    re.I,
)

CONSENT_RE = re.compile(
    r"consent|agree|privacy|terms|policy|gdpr|permission|authori[sz]e|i accept", re.I
)
MARKETING_OPTIN_RE = re.compile(r"newsletter|subscribe|marketing|updates|promotion", re.I)

# Options that ask for more than a click - "other" usually reveals a text box
# that is then required, and an opt-in is not ours to accept.
SKIP_OPTION_RE = re.compile(r"other|please specify|newsletter|subscribe|marketing", re.I)


def classify(fields: list[dict]) -> dict[str, dict]:
    """Assign one field per role. Only visible, fillable fields are considered.

    Hidden fields are deliberately skipped - most of them are honeypots, and
    filling one is the fastest way to get silently binned as a bot.
    """
    roles: dict[str, dict] = {}
    textareas = [f for f in fields if f["tag"] == "textarea" and f["visible"]]

    for f in fields:
        if not f["visible"] or f["type"] in {"hidden", "file", "password"}:
            continue
        if f["type"] in {"checkbox", "radio"}:
            continue
        desc = f["desc"]
        if f["type"] == "email" and "email" not in roles:
            roles["email"] = f
            continue
        if f["type"] == "tel" and "phone" not in roles:
            roles["phone"] = f
            continue
        for role, pattern in ROLE_PATTERNS:
            if role in roles:
                continue
            if pattern.search(desc):
                roles[role] = f
                break

    # A lone textarea is the message box even if it is unlabelled.
    if "message" not in roles and textareas:
        roles["message"] = textareas[0]

    # Unlabelled text inputs: fall back to positional guessing (name, email, ...).
    if "name" not in roles and "first_name" not in roles:
        for f in fields:
            if (f["visible"] and f["tag"] == "input" and f["type"] in {"text", ""}
                    and f not in roles.values()):
                roles["name"] = f
                break
    return roles


def render_tiered_message(env, cfg, ctx: dict, maxlength: int | None) -> str:
    """Render each length tier and return the largest one that fits maxlength.

    With no maxlength (field has none, or wasn't found) the full-length
    version is used. If even the shortest tier doesn't fit, it's truncated
    at the last word boundary that does.
    """
    tiers = cfg.path("form", "message_tiers", default=DEFAULT_MESSAGE_TIERS)
    paths = [tiers.get(k, v) for k, v in DEFAULT_MESSAGE_TIERS.items()] if isinstance(tiers, dict) else list(DEFAULT_MESSAGE_TIERS.values())
    rendered = [render_file(env, cfg, p, ctx) for p in paths]

    if not maxlength:
        return rendered[0]
    for text in rendered:
        if len(text) <= maxlength:
            return text

    shortest = rendered[-1]
    return shortest[:maxlength].rsplit(" ", 1)[0].rstrip(",.;: ")


def values_for(cfg, ctx: dict, message: str, subject: str) -> dict[str, str]:
    s = ctx.get("sender", {}) or {}
    return {
        "name": s.get("name", ""),
        "first_name": s.get("first_name", "") or s.get("name", "").split(" ")[0],
        "last_name": s.get("last_name", "") or " ".join(s.get("name", "").split(" ")[1:]),
        "email": s.get("email", ""),
        "phone": s.get("phone", ""),
        "company": s.get("company", ""),
        "website": s.get("website", ""),
        "city": s.get("city", ""),
        "country": s.get("country", ""),
        # A required zip field left blank fails the whole submission - and it is
        # required far more often than it looks, especially on US service sites
        # that route enquiries by area.
        "postcode": s.get("postcode", ""),
        "subject": subject,
        "message": message,
        "budget": "",
    }


async def fill_form(frame, form: dict, cfg, ctx: dict, env, subject: str) -> dict:
    """Fill every mapped field. Returns a report of what was written.

    ``frame`` is the document that owns the form (page.main_frame, or the
    iframe's frame for an embedded form).
    """
    roles = classify(form["fields"])
    msg_field = roles.get("message")
    message = render_tiered_message(env, cfg, ctx, msg_field.get("maxlength") if msg_field else None)
    values = values_for(cfg, ctx, message, subject)

    # A form with a first-name field and no last-name field is almost always
    # asking for the whole name - an input called "firstname" whose label reads
    # "What's your name?" is a common pattern. Filling only "Irshad" there sends
    # a nameless-looking enquiry, so use the full name when nothing else on the
    # form can carry the surname.
    if "first_name" in roles and "last_name" not in roles and "name" not in roles:
        values["first_name"] = values["name"] or values["first_name"]

    filled: dict[str, str] = {}
    missing_required: list[str] = []

    for role, field in roles.items():
        value = values.get(role, "")
        if not value:
            continue
        sel = f"[data-agent-id='{field['agent_id']}']"
        try:
            if field["tag"] == "select":
                await _select_best(frame, sel, field, role, value)
                filled[role] = "(select)"
            else:
                await frame.fill(sel, value, timeout=5000)
                filled[role] = value[:60]
        except Exception as exc:  # noqa: BLE001
            filled[role] = f"FAILED: {type(exc).__name__}"

    # Selects we did not map but which are required - pick the first real option.
    for field in form["fields"]:
        if field["tag"] != "select" or not field["visible"] or not field["required"]:
            continue
        if any(f is field for f in roles.values()):
            continue
        try:
            opts = [o for o in field["options"] if o["value"] and o["value"] not in {"0", "-1"}]
            if opts:
                await frame.select_option(
                    f"[data-agent-id='{field['agent_id']}']", value=opts[0]["value"], timeout=5000
                )
        except Exception:
            pass

    # Consent checkboxes: tick required ones, never tick marketing opt-ins.
    if cfg.path("form", "accept_consent_checkboxes", default=True):
        for field in form["fields"]:
            if field["type"] != "checkbox" or not field["visible"]:
                continue
            if MARKETING_OPTIN_RE.search(field["desc"]):
                continue
            if field["required"] or CONSENT_RE.search(field["desc"]):
                try:
                    await frame.check(f"[data-agent-id='{field['agent_id']}']", timeout=4000)
                except Exception:
                    pass

    # Required radio groups - "Please select a vehicle brand", "preferred
    # dealer". Left blank the form is rejected and the site falls back to
    # email, which is how a contactable business ends up emailed instead.
    # There is no good answer to give, so take the first real option.
    groups: dict[str, list[dict]] = {}
    for field in form["fields"]:
        if field["type"] != "radio" or not field["visible"]:
            continue
        groups.setdefault(field.get("name") or field["agent_id"], []).append(field)

    for name, options in groups.items():
        if any(o.get("checked") for o in options):
            continue                       # the site already picked one
        if not any(o["required"] for o in options):
            continue                       # optional - leave it alone
        for option in options:
            if SKIP_OPTION_RE.search(option["desc"]):
                continue                   # not "other", which usually opens a text box
            try:
                await frame.check(f"[data-agent-id='{option['agent_id']}']", timeout=4000)
                filled[f"radio:{name[:24]}"] = option["desc"][:40]
                break
            except Exception:
                continue

    for field in form["fields"]:
        if field["required"] and field["visible"] and field["type"] not in {"checkbox", "radio"}:
            if not any(f is field for f in roles.values()):
                missing_required.append(field["desc"][:60] or field["type"])

    return {"filled": filled, "roles": list(roles), "missing_required": missing_required}


async def _select_best(frame, sel, field, role, value):
    """Choose the option whose text best matches, else the first real option."""
    opts = [o for o in field["options"] if o["value"]]
    if not opts:
        return
    target = value.lower()
    for o in opts:
        if target and target in (o["text"] or "").lower():
            await frame.select_option(sel, value=o["value"], timeout=5000)
            return
    real = [o for o in opts if o["value"] not in {"0", "-1", ""}]
    await frame.select_option(sel, value=(real or opts)[0]["value"], timeout=5000)


async def submit_form(frame, form_key: str, timeout_ms: int) -> str:
    """Click the submit control. Returns a short description of what was clicked."""
    scope = f"form[data-agent-form='{form_key}']"
    for sel in SUBMIT_SELECTORS:
        try:
            loc = frame.locator(f"{scope} {sel}").first
            if await loc.count() == 0 or not await loc.is_visible():
                continue
            label = (await loc.inner_text() or "").strip()[:40]
            await loc.click(timeout=timeout_ms)
            return f"clicked:{sel}:{label}"
        except Exception:
            continue
    # Last resort: submit the form element itself.
    try:
        await frame.eval_on_selector(scope, "f => f.requestSubmit ? f.requestSubmit() : f.submit()")
        return "requestSubmit()"
    except Exception as exc:  # noqa: BLE001
        return f"no_submit_control:{type(exc).__name__}"


async def _read_body(target) -> str:
    """body text of a page or frame; "" if it is gone or unreadable."""
    try:
        return (await target.inner_text("body"))[:8000]
    except Exception:
        return ""


async def verify_submission(page, frame, before_url: str, form_key: str) -> tuple[str, str]:
    """Best-effort read of whether the submission landed.

    Returns (status, detail) where status is success | uncertain | failed.

    An embedded form renders its "thanks, we got it" inside the iframe, and the
    parent page's body text does not include it - so the frame is read first and
    the page second, and either one carrying a confirmation counts.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(2500)

    after_url = page.url
    body = ""
    if frame is not None and frame is not page.main_frame:
        # Submitting can detach and replace the embed's frame; that on its own
        # is a decent success signal, but only alongside the text checks below.
        body = await _read_body(frame)
    page_body = await _read_body(page)
    body = (body + chr(10) + page_body).strip() if body else page_body

    if after_url != before_url and re.search(r"thank|success|sent|submitted", after_url, re.I):
        return "success", f"redirected to {after_url}"

    if SUCCESS_TEXT_RE.search(body):
        match = SUCCESS_TEXT_RE.search(body)
        snippet = body[max(0, match.start() - 40): match.end() + 60].replace("\n", " ")
        return "success", f"confirmation text: {snippet.strip()[:140]}"

    if ERROR_TEXT_RE.search(body):
        match = ERROR_TEXT_RE.search(body)
        snippet = body[max(0, match.start() - 60): match.end() + 60].replace("\n", " ")
        return "failed", f"validation error: {snippet.strip()[:140]}"

    scope = frame if frame is not None else page
    try:
        if await scope.locator(f"form[data-agent-form='{form_key}']").count() == 0:
            return "success", "form removed from page after submit"
    except Exception:
        # Reading a detached frame throws - the embed tore its form down, which
        # is what a hosted form does once it has accepted the submission.
        if frame is not None and frame is not page.main_frame:
            try:
                if frame.is_detached():
                    return "success", "form iframe replaced after submit"
            except Exception:
                pass

    if after_url != before_url:
        return "uncertain", f"navigated to {after_url}, no confirmation text found"
    return "uncertain", "no confirmation or error detected - check the screenshot"
