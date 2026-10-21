"""Outbound channels: email through Resend, SMS through Twilio."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

from config import Settings

log = logging.getLogger("lead-engine.channels")

# Twilio error for a number that has replied STOP to this sender.
TWILIO_UNSUBSCRIBED = 21610


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: Optional[str] = None
    unsubscribed: bool = False


def normalize_us_phone(raw: Optional[str]) -> Optional[str]:
    """Return an E.164 number for US/Canada input, or None if it isn't one."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


class Channels:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.http = httpx.Client(timeout=15.0)
        self.twilio = (
            TwilioClient(settings.twilio.account_sid, settings.twilio.auth_token) if settings.twilio else None
        )

    def email(self, to: str, subject: str, html: str, reply_to: Optional[str] = None) -> SendResult:
        payload = {"from": self.s.from_email, "to": [to], "subject": subject, "html": html}
        if reply_to:
            payload["reply_to"] = reply_to
        try:
            r = self.http.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self.s.resend_api_key}"},
                json=payload,
            )
        except httpx.HTTPError as e:
            log.error("Resend unreachable: %s", e)
            return SendResult(False, f"unreachable: {e}")
        if r.status_code >= 300:
            log.error("Resend send failed (%s): %s", r.status_code, r.text[:300])
            return SendResult(False, f"{r.status_code}: {r.text[:300]}")
        return SendResult(True)

    def sms(self, to: str, body: str) -> SendResult:
        if self.twilio is None or self.s.twilio is None:
            raise RuntimeError("SMS requested but Twilio is not configured")
        try:
            self.twilio.messages.create(
                to=to, messaging_service_sid=self.s.twilio.messaging_service_sid, body=body
            )
        except TwilioRestException as e:
            log.error("Twilio send failed (%s): %s", e.code, e.msg)
            return SendResult(False, f"{e.code}: {e.msg}", unsubscribed=e.code == TWILIO_UNSUBSCRIBED)
        return SendResult(True)
