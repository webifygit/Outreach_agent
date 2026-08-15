"""Find the contact page, the form on it, and any published email address."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

CONTACT_TEXT_RE = re.compile(
    r"contact|get\s*in\s*touch|reach\s*us|enquir|inquir|kontakt|talk\s*to\s*us|"
    r"work\s*with\s*us|request\s*a\s*quote|book\s*a\s*demo|write\s*to\s*us",
    re.I,
)

COMMON_CONTACT_PATHS = [
    "/contact", "/contact-us", "/contact-us/", "/contactus", "/contact.html",
    "/contact.php", "/get-in-touch", "/enquiry", "/inquiry", "/reach-us",
    "/about/contact", "/pages/contact", "/en/contact",
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

JUNK_EMAIL_RE = re.compile(
    r"(example|sentry|wixpress|\.png|\.jpg|\.jpeg|\.gif|\.webp|@2x|domain\.com|"
    r"yourdomain|email@|name@|user@)",
    re.I,
)

CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='challenges.cloudflare.com']",
    ".g-recaptcha",
    ".h-captcha",
    ".cf-turnstile",
    "[data-sitekey]",
]

# JS that tags every candidate field with data-agent-id and returns a description
# of each form on the page. Running one script beats dozens of round trips.
EXTRACT_FORMS_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) < 0.05)
      return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    if (r.left + r.width < -500 || r.top + r.height < -2000) return false;
    return true;
  };

  const describe = (el) => {
    const bits = [
      el.getAttribute('name'), el.id, el.getAttribute('placeholder'),
      el.getAttribute('aria-label'), el.getAttribute('autocomplete'),
      el.getAttribute('title'), el.className,
    ];
    let label = '';
    if (el.id) {
      try {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l) label = l.innerText;
      } catch (e) {}
    }
    if (!label) {
      const parent = el.closest('label');
      if (parent) label = parent.innerText;
    }
    if (!label) {
      const wrap = el.closest('div,p,li,td');
      if (wrap) {
        const l = wrap.querySelector('label');
        if (l) label = l.innerText;
      }
    }
    bits.push(label);
    return bits.filter(Boolean).join(' ').replace(/\s+/g, ' ').toLowerCase().slice(0, 300);
  };

  const forms = [];
  document.querySelectorAll('form').forEach((form, fi) => {
    form.setAttribute('data-agent-form', 'form-' + fi);
    const fields = [];
    let ei = 0;
    form.querySelectorAll('input, textarea, select').forEach((el) => {
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute('type') || (tag === 'textarea' ? 'textarea' : 'text')).toLowerCase();
      if (['submit', 'button', 'image', 'reset'].includes(type)) return;
      const id = 'f' + fi + '-e' + (ei++);
      el.setAttribute('data-agent-id', id);
      const rawMax = el.maxLength;
      fields.push({
        agent_id: id,
        tag: tag,
        type: type,
        required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
        visible: visible(el) && type !== 'hidden',
        maxlength: (typeof rawMax === 'number' && rawMax > 0 && rawMax < 100000) ? rawMax : null,
        desc: describe(el),
        options: tag === 'select'
          ? Array.from(el.options).map(o => ({ value: o.value, text: (o.text || '').trim() })).slice(0, 60)
          : [],
      });
    });

    let submitText = '';
    const btn = form.querySelector(
      'button[type=submit], input[type=submit], button:not([type]), [role=button]'
    );
    if (btn) submitText = (btn.innerText || btn.value || '').trim().slice(0, 60);

    forms.push({
      form_key: 'form-' + fi,
      action: form.getAttribute('action') || '',
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      desc: [form.id, form.className, form.getAttribute('name')]
        .filter(Boolean).join(' ').toLowerCase().slice(0, 200),
      visible: visible(form),
      submit_text: submitText,
      fields: fields,
    });
  });
  return forms;
}
"""


def score_form(form: dict) -> int:
    """Higher = more likely to be a real contact form. Negative = reject."""
    fields = [f for f in form["fields"] if f["visible"]]
    types = {f["type"] for f in fields}
    blob = form["desc"] + " " + " ".join(f["desc"] for f in fields) + " " + form["submit_text"].lower()

    if "password" in types:
        return -100                      # login / signup
    if not form["visible"] or not fields:
        return -100
    if any(t in types for t in ("search",)) or re.search(r"\bsearch\b", form["desc"]):
        return -100
    if len(fields) <= 2 and "textarea" not in types:
        # single email box = newsletter signup, not a contact form
        return -50 if re.search(r"newsletter|subscribe|signup|sign-up", blob) else -20

    score = 0
    if "textarea" in types:
        score += 40
    if any(f["type"] == "email" or "email" in f["desc"] or "e-mail" in f["desc"] for f in fields):
        score += 25
    if re.search(r"contact|enquir|inquir|message|get in touch|quote", blob):
        score += 25
    if re.search(r"send|submit|enquir|inquir|get in touch", form["submit_text"], re.I):
        score += 10
    score += min(len(fields), 6) * 3
    if re.search(r"newsletter|subscribe|login|sign.?in|search", form["desc"]):
        score -= 30
    return score


async def has_captcha(page) -> str:
    for sel in CAPTCHA_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return sel
        except Exception:
            continue
    return ""


async def collect_emails(page, site_host: str) -> list[str]:
    """Prefer mailto: links, then anything in the raw HTML. Same-domain first."""
    found: list[str] = []
    try:
        hrefs = await page.eval_on_selector_all(
            "a[href^='mailto:']", "els => els.map(e => e.getAttribute('href'))"
        )
        for h in hrefs or []:
            addr = h.replace("mailto:", "").split("?")[0].strip()
            if addr and EMAIL_RE.fullmatch(addr):
                found.append(addr)
    except Exception:
        pass
    try:
        html = await page.content()
        found.extend(EMAIL_RE.findall(html))
    except Exception:
        pass

    seen, clean = set(), []
    for addr in found:
        addr = addr.strip().strip(".,;:'\"").lower()
        if addr in seen or JUNK_EMAIL_RE.search(addr):
            continue
        seen.add(addr)
        clean.append(addr)

    root = ".".join(site_host.replace("www.", "").split(".")[-2:])
    clean.sort(key=lambda a: (root not in a.split("@")[-1], len(a)))
    return clean[:5]


async def find_contact_links(page, base_url: str, limit: int) -> list[str]:
    """Links on the current page whose text or href smells like a contact page."""
    try:
        anchors = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.getAttribute('href'), text: (e.innerText||'').trim()}))",
        )
    except Exception:
        return []

    host = urlparse(base_url).netloc
    scored: list[tuple[int, str]] = []
    seen = set()
    for a in anchors or []:
        href, text = a.get("href") or "", a.get("text") or ""
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href).split("#")[0]
        if urlparse(full).netloc != host or full in seen:
            continue
        seen.add(full)
        weight = 0
        if CONTACT_TEXT_RE.search(text):
            weight += 3
        if CONTACT_TEXT_RE.search(href):
            weight += 2
        if weight:
            scored.append((weight, full))

    scored.sort(key=lambda t: -t[0])
    return [u for _, u in scored[:limit]]


async def page_summary(page, max_chars: int = 500) -> str:
    """Title + meta description + first heading - context for the local LLM hook."""
    try:
        data = await page.evaluate(
            """() => {
                const meta = (name) => document.querySelector(`meta[name="${name}"]`)?.content
                    || document.querySelector(`meta[property="og:${name}"]`)?.content || '';
                const bits = [document.title || '', meta('description'),
                    document.querySelector('h1')?.innerText || ''];
                return bits.filter(Boolean).join(' | ');
            }"""
        )
    except Exception:
        return ""
    return " ".join((data or "").split())[:max_chars]


def guessed_paths(base_url: str) -> list[str]:
    parts = urlparse(base_url)
    root = f"{parts.scheme}://{parts.netloc}"
    return [root + p for p in COMMON_CONTACT_PATHS]
