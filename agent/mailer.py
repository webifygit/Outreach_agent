"""SMTP fallback: send the templated email when no usable form exists.

Sends as whichever sender identity was picked for the row (agent.senders),
authenticating as that person's own inbox so the From header always matches
the authenticated account - required by Gmail and most providers, and the
only way multiple rotated identities stay deliverable.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .config import Config


class Mailer:
    def __init__(self, cfg: Config, live: bool):
        self.cfg = cfg
        self.live = live
        self.host = cfg.path("email", "smtp_host", default="")
        self.port = int(cfg.path("email", "smtp_port", default=587))
        self._smtp: dict[str, smtplib.SMTP] = {}  # keyed by sender email, reused across sends

    def preflight(self) -> str:
        if not self.cfg.path("email", "enabled", default=True):
            return "email disabled in config"
        if not self.host:
            return "smtp_host not configured"
        people = self.cfg.get("email_senders") or ([self.cfg.get("sender")] if self.cfg.get("sender") else [])
        if not people:
            return "no email_senders configured"
        if self.live:
            missing = [
                p.get("email", "?") for p in people
                if not os.environ.get(p.get("smtp_password_env", "SMTP_PASSWORD"))
            ]
            if missing:
                return f"no SMTP password set for: {', '.join(missing)}"
        return ""

    def _connection(self, sender: dict) -> smtplib.SMTP:
        email = sender.get("email", "")
        smtp = self._smtp.get(email)
        if smtp is not None:
            return smtp
        smtp = smtplib.SMTP(self.host, self.port, timeout=30)
        smtp.ehlo()
        if self.port in (587, 25):
            smtp.starttls()
            smtp.ehlo()
        password = os.environ.get(sender.get("smtp_password_env", "SMTP_PASSWORD"), "")
        smtp.login(email, password)
        self._smtp[email] = smtp
        return smtp

    def build(self, to_addr: str, subject: str, body: str, html: str, sender: dict) -> EmailMessage:
        from_email = sender.get("email", "")
        from_name = sender.get("name") or f"{sender.get('first_name', '')} {sender.get('last_name', '')}".strip()
        msg = EmailMessage()
        msg["From"] = formataddr((from_name, from_email))
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid()
        msg["Reply-To"] = from_email
        # Gives recipients a one-click opt-out and keeps mailbox providers happier.
        msg["List-Unsubscribe"] = f"<mailto:{from_email}?subject=unsubscribe>"
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        return msg

    def send(self, to_addr: str, subject: str, body: str, html: str, sender: dict) -> tuple[str, str]:
        msg = self.build(to_addr, subject, body, html, sender)
        from_email = sender.get("email", "?")
        if not self.live:
            return "dry_run", f"would email {to_addr} as {from_email} | subject: {subject}"
        try:
            smtp = self._connection(sender)
            smtp.send_message(msg)
            return "sent", f"emailed {to_addr} from {from_email}"
        except Exception as exc:  # noqa: BLE001
            self._smtp.pop(from_email, None)
            return "failed", f"smtp error: {type(exc).__name__}: {exc}"

    def close(self) -> None:
        for smtp in self._smtp.values():
            try:
                smtp.quit()
            except Exception:
                pass
        self._smtp.clear()
