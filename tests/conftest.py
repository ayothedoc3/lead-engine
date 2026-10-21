import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Configure a full-featured instance before main.py is imported.
_db = Path(tempfile.mkdtemp()) / "engine.db"
os.environ.update({
    "RESEND_API_KEY": "re_test",
    "LEAD_ENGINE_SECRET": "test-secret",
    "CLIENT_NAME": "Acme Agency",
    "CLIENT_VOICE": "Warm and concise.",
    "BOOKING_LINK": "https://cal.example.com/acme",
    "FROM_EMAIL": "Acme <hello@acme.test>",
    "REPLY_TO_EMAIL": "owner@acme.test",
    "ALERT_EMAIL": "owner@acme.test",
    "PUBLIC_BASE_URL": "https://engine.acme.test",
    "DATABASE_PATH": str(_db),
    "CLIENT_TIMEZONE": "America/New_York",
    "FOLLOWUP_DAYS": "1,3,5",
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "twilio-token",
    "TWILIO_MESSAGING_SERVICE_SID": "MGtest",
    "ANTHROPIC_API_KEY": "",
})
