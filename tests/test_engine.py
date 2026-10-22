import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

import main
from channels import SendResult, normalize_us_phone
from followups import due_time, process_due, within_business_hours

NY = ZoneInfo("America/New_York")
S = main.app.state
SECRET = {"X-Lead-Secret": "test-secret"}


class Outbox:
    def __init__(self):
        self.emails, self.texts = [], []
        self.unsubscribed = set()

    def email(self, to, subject, html, reply_to=None):
        self.emails.append({"to": to, "subject": subject, "html": html, "reply_to": reply_to})
        return SendResult(True)

    def sms(self, to, body):
        if to in self.unsubscribed:
            return SendResult(False, "21610: unsubscribed", unsubscribed=True)
        self.texts.append({"to": to, "body": body})
        return SendResult(True)


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(S.channels, "email", box.email)
    monkeypatch.setattr(S.channels, "sms", box.sms)
    return box


@pytest.fixture
def client():
    return TestClient(main.app)


def post_lead(client, **overrides):
    body = {
        "name": "Jane Doe",
        "email": f"jane{time.time_ns()}@example.com",
        "message": "Do you do SEO retainers?",
        "phone": "(415) 555-0100",
        "sms_consent": "on",
    }
    body.update(overrides)
    r = client.post("/lead", json=body, headers=SECRET)
    assert r.status_code == 200, r.text
    return r.json(), body


def test_phone_normalization():
    assert normalize_us_phone("(415) 555-0100") == "+14155550100"
    assert normalize_us_phone("1-415-555-0100") == "+14155550100"
    assert normalize_us_phone("+44 20 7946 0958") is None
    assert normalize_us_phone("") is None


def test_business_hours_window():
    friday_night = datetime(2026, 10, 23, 21, 30, tzinfo=NY).timestamp()
    moved = datetime.fromtimestamp(within_business_hours(friday_night, NY), NY)
    assert (moved.weekday(), moved.hour, moved.minute) == (0, 9, 0)  # Monday 9:00

    early = datetime(2026, 10, 21, 6, 0, tzinfo=NY).timestamp()
    assert datetime.fromtimestamp(within_business_hours(early, NY), NY).hour == 9

    midday = datetime(2026, 10, 21, 13, 15, tzinfo=NY).timestamp()
    assert within_business_hours(midday, NY) == midday


def test_rejects_bad_secret(client):
    r = client.post("/lead", json={"email": "a@b.co"}, headers={"X-Lead-Secret": "nope"})
    assert r.status_code == 401


def test_lead_gets_email_text_alert_and_followup_schedule(client, outbox):
    result, body = post_lead(client)
    assert result["replied"] and result["texted"]

    reply = outbox.emails[0]
    assert reply["to"] == body["email"] and reply["reply_to"] == "owner@acme.test"
    assert "https://cal.example.com/acme" in reply["html"]

    text = outbox.texts[0]
    assert text["to"] == "+14155550100"
    assert text["body"].startswith("Acme Agency: ") and text["body"].endswith("Reply STOP to opt out.")

    alert = outbox.emails[1]
    assert alert["to"] == "owner@acme.test" and "/stop/" in alert["html"]

    lead = S.store.get(result["lead_id"])
    assert lead.first_reply_at - lead.received_at < 60
    assert lead.next_followup_at == due_time(main.load_settings(), lead.received_at, 1)


def test_no_text_without_consent(client, outbox):
    result, _ = post_lead(client, sms_consent=None)
    assert result["replied"] and not result["texted"]
    assert outbox.texts == []


def test_alert_escapes_lead_html(client, outbox):
    post_lead(client, name="<script>x</script>", message="<img src=x onerror=alert(1)>")
    alert = outbox.emails[1]["html"]
    assert "<script>" not in alert and "<img" not in alert


def test_followups_run_then_complete(client, outbox):
    result, body = post_lead(client)
    lead_id = result["lead_id"]
    settings = main.load_settings()

    for step in (1, 2, 3):
        lead = S.store.get(lead_id)
        outbox.emails.clear(), outbox.texts.clear()
        process_due(settings, S.store, S.drafter, S.channels, now=lead.next_followup_at + 1)
        assert outbox.emails[0]["subject"].startswith("Re: ")
        assert "Reply STOP" not in outbox.texts[0]["body"]
        assert S.store.get(lead_id).followups_sent == step

    final = S.store.get(lead_id)
    assert final.status == "completed" and final.next_followup_at is None


def test_booking_stops_followups(client, outbox):
    result, body = post_lead(client)
    calendly = {"event": "invitee.created", "payload": {"email": body["email"].upper()}}
    r = client.post("/booked?key=test-secret", json=calendly)
    assert r.json()["stopped"] == 1
    assert S.store.get(result["lead_id"]).status == "booked"

    result2, body2 = post_lead(client)
    calcom = {"triggerEvent": "BOOKING_CREATED", "payload": {"attendees": [{"email": body2["email"]}]}}
    assert client.post("/booked?key=test-secret", json=calcom).json()["stopped"] == 1

    assert client.post("/booked?key=wrong", json=calcom).status_code == 401


def _twilio_post(client, params, token="twilio-token"):
    url = "https://engine.acme.test/sms/inbound"
    sig = RequestValidator(token).compute_signature(url, params)
    return client.post("/sms/inbound", data=params, headers={"X-Twilio-Signature": sig})


def test_inbound_text_stops_followups_and_forwards(client, outbox):
    result, _ = post_lead(client, phone="415-555-0177")
    outbox.emails.clear()
    r = _twilio_post(client, {"From": "+14155550177", "Body": "Yes, Thursday works", "MessageSid": "SM1"})
    assert r.status_code == 200 and "<Response>" in r.text
    assert S.store.get(result["lead_id"]).status == "replied"
    assert "Thursday works" in outbox.emails[0]["html"]


def test_inbound_stop_opts_out(client, outbox):
    result, _ = post_lead(client, phone="415-555-0188")
    _twilio_post(client, {"From": "+14155550188", "Body": "STOP", "MessageSid": "SM2"})
    assert S.store.get(result["lead_id"]).sms_opted_out


def test_inbound_rejects_bad_signature(client, outbox):
    r = _twilio_post(client, {"From": "+14155550100", "Body": "hi"}, token="wrong")
    assert r.status_code == 403


def test_stop_link_needs_post_and_valid_token(client, outbox):
    result, _ = post_lead(client)
    lead_id = result["lead_id"]
    token = main.stop_token("test-secret", lead_id)

    assert client.get(f"/stop/{lead_id}/{token}").status_code == 200
    assert S.store.get(lead_id).status == "active"  # GET alone changes nothing
    assert client.post(f"/stop/{lead_id}/bad").status_code == 404
    assert client.post(f"/stop/{lead_id}/{token}").status_code == 200
    assert S.store.get(lead_id).status == "stopped"


def test_new_enquiry_supersedes_running_sequence(client, outbox):
    first, body = post_lead(client)
    second, _ = post_lead(client, email=body["email"])
    assert S.store.get(first["lead_id"]).status == "superseded"
    assert S.store.get(second["lead_id"]).status == "active"


def test_unsubscribed_number_marked(client, outbox):
    outbox.unsubscribed.add("+14155550199")
    result, _ = post_lead(client, phone="4155550199")
    assert not result["texted"]
    assert S.store.get(result["lead_id"]).sms_opted_out


def test_stats(client, outbox):
    post_lead(client)
    r = client.get("/stats?key=test-secret&days=1").json()
    assert r["leads"] >= 1 and r["over_60_seconds"] == 0 and r["median_seconds"] is not None
    assert client.get("/stats?key=nope").status_code == 401
