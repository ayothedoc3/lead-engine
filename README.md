# 60-Second Lead Engine

A small, custom-built service that turns a new inbound lead into a booked call.
For every lead, it:

1. **Drafts a personalized, on-brand reply** with Claude (in the client's voice, with their booking link)
2. **Sends it to the lead** within seconds via Resend
3. **Pings the operator** with the lead details + the reply that went out

This is your reusable delivery asset. One codebase, one `.env` per client — deploy a separate instance for each.

> **How to talk about it with clients:** "a custom lead-response system, built for your business." Never mention Claude/Resend/Python/FastAPI. They're buying the outcome — every lead answered in under 60 seconds — not the tooling.

---

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in for this client
uvicorn main:app --port 8080
```

Test it:

```bash
curl -X POST http://localhost:8080/lead \
  -H "Content-Type: application/json" \
  -H "X-Lead-Secret: <your LEAD_ENGINE_SECRET>" \
  -d '{"name":"Jane Doe","email":"jane@example.com","message":"Do you do SEO retainers?","source":"website form"}'
```

`/health` returns the configured client + model.

---

## Per-client setup (the repeatable playbook)

1. **Copy `.env`** and fill in the client's `CLIENT_NAME`, `CLIENT_VOICE` (paste their real voice), `BOOKING_LINK`, `FROM_EMAIL`, `ALERT_EMAIL`.
2. **Verify their sending domain in Resend** so `FROM_EMAIL` works (add the DKIM record to their DNS).
3. **Deploy** the instance (Coolify, a small VPS, Fly, etc.) — it's a standard FastAPI app; `uvicorn main:app` behind HTTPS.
4. **Point their form at it.** Most form tools (Typeform, Tally, native site form, HubSpot, Zapier/Make) can POST a webhook. Set the URL to `https://<host>/lead` and add the header `X-Lead-Secret: <secret>`. Map their fields to `name`, `email`, `message` (extra fields are accepted and ignored).
5. **Send a test lead**, confirm the reply + operator alert land, hand it over.

Delivery time per client: ~1 hour once you've done it twice.

---

## Configuration

All config is environment variables — see [.env.example](.env.example). Key ones:

| Var | Purpose |
|---|---|
| `LEAD_ENGINE_SECRET` | Shared secret the form must send in `X-Lead-Secret`. Generate with `python -c "import secrets; print(secrets.token_hex(24))"`. |
| `LEAD_ENGINE_MODEL` | Defaults to `claude-opus-4-7` (best quality). Use `claude-haiku-4-5` for lowest cost/latency at volume, or `claude-sonnet-4-6` for a middle ground. |
| `CLIENT_VOICE` | Paste the client's actual tone/positioning — this is what makes the reply sound like them. |

---

## Notes

- **Always-on reply:** if Claude is unavailable, the engine still sends a clean fallback reply so no lead is ever dropped.
- **Prompt caching:** the per-client system prompt is marked cacheable. It only actually caches once the prompt exceeds the model's minimum cacheable prefix (~4096 tokens on Opus 4.7), so for short brand prompts it's a no-op — harmless, and a real saving for large brand briefs.
- **Cost:** replies are tiny (a few hundred tokens each), so even on Opus this is roughly a cent per lead.
- **Security:** the webhook is gated by the shared secret. Keep `.env` out of version control (add it to `.gitignore`).
- **Want non-blocking sends?** Move `send_email` calls into FastAPI `BackgroundTasks` if you'd rather return `200` to the form instantly and email a beat later.
