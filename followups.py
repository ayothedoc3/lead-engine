"""Follow-up sequence: scheduling inside business hours and sending due steps."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from channels import Channels
from config import Settings
from db import Lead, Store
from drafting import Drafter, sms_body

log = logging.getLogger("lead-engine.followups")

DAY = 86400.0
WINDOW_START_HOUR = 9   # follow-ups only go out 9:00 to 18:00, Monday to Friday,
WINDOW_END_HOUR = 18    # in the client's time zone


def within_business_hours(ts: float, tz: ZoneInfo) -> float:
    """Move a timestamp forward to the next weekday 9:00 to 18:00 slot in `tz`."""
    dt = datetime.fromtimestamp(ts, tz)
    if dt.hour >= WINDOW_END_HOUR:
        dt = (dt + timedelta(days=1)).replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    elif dt.hour < WINDOW_START_HOUR:
        dt = dt.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    while dt.weekday() >= 5:  # Saturday, Sunday
        dt = (dt + timedelta(days=1)).replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    return dt.timestamp()


def due_time(settings: Settings, received_at: float, step: int) -> Optional[float]:
    """When follow-up `step` (1-based) is due, or None when the sequence is over."""
    if step > len(settings.followup_days):
        return None
    return within_business_hours(received_at + settings.followup_days[step - 1] * DAY, settings.client_timezone)


def _earlier_texts(store: Store, lead_id: int) -> list[str]:
    return [m["body"] for m in store.messages_for(lead_id) if m["channel"] == "email" and m["ok"]]


def send_followup(settings: Settings, store: Store, drafter: Drafter, channels: Channels, lead: Lead) -> None:
    step = lead.followups_sent + 1
    total = len(settings.followup_days)
    draft = drafter.followup(lead, step, total, _earlier_texts(store, lead.id))
    subject = f"Re: {lead.first_subject}" if lead.first_subject else draft.subject

    result = channels.email(lead.email, subject, draft.body_html, reply_to=settings.reply_to_email)
    store.log_message(lead.id, "followup", "email", draft.body_html, result.ok, result.error, step=step)

    if settings.sms_enabled and lead.can_text:
        text = sms_body(settings, draft.sms_text, include_opt_out=False)
        sms = channels.sms(lead.phone, text)
        store.log_message(lead.id, "followup", "sms", text, sms.ok, sms.error, step=step)
        if sms.unsubscribed:
            store.mark_sms_opted_out(lead.phone)

    store.schedule(lead.id, due_time(settings, lead.received_at, step + 1), followups_sent=step)


def process_due(settings: Settings, store: Store, drafter: Drafter, channels: Channels, now: float) -> int:
    """Send every follow-up that is due. Returns how many leads were processed."""
    due = store.claim_due(now)
    for lead in due:
        try:
            send_followup(settings, store, drafter, channels, lead)
        except Exception:  # noqa: BLE001  one bad lead must not block the rest
            log.exception("Follow-up failed for lead %s", lead.id)
    return len(due)
