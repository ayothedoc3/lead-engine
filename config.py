"""Per-client configuration, loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: Optional[str]  # optional: without it, a templated fallback reply is used
    resend_api_key: str
    webhook_secret: str
    model: str
    client_name: str
    client_voice: str
    booking_link: str
    from_email: str
    alert_email: str


def load_settings() -> Settings:
    def required(key: str) -> str:
        val = os.environ.get(key, "").strip()
        if not val:
            raise RuntimeError(f"Missing required env var: {key}")
        return val

    return Settings(
        anthropic_api_key=(os.environ.get("ANTHROPIC_API_KEY", "").strip() or None),
        resend_api_key=required("RESEND_API_KEY"),
        webhook_secret=required("LEAD_ENGINE_SECRET"),
        model=os.environ.get("LEAD_ENGINE_MODEL", "claude-opus-4-7").strip(),
        client_name=required("CLIENT_NAME"),
        client_voice=required("CLIENT_VOICE"),
        booking_link=required("BOOKING_LINK"),
        from_email=required("FROM_EMAIL"),
        alert_email=required("ALERT_EMAIL"),
    )


def build_system_prompt(s: Settings) -> str:
    """Stable per-client system prompt — same for every lead, so it caches well."""
    return f"""You write the first reply to a new inbound lead for {s.client_name}.

Your job: turn a cold inbound enquiry into a booked call, fast. Write a short,
genuinely helpful first response that a busy founder would be happy to send.

Brand voice and rules:
{s.client_voice}

Hard requirements for every reply:
- Address the person by their first name if available.
- Acknowledge their specific enquiry in one sentence (reference what they wrote).
- Be concise: 3-5 short sentences. No filler, no "I hope this email finds you well".
- Give one small piece of genuine value or reassurance relevant to their message.
- Invite them to book a quick call and include this exact link: {s.booking_link}
- Never invent facts, prices, availability, or promises. If you don't know, keep it general.
- Plain, human tone. No emoji unless the brand voice explicitly uses them.

Return:
- subject: a short, specific email subject line (no "Re:" prefix, no spammy words).
- body_html: the email body as simple HTML using only <p> and <a> tags. The booking
  link must be an <a href="..."> anchor. Do not include <html>, <head>, or <body> tags.
"""
