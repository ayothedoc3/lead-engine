"""Log every lead to the client's CRM: HubSpot natively, anything else by webhook."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape

import httpx

from config import Settings
from db import Lead
from drafting import Draft

log = logging.getLogger("lead-engine.crm")

HUBSPOT = "https://api.hubapi.com"
NOTE_TO_CONTACT = 202  # HubSpot-defined association type


def _split_name(name: str | None) -> tuple[str, str]:
    parts = (name or "").strip().split(" ", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def log_to_hubspot(http: httpx.Client, token: str, lead: Lead, reply: Draft) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    first, last = _split_name(lead.name)
    properties = {k: v for k, v in {
        "firstname": first,
        "lastname": last,
        "company": lead.company,
        "phone": lead.phone,
    }.items() if v}
    r = http.post(
        f"{HUBSPOT}/crm/v3/objects/contacts/batch/upsert",
        headers=headers,
        json={"inputs": [{"id": lead.email, "idProperty": "email", "properties": properties}]},
    )
    r.raise_for_status()
    contact_id = r.json()["results"][0]["id"]

    note = (
        f"<p><strong>Inbound lead via {escape(lead.source or 'website')}</strong></p>"
        f"<p>{escape(lead.message or '(no message)')}</p>"
        f"<p><strong>Auto-reply sent:</strong> {escape(reply.subject)}</p>{reply.body_html}"
    )
    r = http.post(
        f"{HUBSPOT}/crm/v3/objects/notes",
        headers=headers,
        json={
            "properties": {
                "hs_timestamp": datetime.fromtimestamp(lead.received_at, timezone.utc).isoformat(),
                "hs_note_body": note,
            },
            "associations": [{
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_CONTACT}],
            }],
        },
    )
    r.raise_for_status()


def log_to_webhook(http: httpx.Client, url: str, lead: Lead, reply: Draft) -> None:
    r = http.post(url, json={
        "event": "lead.received",
        "lead": {
            "id": lead.id,
            "email": lead.email,
            "name": lead.name,
            "company": lead.company,
            "phone": lead.phone,
            "message": lead.message,
            "source": lead.source,
            "sms_consent": lead.sms_consent,
            "received_at": datetime.fromtimestamp(lead.received_at, timezone.utc).isoformat(),
        },
        "reply": {"subject": reply.subject, "body_html": reply.body_html, "sms_text": reply.sms_text},
    })
    r.raise_for_status()


def log_lead(settings: Settings, lead: Lead, reply: Draft) -> None:
    """Runs after the reply has gone out; a CRM failure is logged, never fatal."""
    with httpx.Client(timeout=20.0) as http:
        if settings.hubspot_token:
            try:
                log_to_hubspot(http, settings.hubspot_token, lead, reply)
            except Exception:  # noqa: BLE001
                log.exception("HubSpot logging failed for lead %s", lead.id)
        if settings.crm_webhook_url:
            try:
                log_to_webhook(http, settings.crm_webhook_url, lead, reply)
            except Exception:  # noqa: BLE001
                log.exception("CRM webhook failed for lead %s", lead.id)
