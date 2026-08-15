"""Optional personalization via a locally-running Ollama model.

No API key, no cloud call: talks to a local Ollama server (default
http://localhost:11434) over plain HTTP using only the standard library.
Any failure (Ollama not installed, not running, model not pulled, timeout)
is swallowed and treated as "no hook" - the static Jinja2 templates are the
fallback, so a run never breaks because the model is unavailable.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class OllamaError(Exception):
    pass


class Ollama:
    def __init__(self, host: str, model: str, timeout: float = 30.0):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout

    def generate(self, prompt: str, system: str = "", temperature: float = 0.4) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {"temperature": temperature},
        }
        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise OllamaError(str(exc)) from exc
        if body.get("error"):
            raise OllamaError(str(body["error"]))
        return (body.get("response") or "").strip()


HOOK_SYSTEM_PROMPT = (
    "You write exactly one short sentence (max 220 characters) for a cold "
    "outreach message. It connects the sender's offering to something "
    "specific about the target company. No greeting, no signature, no "
    "markdown, no quotes - just the sentence. Only use facts given below; "
    "never invent details about the target company."
)


def build_hook(cfg, ctx: dict, site_summary: str = "") -> str:
    """Return a personalized one-line hook, or "" if disabled/unavailable."""
    if not cfg.path("llm", "enabled", default=False):
        return ""

    sender = ctx.get("sender", {}) or {}
    lines = [
        f"Sender company: {sender.get('company', '')}",
        f"What the sender does: {sender.get('pitch', '')}",
        f"Target company: {ctx.get('company_name', '')}",
        f"Target website: {ctx.get('website', '')}",
    ]
    if ctx.get("notes"):
        lines.append(f"Notes: {ctx['notes']}")
    if site_summary:
        lines.append(f"What their homepage says: {site_summary}")

    client = Ollama(
        host=str(cfg.path("llm", "host", default="http://localhost:11434")),
        model=str(cfg.path("llm", "model", default="llama3.1")),
        timeout=float(cfg.path("llm", "timeout_s", default=30)),
    )
    try:
        text = client.generate(
            prompt="\n".join(lines),
            system=HOOK_SYSTEM_PROMPT,
            temperature=float(cfg.path("llm", "temperature", default=0.4)),
        )
    except OllamaError:
        return ""

    text = " ".join(text.split())
    max_chars = int(cfg.path("llm", "max_hook_chars", default=220))
    return text[:max_chars].rstrip()
