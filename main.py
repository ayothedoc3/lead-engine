"""
60-Second Lead Engine
======================
For every new inbound lead:
  1. Drafts a personalized, on-brand reply with Claude (email + SMS text)
  2. Sends it to the lead within seconds: email via Resend, SMS via Twilio when
     the lead opted in on the form
  3. Alerts the operator with the lead, the reply and a one-click stop link
  4. Logs the lead to the client's CRM
  5. Follows up on the configured days until the lead books, texts back or is stopped

Routes
  POST /lead           new lead (header X-Lead-Secret)
  POST /booked?key=    booking webhook from Calendly, Cal.com or Zapier
  POST /sms/inbound    Twilio incoming-message webhook (signature checked)
  GET/POST /stop/{id}/{token}   operator stops a lead's follow-ups
  GET  /stats?key=&days=        response-time report
  GET  /health

Run locally:   uvicorn main:app --reload --port 8080
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import statistics
import time
from contextlib import asynccontextmanager
from html import escape
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from twilio.request_validator import RequestValidator

import crm
from channels import Channels, normalize_us_phone
from config import Settings, load_settings
from db import BOOKED, REPLIED, STOPPED, Lead, Store
from drafting import Draft, Drafter, sms_body
from followups import due_time, process_due

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lead-engine")

FOLLOWUP_POLL_SECONDS = 60
STOP_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "REVOKE", "OPTOUT"}
CONSENT_TRUE = {"true", "yes", "y", "on", "1", "checked"}


class LeadIn(BaseModel):
    """Flexible inbound payload: most form tools send at least name + email."""
    name: Optional[str] = None
    email: str
    message: Optional[str] = None
    company: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = None
    sms_consent: Any = None  # checkbox values arrive as bools or strings

    model_config = {"extra": "allow"}  # tolerate extra fields from any form tool


def consent_given(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in CONSENT_TRUE


def stop_token(secret: str, lead_id: int) -> str:
    return hmac.new(secret.encode(), f"stop:{lead_id}".encode(), hashlib.sha256).hexdigest()[:32]


def booked_emails(payload: dict) -> list[str]:
    """Invitee emails from a Calendly (invitee.created), Cal.com (BOOKING_CREATED) or flat
    Zapier-style payload."""
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    emails = [
        payload.get("email"),                                   # Zapier / custom
        inner.get("email"),                                     # Calendly invitee
        *[a.get("email") for a in inner.get("attendees", []) if isinstance(a, dict)],  # Cal.com
    ]
    return [e.strip() for e in emails if isinstance(e, str) and e.strip()]


def operator_alert_html(s: Settings, lead: Lead, reply: Draft, texted: bool) -> str:
    e = lambda v: escape(v) if v else "-"  # noqa: E731
    stop = ""
    if s.followups_enabled:
        url = f"{s.public_base_url}/stop/{lead.id}/{stop_token(s.webhook_secret, lead.id)}"
        stop = f'<p><a href="{url}">Stop follow-ups for this lead</a> (they stop automatically if they book or text back).</p>'
    sms = f"<p><strong>Text sent:</strong> {escape(reply.sms_text)}</p>" if texted else ""
    return (
        f"<p><strong>New lead, auto-replied in seconds.</strong></p>"
        f"<p><strong>Name:</strong> {e(lead.name)}<br/>"
        f"<strong>Email:</strong> {e(lead.email)}<br/>"
        f"<strong>Company:</strong> {e(lead.company)}<br/>"
        f"<strong>Phone:</strong> {e(lead.phone)}<br/>"
        f"<strong>Source:</strong> {e(lead.source or 'website')}</p>"
        f"<p><strong>Their message:</strong><br/>{e(lead.message)}</p>"
        f"<hr/><p><strong>Email we sent</strong> (subject: {escape(reply.subject)}):</p>{reply.body_html}"
        f"{sms}{stop}"
    )


def create_app(settings: Settings) -> FastAPI:
    store = Store(settings.database_path)
    drafter = Drafter(settings)
    channels = Channels(settings)
    twilio_validator = RequestValidator(settings.twilio.auth_token) if settings.twilio else None

    async def followup_loop() -> None:
        while True:
            try:
                await run_in_threadpool(process_due, settings, store, drafter, channels, time.time())
            except Exception:  # noqa: BLE001
                log.exception("Follow-up loop iteration failed")
            await asyncio.sleep(FOLLOWUP_POLL_SECONDS)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(followup_loop()) if settings.followups_enabled else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="60-Second Lead Engine", lifespan=lifespan)
    app.state.store = store
    app.state.drafter = drafter
    app.state.channels = channels

    def check_key(key: str) -> None:
        if not hmac.compare_digest(key, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid key")

    def after_reply(lead: Lead, reply: Draft, texted: bool) -> None:
        channels.email(
            settings.alert_email,
            f"New lead: {lead.name or lead.email}",
            operator_alert_html(settings, lead, reply, texted),
        )
        crm.log_lead(settings, lead, reply)

    @app.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "client": settings.client_name,
            "model": settings.model,
            "sms": settings.sms_enabled,
            "followup_days": list(settings.followup_days),
            "hubspot": bool(settings.hubspot_token),
            "crm_webhook": bool(settings.crm_webhook_url),
        }

    @app.post("/lead")
    def handle_lead(payload: LeadIn, background: BackgroundTasks, x_lead_secret: str = Header(default="")) -> dict:
        check_key(x_lead_secret)
        lead = store.create_lead(
            email=payload.email.strip(),
            name=payload.name,
            company=payload.company,
            phone=normalize_us_phone(payload.phone),
            message=payload.message,
            source=payload.source,
            sms_consent=consent_given(payload.sms_consent),
            received_at=time.time(),
        )

        reply = drafter.first_reply(lead)
        emailed = channels.email(lead.email, reply.subject, reply.body_html, reply_to=settings.reply_to_email)
        store.log_message(lead.id, "reply", "email", reply.body_html, emailed.ok, emailed.error)

        texted = False
        if settings.sms_enabled and lead.can_text:
            text = sms_body(settings, reply.sms_text, include_opt_out=True)
            sms = channels.sms(lead.phone, text)
            store.log_message(lead.id, "reply", "sms", text, sms.ok, sms.error)
            texted = sms.ok
            if sms.unsubscribed:
                store.mark_sms_opted_out(lead.phone)

        if emailed.ok or texted:
            store.record_first_reply(lead.id, time.time(), reply.subject, reply.body_html)
        if settings.followups_enabled:
            store.schedule(lead.id, due_time(settings, lead.received_at, 1), followups_sent=0)

        background.add_task(after_reply, lead, reply, texted)
        log.info("Lead %s: emailed=%s texted=%s", lead.id, emailed.ok, texted)
        return {"status": "ok", "lead_id": lead.id, "replied": emailed.ok, "texted": texted}

    @app.post("/booked")
    async def booked(request: Request, key: str = "") -> dict:
        check_key(key)
        emails = booked_emails(await request.json())
        stopped = sum(store.end_sequences_for_email(e, BOOKED) for e in emails)
        log.info("Booking webhook: emails=%s stopped=%s", emails, stopped)
        return {"status": "ok", "stopped": stopped}

    @app.post("/sms/inbound")
    async def sms_inbound(request: Request, x_twilio_signature: str = Header(default="")) -> Response:
        if twilio_validator is None:
            raise HTTPException(status_code=404, detail="SMS is not enabled")
        form = dict(await request.form())
        url = f"{settings.public_base_url}/sms/inbound"
        if not twilio_validator.validate(url, form, x_twilio_signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

        phone = normalize_us_phone(str(form.get("From", "")))
        body = str(form.get("Body", "")).strip()
        lead = store.latest_by_phone(phone) if phone else None
        if lead:
            store.log_message(lead.id, "inbound_sms", "sms", body, True)
            store.end_sequence(lead.id, REPLIED)
            if body.upper() in STOP_KEYWORDS:
                store.mark_sms_opted_out(phone)
            channels.email(
                settings.alert_email,
                f"Text reply from {lead.name or lead.email}",
                f"<p><strong>{escape(lead.name or lead.email)}</strong> ({escape(phone)}) replied by text:</p>"
                f"<blockquote>{escape(body)}</blockquote>"
                f"<p>Follow-ups for this lead have stopped. Reply to them directly from your phone or inbox.</p>",
            )
        else:
            log.info("Inbound SMS from unknown number %s", phone)
        return Response(content="<Response></Response>", media_type="application/xml")

    def stop_page(message: str) -> HTMLResponse:
        return HTMLResponse(
            f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>{message}</body>"
        )

    def lead_for_token(lead_id: int, token: str) -> Lead:
        if not hmac.compare_digest(token, stop_token(settings.webhook_secret, lead_id)):
            raise HTTPException(status_code=404, detail="Not found")
        lead = store.get(lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Not found")
        return lead

    # GET only shows a confirm button: email link scanners prefetch GET links.
    @app.get("/stop/{lead_id}/{token}")
    def stop_confirm(lead_id: int, token: str) -> HTMLResponse:
        lead = lead_for_token(lead_id, token)
        who = escape(lead.name or lead.email)
        if lead.status != "active":
            return stop_page(f"<p>Follow-ups for {who} are already stopped ({escape(lead.status)}).</p>")
        return stop_page(
            f"<p>Stop all follow-ups to <strong>{who}</strong>?</p>"
            f"<form method=post><button style='padding:.6rem 1.2rem'>Stop follow-ups</button></form>"
        )

    @app.post("/stop/{lead_id}/{token}")
    def stop(lead_id: int, token: str) -> HTMLResponse:
        lead = lead_for_token(lead_id, token)
        store.end_sequence(lead.id, STOPPED)
        return stop_page(f"<p>Done. No more follow-ups will go to {escape(lead.name or lead.email)}.</p>")

    @app.get("/stats")
    def stats(key: str = "", days: int = 30) -> dict:
        check_key(key)
        leads = store.leads_since(time.time() - days * 86400)
        times = [l.first_reply_at - l.received_at for l in leads if l.first_reply_at]
        return {
            "days": days,
            "leads": len(leads),
            "replied": len(times),
            "not_replied": len(leads) - len(times),
            "median_seconds": round(statistics.median(times), 1) if times else None,
            "max_seconds": round(max(times), 1) if times else None,
            "over_60_seconds": sum(1 for t in times if t > 60),
            "booked": sum(1 for l in leads if l.status == BOOKED),
            "texted_back": sum(1 for l in leads if l.status == REPLIED),
        }

    return app


app = create_app(load_settings())
