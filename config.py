"""Per-client configuration, loaded from environment variables.

Core settings are required. Optional feature groups (SMS, follow-ups, CRM) are
switched on by setting their variables; a partially configured group fails at
startup instead of misbehaving later.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class TwilioSettings:
    account_sid: str
    auth_token: str
    messaging_service_sid: str


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: Optional[str]  # without it, the templated backup reply is used
    resend_api_key: str
    webhook_secret: str
    model: str
    client_name: str
    client_voice: str
    booking_link: str
    from_email: str
    reply_to_email: str
    alert_email: str
    public_base_url: str
    database_path: str
    client_timezone: ZoneInfo
    followup_days: tuple[int, ...]  # empty = follow-ups off
    twilio: Optional[TwilioSettings]  # None = SMS off
    hubspot_token: Optional[str]
    crm_webhook_url: Optional[str]

    @property
    def sms_enabled(self) -> bool:
        return self.twilio is not None

    @property
    def followups_enabled(self) -> bool:
        return len(self.followup_days) > 0


def _required(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def _optional(key: str) -> Optional[str]:
    return os.environ.get(key, "").strip() or None


def _parse_followup_days(raw: str) -> tuple[int, ...]:
    if not raw.strip():
        return ()
    days = tuple(int(part) for part in raw.split(","))
    if any(d <= 0 for d in days) or list(days) != sorted(set(days)):
        raise RuntimeError("FOLLOWUP_DAYS must be increasing positive integers, e.g. 1,3,5")
    return days


def _load_twilio() -> Optional[TwilioSettings]:
    keys = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_MESSAGING_SERVICE_SID")
    present = [k for k in keys if _optional(k)]
    if not present:
        return None
    if len(present) != len(keys):
        missing = sorted(set(keys) - set(present))
        raise RuntimeError(f"SMS is partially configured. Missing: {', '.join(missing)}")
    return TwilioSettings(*(_required(k) for k in keys))


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=_optional("ANTHROPIC_API_KEY"),
        resend_api_key=_required("RESEND_API_KEY"),
        webhook_secret=_required("LEAD_ENGINE_SECRET"),
        model=os.environ.get("LEAD_ENGINE_MODEL", "claude-opus-4-7").strip(),
        client_name=_required("CLIENT_NAME"),
        client_voice=_required("CLIENT_VOICE"),
        booking_link=_required("BOOKING_LINK"),
        from_email=_required("FROM_EMAIL"),
        reply_to_email=_required("REPLY_TO_EMAIL"),
        alert_email=_required("ALERT_EMAIL"),
        public_base_url=_required("PUBLIC_BASE_URL").rstrip("/"),
        database_path=_required("DATABASE_PATH"),
        client_timezone=ZoneInfo(_required("CLIENT_TIMEZONE")),
        followup_days=_parse_followup_days(os.environ.get("FOLLOWUP_DAYS", "")),
        twilio=_load_twilio(),
        hubspot_token=_optional("HUBSPOT_TOKEN"),
        crm_webhook_url=_optional("CRM_WEBHOOK_URL"),
    )
