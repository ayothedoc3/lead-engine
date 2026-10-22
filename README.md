# 60-Second Lead Engine

A small, custom-built service that turns a new inbound lead into a booked call.
For every lead, it:

1. **Drafts a personalized, on-brand reply** with Claude (email and text, in the client's voice, with their booking link)
2. **Sends it within seconds**: email via Resend, plus a text via Twilio when the lead ticked the SMS consent box
3. **Alerts the operator** with the lead, the reply that went out and a one-click link to stop follow-ups
4. **Logs the lead to the CRM**: HubSpot built in, anything else through a webhook (Zapier, Make, GoHighLevel, Pipedrive)
5. **Follows up** on the configured days, inside business hours, until the lead books, texts back or is stopped

One codebase, one `.env` per client: deploy a separate instance for each.

> **How to talk about it with clients:** "a custom lead-response system, built for your business." Never mention Claude/Resend/Twilio/Python/FastAPI. They're buying the outcome: every lead answered in under 60 seconds.

## Plans map to config

| Plan | Config |
|---|---|
| Lead Engine | Core settings only. Leave `FOLLOWUP_DAYS`, `TWILIO_*`, `HUBSPOT_TOKEN`, `CRM_WEBHOOK_URL` blank. |
| Lead Engine Pro | Add `FOLLOWUP_DAYS=1,3,5`, the three `TWILIO_*` values and `HUBSPOT_TOKEN` or `CRM_WEBHOOK_URL`. |

A partially configured feature (for example two of the three Twilio values) stops the app at startup with a clear error.

---

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in for this client
uvicorn main:app --port 8080
```

Send a test lead:

```bash
curl -X POST http://localhost:8080/lead \
  -H "Content-Type: application/json" \
  -H "X-Lead-Secret: <LEAD_ENGINE_SECRET>" \
  -d '{"name":"Jane Doe","email":"jane@example.com","phone":"415-555-0100","sms_consent":true,"message":"Do you do SEO retainers?","source":"website form"}'
```

Run the tests (no network calls, all providers are faked):

```bash
pip install pytest && python -m pytest -q
```

## Endpoints

| Route | Purpose |
|---|---|
| `POST /lead` | New lead. Header `X-Lead-Secret`. Fields: `name`, `email` (required), `message`, `company`, `phone`, `source`, `sms_consent`. Extra fields are ignored. |
| `POST /booked?key=<secret>` | Booking webhook. Accepts Calendly `invitee.created`, Cal.com `BOOKING_CREATED`, or `{"email": "..."}` from Zapier. Stops that lead's follow-ups. |
| `POST /sms/inbound` | Twilio incoming-message webhook. Signature-checked. Stops follow-ups, records STOP opt-outs and forwards the text to `ALERT_EMAIL`. |
| `GET /stop/<id>/<token>` | Link in the operator alert. Shows a confirm button (POST) so email link scanners can't trigger it. |
| `GET /stats?key=<secret>&days=30` | Response-time report: leads, median and max seconds to first reply, how many went over 60 seconds, booked, texted back. Use it for the monthly report and guarantee claims. |
| `GET /health` | Configured client, model and which features are on. |

---

## Per-client setup (the repeatable playbook)

1. **Fill in `.env`**: `CLIENT_NAME`, `CLIENT_VOICE` (paste their real voice, ideally built from emails they have sent), `BOOKING_LINK`, `CLIENT_TIMEZONE`, `FROM_EMAIL`, `REPLY_TO_EMAIL`, `ALERT_EMAIL`.
2. **Verify their sending domain in Resend** so `FROM_EMAIL` works (add the DKIM records to their DNS).
3. **Deploy** the instance (Coolify works well). Mount a persistent volume and point `DATABASE_PATH` at it, e.g. `/data/lead-engine.db`. Set `PUBLIC_BASE_URL` to its HTTPS URL.
4. **Point their form at `https://<host>/lead`** with header `X-Lead-Secret`. Map fields to `name`, `email`, `message`, `phone`, `sms_consent`.
5. **Pro only:**
   - **Booking webhook:** in Calendly (Webhooks) or Cal.com (Settings > Developer > Webhooks) add `https://<host>/booked?key=<secret>` for new bookings.
   - **SMS:** create a Twilio Messaging Service, register the client's brand and campaign for A2P 10DLC (needs their legal name and EIN; approval usually takes 1 to 3 weeks), add a US number to the service, and set its incoming-message webhook to `https://<host>/sms/inbound`. Email replies run while registration is pending; add the `TWILIO_*` values once approved.
   - **SMS consent on the form:** an unticked checkbox, separate from anything else, with this kind of wording: "Text me about my inquiry. Msg & data rates may apply. Msg frequency varies. Reply STOP to opt out, HELP for help." plus links to the client's privacy policy and SMS terms. Only leads with `sms_consent` true get texts.
   - **CRM:** a HubSpot private app token with `crm.objects.contacts.write` scope, or a Zapier/Make catch-hook URL in `CRM_WEBHOOK_URL`.
6. **Send a test lead**, confirm the email, text and alert land, then hand it over.

---

## Notes

- **The 60-second promise:** Claude calls time out after 15 seconds with one retry. If drafting fails for any reason a templated backup reply goes out, so no lead waits.
- **Follow-up timing:** steps are due `FOLLOWUP_DAYS` after the lead arrived, moved into weekday 9:00 to 18:00 in `CLIENT_TIMEZONE`. A new enquiry from the same email replaces any sequence still running.
- **Texts:** the first text names the business and says "Reply STOP to opt out." Numbers that opt out are never texted again.
- **Prompt caching:** system prompts are marked cacheable; they only cache once they exceed the model's minimum cacheable prefix, so short brand prompts are a no-op.
- **Cost:** a few hundred tokens per message, about a cent per lead even on Opus. Twilio US texts cost about a cent each plus carrier fees.
- **Security:** `/lead` needs the shared secret header; `/booked` and `/stats` need it as `?key=`; `/sms/inbound` checks Twilio's signature; stop links use a per-lead HMAC token. Lead details are HTML-escaped in operator emails.
