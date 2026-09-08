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
    # Before "subject" on purpose: that pattern matches "title", so a Job Title
    # box used to be handed the subject line instead of the sender's role.
    ("designation", re.compile(r"designation|job ?title|your ?title|position|"
                               r"role|occupation|job ?role")),
    ("subject",   re.compile(r"subject|regarding|topic|reason|enquiry ?type|inquiry ?type|title")),
    ("message",   re.compile(r"message|comment|enquir|inquir|detail|describe|question|"
                             r"requirement|project|how can we help|tell us|body|content|brief")),
    ("name",      re.compile(r"\bname\b|full[ _-]?name|your name|contact person")),
    ("postcode",  re.compile(r"zip|postal|post ?code|postcode|pin ?code|eircode")),
    ("city",      re.compile(r"\bcity\b|town|location")),
    ("state",     re.compile(r"\bstate\b|province|\bregion\b|county")),
    # "email address" must stay an email field, so the bare word is only an
    # address when it is not the tail of one of those.
    ("address",   re.compile(r"(?<!e-mail )(?<!email )\baddress\b|street|\baddr\b")),
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


def fillable(field: dict) -> bool:
    """Can we put a value in this field?

    Visible fields, plus any field marked required - a honeypot is never
    required, so a required control hidden behind a JS widget (Select2 and
    friends keep the real <select> at 1x1) is a real field we have to fill.
    Falls back to ``visible`` for records captured before this existed.
    """
    return bool(field.get("fillable", field["visible"]))


def classify(fields: list[dict]) -> dict[str, dict]:
    """Assign one field per role. Only fillable fields are considered.

    Hidden fields are deliberately skipped - most of them are honeypots, and
    filling one is the fastest way to get silently binned as a bot. The
    exception is a field marked required, which no honeypot ever is: see
    ``fillable``.
    """
    roles: dict[str, dict] = {}
    textareas = [f for f in fields if f["tag"] == "textarea" and f["visible"]]

    # Claim the message box FIRST, before any pattern matching. A contact form's
    # message is a textarea essentially every time, and "desc" includes the
    # element's class list - so a plain <input> whose classes happen to contain
    # "content", "detail", "brief" or the like used to match the (deliberately
    # broad) message pattern and take the role, because message is tested before
    # name. The textarea fallback below then never fired, because the role was
    # already taken. The result was the whole pitch typed into the Name box and
    # the real message box left empty, failing the form's own required check -
    # seen on integrand.co.za, 2026-09-08.
    if textareas:
        roles["message"] = textareas[0]

    for f in fields:
        if f in roles.values():
            continue
        if not fillable(f) or f["type"] in {"hidden", "file", "password"}:
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
                if role == "address" and f["type"] in {"email", "tel"}:
                    continue
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


def _address_line(s: dict) -> str:
    """"Ahmedabad, Gujarat 380009, India" from whatever parts are present."""
    line = ", ".join(b for b in [s.get("city", ""), s.get("state", "")] if b)
    if s.get("postcode"):
        line = f"{line} {s['postcode']}".strip()
    if s.get("country"):
        line = f"{line}, {s['country']}" if line else s["country"]
    return line


def values_for(cfg, ctx: dict, message: str, subject: str) -> dict[str, str]:
    s = ctx.get("sender", {}) or {}
    return {
        "name": s.get("name", ""),
        "first_name": s.get("first_name", "") or s.get("name", "").split(" ")[0],
        "last_name": s.get("last_name", "") or " ".join(s.get("name", "").split(" ")[1:]),
        "email": s.get("email", ""),
        # No spaces. Plenty of forms validate a phone field with a pattern that
        # allows digits and the usual punctuation (+ # - * ) but NOT a space,
        # and reject the whole submission with "the field accepts only numbers
        # and phone characters" - seen 2026-09-08 on a site that took the name,
        # email and message fine and failed only on "+91 9819915555". The plus
        # and country code are kept: the recipient has to be able to dial back.
        "phone": re.sub(r"\s+", "", s.get("phone", "")),
        "company": s.get("company", ""),
        "website": s.get("website", ""),
        "city": s.get("city", ""),
        "state": s.get("state", ""),
        "designation": s.get("designation", ""),
        "country": s.get("country", ""),
        # A required zip field left blank fails the whole submission - and it is
        # required far more often than it looks, especially on US service sites
        # that route enquiries by area.
        "postcode": s.get("postcode", ""),
        # Required on US service forms that route by area. Use the explicit
        # address if the identity has one, else build a line from the parts
        # that are already there.
        "address": s.get("address", "") or _address_line(s),
        "subject": subject,
        "message": message,
        "budget": "",
    }


# Setting .value directly bypasses the widget, so fire the events it listens
# for - Select2/Chosen redraw their own label on `change`, and a framework
# form (React, Vue) only records the value when `input` fires.
SET_VALUE_JS = """(el, value) => {
  const proto = el.tagName === 'SELECT' ? window.HTMLSelectElement.prototype
              : el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype
              : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, value);
  if (el.tagName === 'SELECT' && el.value !== value) {
    for (const o of el.options) {
      if ((o.text || '').trim().toLowerCase() === String(value).toLowerCase()) {
        el.selectedIndex = o.index;
        break;
      }
    }
  }
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return el.value;
}"""


async def _set_value(frame, sel: str, value: str, is_select: bool) -> bool:
    """Fill one control, falling back to setting it inside the page.

    Playwright needs the real control to be actionable. A Select2/Chosen
    dropdown keeps it at 1x1 behind its own markup, so the normal call times
    out on a field that is genuinely there and genuinely required.
    """
    try:
        if is_select:
            await frame.select_option(sel, value=value, timeout=4000)
        else:
            await frame.fill(sel, value, timeout=5000)
        return True
    except Exception:
        try:
            await frame.eval_on_selector(sel, SET_VALUE_JS, value)
            return True
        except Exception:
            return False


# Ask the browser itself which fields it would reject. This is the same check
# that silently blocks a submit and leaves the form sitting there.
INVALID_FIELDS_JS = r"""(form) => {
  const out = [];
  for (const el of form.elements) {
    if (!el.willValidate || el.checkValidity()) continue;
    let label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
    if (!label && el.id) {
      try {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l) label = l.innerText;
      } catch (e) {}
    }
    if (!label) {
      const w = el.closest('label') || el.closest('div,p,li,td');
      const l = w && w.querySelector ? w.querySelector('label') : null;
      label = w && w.tagName === 'LABEL' ? w.innerText : (l ? l.innerText : '');
    }
    label = (label || el.getAttribute('name') || el.id || el.type || '')
              .replace(/\s+/g, ' ').trim().slice(0, 50);
    out.push(label + ' (' + (el.validationMessage || 'invalid') + ')');
    if (out.length >= 8) break;
  }
  return out;
}"""


async def invalid_fields(frame, form_key: str) -> list[str]:
    """Fields the browser will refuse to submit, as "label (why)" strings."""
    try:
        return await frame.eval_on_selector(
            f"form[data-agent-form='{form_key}']", INVALID_FIELDS_JS
        )
    except Exception:
        return []


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
            elif await _set_value(frame, sel, value, is_select=False):
                filled[role] = value[:60]
            else:
                filled[role] = "FAILED: could not set value"
        except Exception as exc:  # noqa: BLE001
            filled[role] = f"FAILED: {type(exc).__name__}"

    # Selects we did not map but which are required - pick the first real option.
    for field in form["fields"]:
        if field["tag"] != "select" or not fillable(field) or not field["required"]:
            continue
        if any(f is field for f in roles.values()):
            continue
        try:
            opts = [o for o in field["options"] if o["value"] and o["value"] not in {"0", "-1"}]
            if opts:
                sel = f"[data-agent-id='{field['agent_id']}']"
                if await _set_value(frame, sel, opts[0]["value"], is_select=True):
                    filled[f"select:{(field['desc'] or '')[:24]}"] = opts[0]["text"][:40]
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
        if field["required"] and fillable(field) and field["type"] not in {"checkbox", "radio"}:
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
            await _set_value(frame, sel, o["value"], is_select=True)
            return
    real = [o for o in opts if o["value"] not in {"0", "-1", ""}]
    await _set_value(frame, sel, (real or opts)[0]["value"], is_select=True)


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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


async def read_context_body(page, frame) -> str:
    """The text verify_submission will read, for capturing before a submit."""
    body = ""
    if frame is not None and frame is not page.main_frame:
        body = await _read_body(frame)
    page_body = await _read_body(page)
    return (body + chr(10) + page_body).strip() if body else page_body


def _fresh_confirmation(body: str, before_body: str) -> tuple[str | None, str] | None:
    """A confirmation phrase that was NOT already on the page before submitting.

    "Thank you", "we have received", "Thanks" are extremely common in review
    carousels, footers and cookie banners. Matching one anywhere in the body
    scored a success for pages that had simply never been submitted to - so a
    phrase only counts if it appeared as a result of the submission.
    """
    before = _norm(before_body)
    stale = ""
    for match in SUCCESS_TEXT_RE.finditer(body):
        window = body[max(0, match.start() - 40): match.end() + 60].replace("\n", " ").strip()
        probe = _norm(body[max(0, match.start() - 25): match.end() + 25])
        if before and probe in before:
            stale = stale or window[:90]
            continue
        return window[:140], ""
    return (None, stale) if stale else None


async def verify_submission(page, frame, before_url: str, form_key: str,
                            before_body: str = "") -> tuple[str, str, str]:
    """Best-effort read of whether the submission landed.

    Returns (status, detail, strength). status is success | uncertain | failed;
    strength is strong when the page actually changed in a way only a real
    submission explains, weak when it is an inference.

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
    # Submitting can detach and replace the embed's frame; that on its own
    # is a decent success signal, but only alongside the text checks below.
    body = await read_context_body(page, frame)

    if after_url != before_url and re.search(r"thank|success|sent|submitted", after_url, re.I):
        return "success", f"redirected to {after_url}", "strong"

    fresh = _fresh_confirmation(body, before_body)
    stale_note = ""
    if fresh:
        snippet, stale = fresh
        if snippet:
            return "success", f"confirmation text: {snippet}", "strong"
        stale_note = (f" (ignored '{stale}' - already on the page before submitting)"
                      if stale else "")

    if ERROR_TEXT_RE.search(body):
        match = ERROR_TEXT_RE.search(body)
        snippet = body[max(0, match.start() - 60): match.end() + 60].replace("\n", " ")
        return "failed", f"validation error: {snippet.strip()[:140]}", "strong"

    scope = frame if frame is not None else page

    # The browser refusing the submit leaves the form in place with its
    # invalid fields flagged - the clearest evidence nothing was sent.
    rejected = await invalid_fields(scope, form_key)
    if rejected:
        return "failed", f"browser rejected the form: {rejected}{stale_note}", "strong"

    try:
        if await scope.locator(f"form[data-agent-form='{form_key}']").count() == 0:
            return "success", "form removed from page after submit", "structural"
    except Exception:
        # Reading a detached frame throws - the embed tore its form down, which
        # is what a hosted form does once it has accepted the submission.
        if frame is not None and frame is not page.main_frame:
            try:
                if frame.is_detached():
                    return "success", "form iframe replaced after submit", "structural"
            except Exception:
                pass

    if after_url != before_url:
        return "uncertain", f"navigated to {after_url}, no confirmation text found{stale_note}", "weak"
    return "uncertain", f"no confirmation or error detected - check the screenshot{stale_note}", "weak"
