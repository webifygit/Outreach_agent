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
    ("email_confirm", re.compile(r"(confirm|verify|repeat|re-?enter|again).{0,12}e-?mail|"
                                r"e-?mail.{0,12}(confirm|verify|again|2)")),
    ("email",     re.compile(r"\be-?mail\b|email|correo|courriel")),
    ("phone",     re.compile(r"phone|mobile|tel(ephone)?\b|contact ?(no|number)|whatsapp")),
    ("company",   re.compile(r"company|organi[sz]ation|business|firm|employer|brand")),
    ("website",   re.compile(r"website|web ?site|\burl\b|domain|company site")),
    ("first_name", re.compile(r"first[ _-]?name|fname|given[ _-]?name|\bfirst\b")),
    ("last_name", re.compile(r"last[ _-]?name|lname|surname|family[ _-]?name|\blast\b")),
    # Before "subject" on purpose: that pattern matches "title", so a Job Title
    # box used to be handed the subject line instead of the sender's role.
    # Before subject/message: "How did you hear about us?" is not the enquiry.
    ("heard_about", re.compile(r"hear(d)? about|how did you (hear|find|learn)|find us|"
                               r"where did you (hear|find|see)|referr(al|ed)|lead ?source|\bsource\b")),
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
    ("address",   re.compile(r"(?<!e-mail )(?<!email )\baddress\b|street|\baddr\b|"
                             r"addr[_-]?(line)?[_-]?\d|address[_-]?(line)?[_-]?\d")),
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

# "was successful" as well as "successfully": georgianhousehotel.co.uk says
# "Your submission was successful." and was scored a failure, 2026-09-25.
# ...and "NO, THANKS" is a pop-up's dismiss button, not a confirmation - it
# scored crowngroupofhotels.com a success off a newsletter offer, 2026-09-25.
SUCCESS_TEXT_RE = re.compile(
    r"(?<!no, )(?<!no,)(?<!no )thank(s| you)|we('| ha)?ve received|message (has been )?sent|successfully|"
    r"(was|has been) successful|"
    r"submission received|we'?ll (get back|be in touch)|form submitted|"
    r"your (message|enquiry|inquiry|request) has been",
    re.I,
)

ERROR_TEXT_RE = re.compile(
    r"(is )?required|please (enter|fill|select|complete)|invalid|"
    r"something went wrong|could not be sent|try again|"
    r"fields? (have|has) an error",
    re.I,
)

CONSENT_RE = re.compile(
    r"consent|agree|privacy|terms|policy|gdpr|permission|authori[sz]e|accept|"
    r"condition|acknowledg|i have read|i confirm", re.I
)
MARKETING_OPTIN_RE = re.compile(r"newsletter|subscribe|marketing|updates|promotion", re.I)

# Tick-box and radio groups asking what the enquiry is about, or how to reply:
# take the general option and email - never one that books, subscribes,
# applies or complains. Asked for so a person only has the CAPTCHA left to do
# (care homes, 2026-09-29): "What are you looking for? Book a visit / Request a
# brochure / General enquiry / Contact us" was being left for them to tick.
GENERAL_OPTION_RE = re.compile(r"general|enquir|inquir|contact us|question|information|something else", re.I)
AVOID_OPTION_RE = re.compile(
    r"book|visit|tour|brochure|viewing|appointment|call ?back|newsletter|subscri|marketing|"
    r"\bjobs?\b|career|vacanc|recruit|complain|feedback|admission|respite|\bfees?\b|price|quote|"
    r"\bother\b|please specify|updates|offers", re.I)
CONTACT_PREF_RE = re.compile(r"e-?mail|phone|telephone|\bcall\b|\btext\b|\bsms\b|post", re.I)
# "Are you a resident / family member / professional?" - Pest Stop is a business.
BUSINESS_OPTION_RE = re.compile(r"professional|business|supplier|company|trade|organi[sz]ation|partner|commercial|contractor", re.I)
CONTACT_ME_RE = re.compile(r"(contact|reply to|respond to|get back to|call) me|you (may|can) contact me", re.I)


def _option_text(field: dict) -> str:
    """An option's own words: its desc without the name the whole group shares."""
    name = (field.get("name") or "").lower()
    return field["desc"].replace(name, " ") if name else field["desc"]

# Options that ask for more than a click - "other" usually reveals a text box
# that is then required, and an opt-in is not ours to accept.
# (\b here was typed as literal backspaces since 2026-08-29, so "other" never matched.)
SKIP_OPTION_RE = re.compile(r"\bother\b|please specify|newsletter|subscribe|marketing", re.I)

# An unmapped select left on its "- Please select -" placeholder is chosen for
# when its label says it routes the enquiry. staygenerator.com rejected the
# form over "Who should we send your enquiry to?", enforced by script rather
# than the required attribute, 2026-09-25. Optional-looking selects stay put:
# choosing one can reveal further required fields.
ROUTING_SELECT_RE = re.compile(
    r"\*|required|enquir|inquir|department|who should|send (it|this|your|us|to)|"
    r"recipient|nature of|reason|topic|regarding|hotel|property|venue", re.I)
# The team a pest-control offer is for, when the select lets us say so.
PREFERRED_OPTION_RE = re.compile(
    r"general|facilit|maintenance|operations|management|procure|purchas|supplier|head office",
    re.I)


def truthful_option(field: dict) -> dict | None:
    """An option we can honestly choose for a select we did not map to a role.

    Only a general-enquiry or business-contact option is true of us. A service
    form's "What pest problem do you have?" or "Preferred appointment date" has
    no honest answer, because we are not a customer booking a job - so this
    returns None and the caller leaves the field for a person rather than
    inventing a reply that lands a fake service request in someone's queue.
    """
    opts = [o for o in field.get("options", []) if o.get("value") and o["value"] not in {"0", "-1"}]
    for rx in (PREFERRED_OPTION_RE, GENERAL_OPTION_RE, BUSINESS_OPTION_RE):
        pick = next((o for o in opts
                     if rx.search(o.get("text") or "")
                     and not AVOID_OPTION_RE.search(o.get("text") or "")
                     and not SKIP_OPTION_RE.search(o.get("text") or "")), None)
        if pick:
            return pick
    return None


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
        # The type shortcut must not let a "Confirm email" box take the primary
        # email role - both are type=email, and whichever comes first in the DOM
        # would win, leaving the real one blank.
        if f["type"] == "email":
            confirm = dict(ROLE_PATTERNS)["email_confirm"].search(desc)
            key = "email_confirm" if confirm else "email"
            if key not in roles:
                roles[key] = f
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

    # Even the shortest tier is too long: trim it, but end on a whole sentence
    # where one falls reasonably near the limit, and otherwise on a whole word -
    # so a cut message never stops mid-phrase.
    clipped = rendered[-1][:maxlength]
    sentence_end = max(clipped.rfind(". "), clipped.rfind("! "), clipped.rfind("? "))
    if sentence_end >= maxlength * 0.6:
        return clipped[:sentence_end + 1]
    return clipped.rsplit(" ", 1)[0].rstrip(",.;: ")


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
        "email_confirm": s.get("email", ""),
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
        # How these businesses were found: a Google (Maps) search.
        "heard_about": s.get("heard_about", "Google search"),
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


# HTMLElement.click() works on a checkbox or radio that is styled out of sight
# (opacity 0, 0x0, under a label) and fires the input/change events a site
# listens for. Never un-ticks: an already checked box is left alone.
TICK_JS = """(el) => {
  if (!el.checked) el.click();
  if (!el.checked) {
    el.checked = true;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
  return el.checked;
}"""


async def _tick(frame, sel: str, visible: bool) -> bool:
    """Tick a checkbox or radio - a real click when it can be seen."""
    if visible:
        try:
            await frame.check(sel, timeout=4000)
            return True
        except Exception:
            pass
    try:
        return bool(await frame.eval_on_selector(sel, TICK_JS))
    except Exception:
        return False


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


# Text boxes that hold a value and still fail validation - the site's own
# check has not caught up with a value that arrived without keystrokes.
INVALID_WITH_VALUE_JS = r"""(form) => Array.from(form.elements).filter((el) => {
  if (!el.willValidate || el.checkValidity() || !el.getAttribute('data-agent-id')) return false;
  const typed = el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' &&
    !['checkbox', 'radio', 'file', 'hidden', 'range', 'color'].includes(el.type));
  return typed && (el.value || '').trim() !== '';
}).map((el) => el.getAttribute('data-agent-id'))"""


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
    # Fields a person must finish because we have no honest value for them.
    # Collected (not fabricated) when truthful_only is on, so the row can be
    # routed to the human queue instead of submitting a made-up answer.
    needs_human: list[str] = []
    truthful_only = bool(cfg.path("form", "truthful_only", default=False))

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

    # Selects we did not map but which need an answer: required ones, and ones
    # left on a blank placeholder whose label says they route the enquiry.
    # Prefer the team a pest-control offer is for, never "Other" (it usually
    # opens a required text box), else the first real option.
    for field in form["fields"]:
        if field["tag"] != "select" or not fillable(field):
            continue
        if any(f is field for f in roles.values()):
            continue
        routing = (field["visible"] and field.get("value") == ""
                   and ROUTING_SELECT_RE.search(field["desc"] or ""))
        if not (field["required"] or routing):
            continue
        try:
            opts = [o for o in field["options"] if o["value"] and o["value"] not in {"0", "-1"}]
            if truthful_only:
                # Never fall back to "the first real option": that is how a
                # service form's pest-type or date dropdown got a false answer.
                pick = truthful_option(field)
                if not pick:
                    missing_required.append(f"dropdown needs a person: {(field['desc'] or 'select')[:50]}")
                    needs_human.append(f"dropdown: {(field['desc'] or 'select')[:50]}")
                    continue
            else:
                pick = (next((o for o in opts if PREFERRED_OPTION_RE.search(o["text"])), None)
                        or next((o for o in opts if not SKIP_OPTION_RE.search(o["text"])), None)
                        or (opts[0] if opts else None))
            if pick:
                sel = f"[data-agent-id='{field['agent_id']}']"
                if await _set_value(frame, sel, pick["value"], is_select=True):
                    filled[f"select:{(field['desc'] or '')[:24]}"] = pick["text"][:40]
        except Exception:
            pass

    # Consent checkboxes: tick required ones, never tick marketing opt-ins. A
    # custom-styled box keeps its real input out of sight, so a hidden one is
    # ticked too when it is required or says it is consent - the one on
    # hotel-lancaster-hydepark.com ("I accept that the form entries are checked
    # for spam") was skipped as invisible and the form refused, 2026-09-25. A
    # hidden, unlabelled, optional checkbox is still left alone: a honeypot.
    if cfg.path("form", "accept_consent_checkboxes", default=True):
        for field in form["fields"]:
            if field["type"] != "checkbox" or field.get("checked"):
                continue
            if MARKETING_OPTIN_RE.search(field["desc"]):
                continue
            if field["required"] or CONSENT_RE.search(field["desc"]):
                sel = f"[data-agent-id='{field['agent_id']}']"
                if await _tick(frame, sel, field["visible"]):
                    filled[f"tick:{field['desc'][:24]}"] = "checked"

    # Enquiry-type and reply-method groups, required or not.
    picked_groups: set[str] = set()
    choice_groups: dict[str, list[dict]] = {}
    for field in form["fields"]:
        if field["type"] in ("checkbox", "radio") and fillable(field):
            choice_groups.setdefault(field.get("name") or field["agent_id"], []).append(field)
    for name, options in choice_groups.items():
        if any(o.get("checked") for o in options):
            continue
        texts = [(o, _option_text(o)) for o in options]
        if len(options) == 1:
            o, t = texts[0]
            if CONTACT_ME_RE.search(t) and not MARKETING_OPTIN_RE.search(t):
                if await _tick(frame, f"[data-agent-id='{o['agent_id']}']", o["visible"]):
                    filled[f"tick:{t.strip()[:24]}"] = "checked"
            continue
        if all(CONSENT_RE.search(t) or MARKETING_OPTIN_RE.search(t) for _, t in texts):
            continue                       # consent and opt-ins have their own rule
        pick = next((o for o, t in texts if GENERAL_OPTION_RE.search(t)
                     and not AVOID_OPTION_RE.search(t)), None)
        if pick is None:
            pick = next((o for o, t in texts if BUSINESS_OPTION_RE.search(t)
                         and not AVOID_OPTION_RE.search(t)), None)
        if pick is None and all(CONTACT_PREF_RE.search(t) for _, t in texts):
            pick = next((o for o, t in texts if re.search(r"e-?mail", t)), None)
        if pick is not None and await _tick(frame, f"[data-agent-id='{pick['agent_id']}']", pick["visible"]):
            filled[f"choice:{name[:24]}"] = _option_text(pick).strip()[:40]
            picked_groups.add(name)

    # Required radio groups - "Please select a vehicle brand", "preferred
    # dealer". Left blank the form is rejected and the site falls back to
    # email, which is how a contactable business ends up emailed instead.
    # There is no good answer to give, so take the first real option.
    # Custom-styled radios hide their inputs like checkboxes do (hestonhydehotel
    # .com, "Telephone / Email", 2026-09-25), so a required one counts even
    # when it cannot be seen.
    groups: dict[str, list[dict]] = {}
    for field in form["fields"]:
        if field["type"] != "radio" or not fillable(field):
            continue
        groups.setdefault(field.get("name") or field["agent_id"], []).append(field)

    for name, options in groups.items():
        if name in picked_groups or any(o.get("checked") for o in options):
            continue                       # already chosen - by the site or above
        if not any(o["required"] for o in options):
            continue                       # optional - leave it alone
        if truthful_only:
            # Only a general-enquiry / business option is honest; a "which pest?"
            # or "preferred date" radio has no true answer for us, so tick
            # nothing and flag the row for a person.
            pick = next((o for o in options
                         if (PREFERRED_OPTION_RE.search(o["desc"]) or GENERAL_OPTION_RE.search(o["desc"])
                             or BUSINESS_OPTION_RE.search(o["desc"]))
                         and not AVOID_OPTION_RE.search(o["desc"])
                         and not SKIP_OPTION_RE.search(o["desc"])), None)
            if not pick:
                missing_required.append(f"choice needs a person: {name[:40]}")
                needs_human.append(f"choice: {name[:40]}")
                continue
            if await _tick(frame, f"[data-agent-id='{pick['agent_id']}']", pick["visible"]):
                filled[f"radio:{name[:24]}"] = pick["desc"][:40]
            continue
        for option in options:
            if SKIP_OPTION_RE.search(option["desc"]):
                continue                   # not "other", which usually opens a text box
            if await _tick(frame, f"[data-agent-id='{option['agent_id']}']", option["visible"]):
                filled[f"radio:{name[:24]}"] = option["desc"][:40]
                break

    # Last resort for a required field we could not name. Every site invents its
    # own ("leadtypeid", "dropdown_1", "new_customer_select", "model"), and one
    # of them left unfilled fails the browser's own validity check, which blocks
    # the submit and loses the whole approach - 488 rows in the 74k run alone.
    # A plausible value beats silence: the enquiry still reaches a human, who can
    # read the message. Never touches checkboxes, radios or selects - those have
    # their own passes above - and never invents a number where a real one
    # matters more than submitting, so budget-like fields are left alone.
    for field in form["fields"]:
        if not (field["required"] and fillable(field)):
            continue
        if field["type"] in {"checkbox", "radio"} or field["tag"] == "select":
            continue
        if any(f is field for f in roles.values()):
            continue
        desc = field["desc"]
        guess = _last_resort_value(field, values, desc)
        if not guess:
            missing_required.append(desc[:60] or field["type"])
            continue
        sel = f"[data-agent-id='{field['agent_id']}']"
        try:
            if await _set_value(frame, sel, guess, is_select=False):
                filled[f"required?:{desc[:24]}"] = guess[:40]
            else:
                missing_required.append(desc[:60] or field["type"])
        except Exception:  # noqa: BLE001
            missing_required.append(desc[:60] or field["type"])

    # A value set without keystrokes can leave a site's own validation behind.
    # stghotel.com kept "Please fill out this field." under a Subject box that
    # plainly held the subject, and concept-apartments.co.uk (Contact Form 7)
    # kept "The field is required." under a filled email box, 2026-09-25: both
    # re-check on keyup or change, which a programmatic fill never fires. Type
    # those values again, key by key, and leave the box so change fires.
    # A phone box can refuse the number however it is typed: then try the
    # other ways of writing it until the box accepts one.
    phone_ids = {f["agent_id"] for f in form["fields"]
                 if f.get("type") == "tel" or f is roles.get("phone")}
    for agent_id in await _invalid_with_value(frame, form["form_key"]):
        loc = frame.locator(f"[data-agent-id='{agent_id}']")
        try:
            value = await loc.input_value(timeout=2000)
            tries = [value]
            if agent_id in phone_ids and values.get("phone"):
                tries += phone_variants(values["phone"])
            for attempt in tries:
                await loc.fill("", timeout=3000)
                await loc.press_sequentially(attempt, timeout=60000)
                await loc.press("Tab", timeout=2000)
                filled[f"retyped:{agent_id}"] = attempt[:40]
                if agent_id not in await _invalid_with_value(frame, form["form_key"]):
                    break
        except Exception:  # noqa: BLE001
            continue

    # A phone box whose site refused the number in its own way: type it the
    # other ways until the site stops complaining (+44... first, then 02...).
    if values.get("phone"):
        for field in form["fields"]:
            if field["agent_id"] not in phone_ids or not fillable(field):
                continue
            sel = f"[data-agent-id='{field['agent_id']}']"
            # Most sites judge a phone box when it loses focus, and a value set
            # by script never makes it lose focus - leave it first, as a person
            # tabbing on would, so the site has had its say before we look.
            try:
                await frame.eval_on_selector(sel, """el => {
                  for (const t of ['input', 'change']) el.dispatchEvent(new Event(t, {bubbles: true}));
                  el.dispatchEvent(new FocusEvent('blur')); el.dispatchEvent(new FocusEvent('focusout', {bubbles: true}));
                }""")
                await frame.wait_for_timeout(400)
            except Exception:  # noqa: BLE001
                pass
            if not await _phone_refused(frame, sel):
                continue
            loc = frame.locator(sel)
            for attempt in phone_variants(values["phone"]):
                try:
                    await loc.fill("", timeout=3000)
                    await loc.press_sequentially(attempt, timeout=30000)
                    await loc.press("Tab", timeout=2000)
                    await frame.wait_for_timeout(400)
                except Exception:  # noqa: BLE001
                    break
                filled[f"phone-as:{field['agent_id']}"] = attempt
                if not await _phone_refused(frame, sel):
                    break

    return {"filled": filled, "roles": list(roles), "missing_required": missing_required,
            "needs_human": needs_human}


def phone_variants(phone: str) -> list[str]:
    """The same number written the other ways phone boxes insist on.

    "+442034112329" is refused by boxes that take digits only, by UK boxes
    that want 0 and ten digits, and by ones that already show +44 in front -
    a care-home form turned Pest Stop's number away, 2026-09-29.
    """
    digits = re.sub(r"\D", "", phone)
    out: list[str] = []
    if digits.startswith("44") and len(digits) >= 11:
        out += ["0" + digits[2:], digits[2:]]          # 02034112329, 2034112329
    if digits:
        out.append(digits)                             # 442034112329
    return [v for v in dict.fromkeys(out) if v != phone]


# Is this phone box refusing what it holds? Not only the browser's own check:
# plenty of sites mark the box (aria-invalid, an "error" class) or print their
# own "Please enter a valid phone number" beside it after it loses focus.
PHONE_REFUSED_JS = r"""(el) => {
  if (!el.checkValidity()) return true;
  if (el.getAttribute('aria-invalid') === 'true') return true;
  if (/(^|\s)(error|invalid|is-invalid|has-error|wpcf7-not-valid|parsley-error)(\s|$)/i.test(el.className)) return true;
  const box = el.closest('.form-group, .field, .gfield, .wpcf7-form-control-wrap, p, li, div') || el.parentElement;
  const text = box ? box.innerText : '';
  return /(valid|correct|proper).{0,25}(phone|telephone|mobile|number)|(phone|telephone|mobile|number).{0,40}(invalid|not valid|incorrect|not recogni)/i.test(text);
}"""


async def _phone_refused(frame, sel: str) -> bool:
    try:
        return bool(await frame.eval_on_selector(sel, PHONE_REFUSED_JS))
    except Exception:
        return False


async def _invalid_with_value(frame, form_key: str) -> list[str]:
    try:
        return await frame.eval_on_selector(
            f"form[data-agent-form='{form_key}']", INVALID_WITH_VALUE_JS) or []
    except Exception:
        return []


# Budget/quantity boxes where a made-up number is worse than an unsent form, and
# anything that reads like a date the site will parse itself.
_NO_GUESS = re.compile(r"budget|price|amount|quantity|how many|number of|salary|"
                       r"captcha|security code|verification code|answer", re.I)


def _last_resort_value(field: dict, values: dict, desc: str) -> str:
    """A plausible value for a required field we could not classify."""
    if _NO_GUESS.search(desc):
        return ""
    typ = field.get("type") or ""
    if typ == "email":
        return values.get("email", "")
    if typ == "tel":
        return values.get("phone", "")
    if typ == "url":
        return values.get("website", "")
    if typ == "number":
        return "1"
    if typ == "date":
        return __import__("datetime").date.today().isoformat()
    if field.get("tag") == "textarea":
        return values.get("message", "") or values.get("company", "")
    # A plain text box: the company name is always true, never nonsense, and
    # reads sensibly whatever the field turns out to have been asking for.
    return values.get("company", "")


SOURCE_OPTION_RE = re.compile(r"google|search|internet|online|\bweb", re.I)


async def _select_best(frame, sel, field, role, value):
    """Choose the option whose text best matches, else the most sensible one.

    "The first real option" used to be the fallback, which for an enquiry-type
    select is often "Book a visit", and for a county one "Aberdeenshire".
    """
    opts = [o for o in field["options"] if o["value"]]
    if not opts:
        return
    target = value.lower()
    for o in opts:
        text = (o["text"] or "").lower().strip()
        # "Greater London" matches an option "London" as well as the reverse.
        if target and text and (target in text or (len(text) >= 4 and text in target)):
            await _set_value(frame, sel, o["value"], is_select=True)
            return
    real = [o for o in opts if o["value"] not in {"0", "-1", ""}] or opts
    fits = lambda rx: next((o for o in real if rx.search(o["text"] or "")
                            and not AVOID_OPTION_RE.search(o["text"] or "")), None)
    pick = ((fits(SOURCE_OPTION_RE) if role == "heard_about" else None)
            or fits(GENERAL_OPTION_RE) or fits(PREFERRED_OPTION_RE) or fits(BUSINESS_OPTION_RE)
            or next((o for o in real if not SKIP_OPTION_RE.search(o["text"] or "")
                     and not AVOID_OPTION_RE.search(o["text"] or "")), None)
            or real[0])
    await _set_value(frame, sel, pick["value"], is_select=True)


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


def _fresh_error(body: str, before_body: str) -> str | None:
    """An error phrase that was NOT already on the page before submitting.

    "required", "please select" and "invalid" sit on plenty of pages from the
    start - a Cookiebot banner ("...may also be required"), a "- Please
    select -" dropdown placeholder, a "(required)" label. Matching those
    anywhere reported the banner as the form's error on every Clermont hotel,
    2026-09-25, and would fail a form that had in fact gone through.
    """
    before = _norm(before_body)
    for match in ERROR_TEXT_RE.finditer(body):
        probe = _norm(body[max(0, match.start() - 25): match.end() + 25])
        if before and probe in before:
            continue
        return body[max(0, match.start() - 60): match.end() + 60].replace("\n", " ").strip()[:140]
    return None


# How many text-like controls in the form still hold a value.
FILLED_COUNT_JS = r"""(form) => {
  let n = 0;
  for (const el of form.elements) {
    const t = (el.type || '').toLowerCase();
    if (el.tagName === 'TEXTAREA' || ['text', 'email', 'tel', ''].includes(t)) {
      if (el.tagName !== 'SELECT' && (el.value || '').trim()) n++;
    }
  }
  return n;
}"""


async def _form_was_cleared(scope, form_key: str) -> bool:
    """Every text box we filled is empty again - the site reset the form.

    Only called after the pre-submit validity check passed, so the browser
    had nothing to block. An AJAX form that clears itself after posting then
    reports every required box as empty, which read as "browser rejected the
    form" - seen on georgianhousehotel.co.uk (which also said "Your submission
    was successful") and lensbury.com, 2026-09-25.
    """
    try:
        return await scope.eval_on_selector(
            f"form[data-agent-form='{form_key}']", FILLED_COUNT_JS) == 0
    except Exception:
        return False


# A CAPTCHA that only appears once Submit is pressed (Wix shows "Verification
# - Please confirm you're human"). 270 such blocked submissions had been
# recorded as "probably sent" before this existed, 2026-10-01.
CHALLENGE_TEXT_RE = re.compile(
    r"confirm you.{0,4}r?e? (a )?human|verify (that )?you are (a )?human|are you a robot|"
    r"i'?m not a robot|complete the (captcha|security check)", re.I)


async def _challenge_after_submit(page, body: str, before_body: str) -> str:
    before = _norm(before_body)
    for hit in CHALLENGE_TEXT_RE.finditer(body):
        probe = _norm(body[max(0, hit.start() - 25): hit.end() + 25])
        if not before or probe not in before:
            return hit.group(0)
    try:
        for f in page.frames:
            if re.search(r"recaptcha/api2/bframe|hcaptcha\.com/.*challenge", f.url or ""):
                el = await f.frame_element()
                box = await el.bounding_box()
                if box and box["width"] > 50 and box["height"] > 50 and await el.is_visible():
                    return "picture challenge"
    except Exception:
        pass
    return ""


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

    challenge = await _challenge_after_submit(page, body, before_body)
    if challenge:
        return "failed", f"a CAPTCHA appeared after submit ('{challenge}') - not sent", "strong"

    fresh = _fresh_confirmation(body, before_body)
    stale_note = ""
    if fresh:
        snippet, stale = fresh
        if snippet:
            return "success", f"confirmation text: {snippet}", "strong"
        stale_note = (f" (ignored '{stale}' - already on the page before submitting)"
                      if stale else "")

    error = _fresh_error(body, before_body)
    if error:
        return "failed", f"validation error: {error}", "strong"

    scope = frame if frame is not None else page

    # The browser refusing the submit leaves the form in place with its
    # invalid fields flagged - the clearest evidence nothing was sent. Unless
    # the form is empty: a refused submit keeps what we typed, a reset one
    # does not.
    rejected = await invalid_fields(scope, form_key)
    if rejected and await _form_was_cleared(scope, form_key):
        return "uncertain", f"form cleared after submit - likely sent, no confirmation{stale_note}", "weak"
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
