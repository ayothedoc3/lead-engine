"""
60-Second Lead Engine
======================
A FastAPI webhook that, for every new inbound lead:
  1. Drafts a personalized, on-brand reply with Claude (with the client's booking link)
  2. Sends that reply to the lead via Resend
  3. Pings the operator with the lead details + the reply that went out

Point a client's contact form / CRM / Zapier webhook at  POST /lead  with the
header  X-Lead-Secret: <LEAD_ENGINE_SECRET>.

Run locally:   uvicorn main:app --reload --port 8080
"""
from __future__ import annotations

import logging
from typing import Optional

import anthropic
import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from config import build_system_prompt, load_settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lead-engine")

settings = load_settings()
SYSTEM_PROMPT = build_system_prompt(settings)

# Sync client is fine: FastAPI runs sync path-operation functions in a worker
# thread, so the event loop is never blocked. If no key is set, we skip Claude
# and use the templated fallback reply (engine still works, just not personalized).
claude = anthropic.Anthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None

app = FastAPI(title="60-Second Lead Engine")


# --- Schemas -----------------------------------------------------------------
class LeadIn(BaseModel):
    """Flexible inbound payload — most form tools send at least name + email."""
    name: Optional[str] = None
    email: str
    message: Optional[str] = None
    company: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = None

    model_config = {"extra": "allow"}  # tolerate extra fields from any form tool


class LeadReply(BaseModel):
    """Structured output we ask Claude to produce."""
    subject: str
    body_html: str


# --- Helpers -----------------------------------------------------------------
def _fallback_reply(first_name: str) -> LeadReply:
    """A safe, generic reply so a lead always gets a fast response."""
    return LeadReply(
        subject=f"Thanks for reaching out to {settings.client_name}",
        body_html=(
            f"<p>Hi {first_name},</p>"
            f"<p>Thanks for getting in touch — we've received your message and "
            f"someone from {settings.client_name} will follow up shortly.</p>"
            f'<p>If it\'s easier, grab a time that works for you here: '
            f'<a href="{settings.booking_link}">{settings.booking_link}</a></p>'
            f"<p>Talk soon.</p>"
        ),
    )


def draft_reply(lead: LeadIn) -> LeadReply:
    """Ask Claude for an on-brand subject + HTML body. Falls back on any error."""
    first_name = (lead.name or "").strip().split(" ")[0] or "there"
    if claude is None:
        log.info("No ANTHROPIC_API_KEY set — using templated fallback reply")
        return _fallback_reply(first_name)

    user_block = (
        f"New lead:\n"
        f"- Name: {lead.name or 'unknown'}\n"
        f"- First name: {first_name}\n"
        f"- Company: {lead.company or 'unknown'}\n"
        f"- Their message: {lead.message or '(no message provided)'}\n"
        f"- Source: {lead.source or 'website'}\n\n"
        f"Write their first reply now."
    )
    try:
        resp = claude.messages.parse(
            model=settings.model,
            max_tokens=1024,
            # cache_control caches the per-client system prompt across leads.
            # (Only activates once the prompt exceeds the model's minimum cacheable
            # prefix — ~4096 tokens on Opus 4.7; harmless if it's shorter.)
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_block}],
            output_format=LeadReply,
        )
        if resp.parsed_output is not None:
            return resp.parsed_output
        log.warning("Claude returned no parsed_output (stop_reason=%s)", resp.stop_reason)
    except Exception:  # noqa: BLE001 — never let drafting failure drop the lead
        log.exception("Claude drafting failed; using fallback reply")

    return _fallback_reply(first_name)


def send_email(client: httpx.Client, to: str, subject: str, html: str) -> bool:
    """Send one email via Resend. Returns True on success."""
    r = client.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        json={"from": settings.from_email, "to": [to], "subject": subject, "html": html},
        timeout=20.0,
    )
    if r.status_code >= 300:
        log.error("Resend send failed (%s): %s", r.status_code, r.text[:300])
        return False
    return True


def operator_alert_html(lead: LeadIn, reply: LeadReply) -> str:
    return (
        f"<p><strong>New lead — auto-replied in seconds.</strong></p>"
        f"<p><strong>Name:</strong> {lead.name or '—'}<br/>"
        f"<strong>Email:</strong> {lead.email}<br/>"
        f"<strong>Company:</strong> {lead.company or '—'}<br/>"
        f"<strong>Phone:</strong> {lead.phone or '—'}<br/>"
        f"<strong>Source:</strong> {lead.source or 'website'}</p>"
        f"<p><strong>Their message:</strong><br/>{lead.message or '(none)'}</p>"
        f"<hr/><p><strong>Reply we sent</strong> (subject: {reply.subject}):</p>"
        f"{reply.body_html}"
    )


# --- Routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"ok": True, "client": settings.client_name, "model": settings.model}


@app.post("/lead")
def handle_lead(lead: LeadIn, x_lead_secret: str = Header(default="")) -> dict:
    if x_lead_secret != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Lead-Secret")

    reply = draft_reply(lead)

    with httpx.Client() as http:
        replied = send_email(http, to=str(lead.email), subject=reply.subject, html=reply.body_html)
        alerted = send_email(
            http,
            to=settings.alert_email,
            subject=f"New lead: {lead.name or lead.email}",
            html=operator_alert_html(lead, reply),
        )

    log.info("Lead %s — replied=%s alerted=%s", lead.email, replied, alerted)
    return {"status": "ok", "replied": replied, "operator_alerted": alerted}
