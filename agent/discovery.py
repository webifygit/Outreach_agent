"""Find the contact page, the form on it, and any published email address.

Forms are looked for in the main document *and* in every iframe on the page,
so third-party embeds (HubSpot, Jotform, Google Forms, ...) are filled like
any other form instead of falling through to the email path.
"""
from __future__ import annotations

import asyncio
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

# The address pattern also matches things that merely look like one: a bundled
# script ("vue@3.5.13.min.js"), or a JSON escape swept out of inline data
# ("u003e@example.com"). Both were actually emailed before this existed.
ASSET_TLD = {
    "js", "mjs", "cjs", "css", "map", "json", "svg", "ico", "webp", "avif",
    "woff", "woff2", "ttf", "otf", "eot", "mp4", "webm", "pdf", "zip", "min",
    "html", "htm", "php", "aspx", "xml", "txt",
    "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff",
}
JSON_ESCAPE_RE = re.compile(r"u00[0-9a-f]{2}|\[ux]", re.I)


def plausible_email(addr: str) -> bool:
    """Does this look like a mailbox someone reads, rather than a filename?"""
    addr = (addr or "").strip().lower()
    if addr.count("@") != 1:
        return False
    local, _, domain = addr.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if JSON_ESCAPE_RE.search(local) or JSON_ESCAPE_RE.search(domain):
        return False
    labels = domain.split(".")
    tld = labels[-1]
    if tld in ASSET_TLD or not tld.isalpha() or not (2 <= len(tld) <= 24):
        return False
    # "3.5.13.min.js" - a version string, not a hostname
    if any(label.isdigit() for label in labels):
        return False
    if not re.match(r"^[a-z0-9._%+-]+$", local):
        return False
    return True

CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='challenges.cloudflare.com']",
    ".g-recaptcha",
    ".h-captcha",
    ".cf-turnstile",
    "[data-sitekey]",
]

# A frame whose URL matches this is a hosted form embed. Such a form is a real
# contact form even when its markup is generic, so it gets a scoring nudge.
EMBED_HOST_RE = re.compile(
    r"hsforms|hubspot|jotform|formstack|wufoo|cognitoforms|tally\.so|fillout\.com|"
    r"paperform|zohopublic|forms\.zoho|docs\.google\.com/forms|forms\.office\.com|"
    r"formsubmit|getform|formspree|gravityforms|typeform|pardot|marketo|activehosted",
    re.I,
)

# Frames that never hold a contact form - skipped so a page stuffed with ad and
# analytics iframes doesn't cost a DOM scan each.
SKIP_FRAME_RE = re.compile(
    r"googletagmanager|googlesyndication|googleadservices|doubleclick|adsbygoogle|"
    r"google-analytics|facebook\.com/(tr|plugins)|connect\.facebook|youtube\.com/embed|"
    r"youtube-nocookie|player\.vimeo|platform\.twitter|/gtm\.|hotjar|intercom|"
    r"gstatic\.com|recaptcha|hcaptcha|challenges\.cloudflare",
    re.I,
)

# Same idea for CAPTCHAs: they live in their own iframe, so the frame URL is
# the most reliable tell - the parent DOM selectors can miss a late injection.
CAPTCHA_URL_RE = re.compile(r"recaptcha|hcaptcha|challenges\.cloudflare|turnstile", re.I)

# A page with hundreds of frames is pathological; cap the scan.
MAX_FRAMES_SCANNED = 15

# page.evaluate() has NO timeout in Playwright - the browser's default timeout
# does not apply to it. A page whose main thread is blocked (heavy corporate
# sites, a script in a busy loop) therefore hangs the extractor forever, and
# with it the whole batch. Every evaluate below is bounded by this instead.
EVAL_TIMEOUT_S = 15.0

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
        name: el.getAttribute('name') || '',
        checked: !!el.checked,
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
    if EMBED_HOST_RE.search(form.get("frame_url", "")):
        # A form served by HubSpot/Jotform/etc is there to be filled in. Their
        # markup is generic (field names like "0-1/email"), which the keyword
        # rules above under-score, so give the provider itself some weight.
        score += 30
    return score


async def has_captcha(page, frame=None) -> str:
    """Detect a CAPTCHA guarding the page, or the frame the form lives in.

    Checked two ways because either alone misses cases: the DOM selectors catch
    a widget the page renders itself, and the frame URLs catch one injected
    later (or one inside a third-party form embed, invisible to the parent DOM).
    """
    targets = [page]
    if frame is not None and frame not in targets:
        targets.append(frame)

    for target in targets:
        for sel in CAPTCHA_SELECTORS:
            try:
                if await target.locator(sel).count() > 0:
                    return sel
            except Exception:
                continue

    try:
        for f in page.frames:
            if CAPTCHA_URL_RE.search(f.url or ""):
                return f"frame:{CAPTCHA_URL_RE.search(f.url).group(0)}"
    except Exception:
        pass
    return ""


async def extract_forms(target) -> list[dict]:
    """Run the extractor against one Page or Frame. Never raises, never hangs."""
    try:
        return await asyncio.wait_for(
            target.evaluate(EXTRACT_FORMS_JS), timeout=EVAL_TIMEOUT_S
        ) or []
    except Exception:
        # Timed out, or: cross-origin frame still navigating, detached
        # mid-scan, a frame that refuses script evaluation - not fatal, just
        # nothing to score.
        return []


def _worth_scanning(frame, is_main: bool) -> bool:
    url = frame.url or ""
    if is_main:
        return True
    if SKIP_FRAME_RE.search(url):
        return False
    try:
        if frame.is_detached():
            return False
    except Exception:
        return False
    return True


async def scan_frames(page, limit: int = MAX_FRAMES_SCANNED) -> list[tuple]:
    """Every form on the page, main document and iframes alike.

    Returns (frame, form) pairs - the frame is carried along because filling,
    submitting and verifying all have to be scoped to the document that owns
    the form. Playwright locators do not cross a frame boundary.
    """
    pairs: list[tuple] = []
    try:
        frames = list(page.frames)[:limit]
    except Exception:
        frames = [page.main_frame]

    for frame in frames:
        is_main = frame is page.main_frame
        if not _worth_scanning(frame, is_main):
            continue
        for form in await extract_forms(frame):
            form["in_iframe"] = not is_main
            form["frame_url"] = "" if is_main else (frame.url or "")
            pairs.append((frame, form))
    return pairs


def _embed_frame_present(page) -> bool:
    try:
        return any(EMBED_HOST_RE.search(f.url or "") for f in page.frames)
    except Exception:
        return False


async def forms_everywhere(page, settle_ms: int = 3000) -> list[tuple]:
    """scan_frames, with one grace period for a slow-booting form embed.

    Hosted embeds mount their form after the parent page has already fired
    domcontentloaded, so a first scan can legitimately find nothing inside an
    iframe that is about to contain the only contact form on the site.
    """
    pairs = await scan_frames(page)
    if not any(form["in_iframe"] for _, form in pairs) and _embed_frame_present(page):
        try:
            await page.wait_for_timeout(settle_ms)
        except Exception:
            return pairs
        pairs = await scan_frames(page)
    return pairs


def rank_forms(pairs: list[tuple]) -> list[tuple]:
    """(score, form, frame), best first."""
    ranked = [(score_form(form), form, frame) for frame, form in pairs]
    ranked.sort(key=lambda t: -t[0])
    return ranked


async def collect_emails(page, site_host: str) -> list[str]:
    """Prefer mailto: links, then anything in the raw HTML. Same-domain first."""
    found: list[str] = []
    try:
        hrefs = await asyncio.wait_for(
            page.eval_on_selector_all(
                "a[href^='mailto:']", "els => els.map(e => e.getAttribute('href'))"),
            timeout=EVAL_TIMEOUT_S)
        for h in hrefs or []:
            addr = h.replace("mailto:", "").split("?")[0].strip()
            if addr and EMAIL_RE.fullmatch(addr):
                found.append(addr)
    except Exception:
        pass
    try:
        html = await asyncio.wait_for(page.content(), timeout=EVAL_TIMEOUT_S)
        found.extend(EMAIL_RE.findall(html))
    except Exception:
        pass

    seen, clean = set(), []
    for addr in found:
        addr = addr.strip().strip(".,;:'\"").lower()
        if addr in seen or JUNK_EMAIL_RE.search(addr) or not plausible_email(addr):
            continue
        seen.add(addr)
        clean.append(addr)

    root = ".".join(site_host.replace("www.", "").split(".")[-2:])
    clean.sort(key=lambda a: (root not in a.split("@")[-1], len(a)))
    return clean[:5]


# What "ready" looks like. A contact form almost always has a message box or an
# email field; a header search box has neither, so this does not fire on every
# page that merely contains an <input>. The CAPTCHA selectors count as ready
# too: the form is there, we simply will not be able to submit it.
FORM_READY_SEL = (
    "textarea, input[type=email], "
    "form input[name*='mail' i], form input[id*='mail' i], "
    "iframe[src*='recaptcha'], [data-sitekey]"
)
# once a field exists, give its siblings a moment to mount alongside it
FORM_READY_GRACE_MS = 350
IDLE_CEILING_S = 6.0


async def settle(page, extra_ms: int = 1800, adaptive: bool = True) -> None:
    """Wait until the page has something worth reading.

    Reading the DOM immediately after domcontentloaded finds an empty shell on
    any site that builds its form client-side, so some waiting is unavoidable.
    Waiting a *fixed* time is not: most pages have their form within a tenth of
    a second, and the rest of the pause is spent on nothing.

    A form field appearing and the network going idle are raced under the same
    ceiling the fixed wait used, so this can never be slower than waiting the
    full time - a page that never shows a field still waits it out.
    """
    if not adaptive:
        try:
            await asyncio.wait_for(page.wait_for_load_state("networkidle"),
                                   timeout=IDLE_CEILING_S)
        except Exception:
            pass
        try:
            await page.wait_for_timeout(extra_ms)
        except Exception:
            pass
        return

    form = asyncio.ensure_future(
        page.wait_for_selector(FORM_READY_SEL, state="attached",
                               timeout=IDLE_CEILING_S * 1000))
    idle = asyncio.ensure_future(page.wait_for_load_state("networkidle"))
    try:
        done, pending = await asyncio.wait(
            {form, idle}, timeout=IDLE_CEILING_S,
            return_when=asyncio.FIRST_COMPLETED)
    except Exception:
        done, pending = set(), {form, idle}

    # Whichever lost the race is cancelled, then awaited: a cancelled Playwright
    # call still has to be collected or it resurfaces later as an unretrieved
    # exception on an unrelated await. CancelledError is a BaseException, so it
    # is caught by name - "except Exception" lets it straight through.
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except (Exception, asyncio.CancelledError):
            pass

    ready = False
    for task in done:                      # never leave an exception unread
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            continue
        if task is form and exc is None:
            ready = True

    try:
        await page.wait_for_timeout(FORM_READY_GRACE_MS if ready else extra_ms)
    except Exception:
        pass


async def harvest_emails(page, host: str, candidates: list[str], timeout: int,
                         limit: int = 4) -> list[str]:
    """Look for an address on this page, then on the contact pages.

    A homepage often lists none while /contact-us/ lists several, so stopping
    at the current page is what leaves a site with no route at all.
    """
    found: list[str] = list(await collect_emails(page, host))
    if found:
        return found

    start_url = page.url
    for cand in candidates[:limit]:
        if cand == start_url:
            continue
        try:
            resp = await page.goto(cand, wait_until="domcontentloaded", timeout=timeout)
            if resp and resp.status >= 400:
                continue
            await settle(page, 1200)
        except Exception:
            continue
        found = list(await collect_emails(page, host))
        if found:
            return found
    return found


async def find_contact_links(page, base_url: str, limit: int) -> list[str]:
    """Links on the current page whose text or href smells like a contact page."""
    try:
        anchors = await asyncio.wait_for(
            page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => ({href: e.getAttribute('href'), text: (e.innerText||'').trim()}))"),
            timeout=EVAL_TIMEOUT_S)
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


# Titles are usually "Name | tagline" or "Name - Home". Cut at the separator
# and drop the boilerplate half.
TITLE_SPLIT_RE = re.compile(r"\s+[|–—\-:·]\s+")
TITLE_JUNK_RE = re.compile(
    r"^(home|welcome|index|untitled|home ?page|official (site|website))$", re.I)


async def site_name(page) -> str:
    """The company's own name for itself, properly spaced. "" if unusable.

    A name derived from the domain runs the words together
    ("goldencityfinance" -> "Goldencityfinance"), which reads badly in a
    greeting. The site itself nearly always states the real name.
    """
    try:
        raw = await asyncio.wait_for(page.evaluate(
            """() => document.querySelector('meta[property="og:site_name"]')?.content
                 || document.querySelector('meta[name="application-name"]')?.content
                 || document.title || ''"""), timeout=EVAL_TIMEOUT_S)
    except Exception:
        return ""

    name = " ".join(str(raw or "").split())
    if not name:
        return ""
    # keep the longest leading chunk that still looks like a name
    parts = [p.strip() for p in TITLE_SPLIT_RE.split(name) if p.strip()]
    if parts:
        name = parts[0]
    name = name.strip(" .,|-–—")
    if not name or len(name) > 60 or TITLE_JUNK_RE.match(name):
        return ""
    if not re.search(r"[A-Za-z]{2}", name):
        return ""
    return name


async def page_summary(page, max_chars: int = 500) -> str:
    """Title + meta description + first heading - context for the local LLM hook."""
    try:
        data = await asyncio.wait_for(page.evaluate(
            """() => {
                const meta = (name) => document.querySelector(`meta[name="${name}"]`)?.content
                    || document.querySelector(`meta[property="og:${name}"]`)?.content || '';
                const bits = [document.title || '', meta('description'),
                    document.querySelector('h1')?.innerText || ''];
                return bits.filter(Boolean).join(' | ');
            }"""
        ), timeout=EVAL_TIMEOUT_S)
    except Exception:
        return ""
    return " ".join((data or "").split())[:max_chars]


def guessed_paths(base_url: str) -> list[str]:
    parts = urlparse(base_url)
    root = f"{parts.scheme}://{parts.netloc}"
    return [root + p for p in COMMON_CONTACT_PATHS]
