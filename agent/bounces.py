"""Find the emails that were accepted but never actually delivered.

SMTP "sent" only means the provider took the message. An address that does not
exist bounces minutes later, and the bounce lands in the sending mailbox - the
agent never sees it, so the site stays recorded as contacted and the duplicate
check then blocks it forever. That is the wrong outcome for a business we never
actually reached: it should be free to try again by another route.

This reads the sending mailboxes over IMAP, strictly read-only, looking only at
delivery-status notifications and only for addresses we ourselves wrote to.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from email import policy

BOUNCE_SEARCHES = [
    '(FROM "mailer-daemon")',
    '(FROM "postmaster")',
    '(SUBJECT "Delivery Status Notification")',
    '(SUBJECT "Undelivered Mail Returned")',
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# "Final-Recipient: rfc822; someone@example.com"
RECIPIENT_RE = re.compile(r"(?:Final|Original)-Recipient:\s*[^;]*;\s*([^\s]+)", re.I)
STATUS_RE = re.compile(r"Status:\s*([245])\.\d+\.\d+", re.I)
ACTION_RE = re.compile(r"Action:\s*(failed|delayed|delivered)", re.I)


class BounceError(RuntimeError):
    pass


def _addresses_from(msg) -> tuple[set[str], bool]:
    """Recipients named in a bounce, and whether it is a hard failure.

    A delayed notice is not a bounce - the provider is still trying - so only
    "failed" counts. Where no machine-readable part exists, fall back to any
    address in the text, which the caller filters against what we actually sent.
    """
    found: set[str] = set()
    hard = False
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in ("message/delivery-status", "text/rfc822-headers"):
            try:
                text = part.get_payload(decode=True).decode("utf-8", "replace")
            except Exception:
                text = str(part)
            for m in RECIPIENT_RE.finditer(text):
                found.add(m.group(1).strip("<>").lower())
            action = ACTION_RE.search(text)
            status = STATUS_RE.search(text)
            if (action and action.group(1).lower() == "failed") or (status and status.group(1) == "5"):
                hard = True
    if not found:
        try:
            body = msg.get_body(preferencelist=("plain", "html"))
            text = body.get_content() if body else ""
        except Exception:
            text = ""
        found = {a.lower() for a in EMAIL_RE.findall(text or "")}
        hard = hard or bool(re.search(r"address not found|does not exist|user unknown|"
                                      r"no such user|mailbox unavailable|550", text or "", re.I))
    return found, hard


def find_bounced(mailbox: str, password: str, of_interest: set[str],
                 host: str = "imap.gmail.com", limit: int = 400) -> dict[str, str]:
    """Addresses among `of_interest` that bounced, mapped to a short reason.

    Read-only: the mailbox is opened readonly and nothing is flagged or moved.
    """
    if not password:
        raise BounceError(f"no password available for {mailbox}")

    bounced: dict[str, str] = {}
    try:
        conn = imaplib.IMAP4_SSL(host, 993, timeout=30)
        conn.login(mailbox, password)
        conn.select("INBOX", readonly=True)
    except Exception as exc:  # noqa: BLE001
        raise BounceError(f"{mailbox}: {type(exc).__name__}: {exc}") from exc

    try:
        ids: list[bytes] = []
        for query in BOUNCE_SEARCHES:
            try:
                typ, data = conn.search(None, query)
            except Exception:
                continue
            if typ == "OK" and data and data[0]:
                ids.extend(data[0].split())
        seen: set[bytes] = set()
        for msg_id in reversed(ids):          # newest first
            if msg_id in seen or len(seen) >= limit:
                continue
            seen.add(msg_id)
            try:
                typ, data = conn.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not data or not data[0]:
                    continue
                msg = email.message_from_bytes(data[0][1], policy=policy.default)
            except Exception:
                continue
            addrs, hard = _addresses_from(msg)
            if not hard:
                continue
            for addr in addrs & of_interest:
                bounced.setdefault(addr, (msg.get("Subject") or "delivery failure")[:90])
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return bounced


def check_all(cfg, of_interest: set[str]) -> tuple[dict[str, str], list[str]]:
    """Every sending mailbox. Returns (bounced, problems)."""
    bounced: dict[str, str] = {}
    problems: list[str] = []
    for sender in cfg.get("email_senders") or []:
        addr = sender.get("email", "")
        pw = os.environ.get(sender.get("smtp_password_env", ""), "")
        try:
            bounced.update(find_bounced(addr, pw, of_interest))
        except BounceError as exc:
            problems.append(str(exc))
    return bounced, problems
