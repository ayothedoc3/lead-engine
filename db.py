"""SQLite storage: one row per lead plus a log of every message sent or received.

The log is the delivery record behind the 60-Second Guarantee and the monthly
response-time report.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    name TEXT,
    company TEXT,
    phone TEXT,
    message TEXT,
    source TEXT,
    sms_consent INTEGER NOT NULL DEFAULT 0,
    sms_opted_out INTEGER NOT NULL DEFAULT 0,
    received_at REAL NOT NULL,
    first_reply_at REAL,
    first_subject TEXT,
    first_body TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    followups_sent INTEGER NOT NULL DEFAULT 0,
    next_followup_at REAL
);
CREATE INDEX IF NOT EXISTS leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS leads_phone ON leads(phone);
CREATE INDEX IF NOT EXISTS leads_due ON leads(status, next_followup_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    kind TEXT NOT NULL,      -- reply | followup | inbound_sms
    channel TEXT NOT NULL,   -- email | sms
    step INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_lead ON messages(lead_id);
"""

# Statuses that end a follow-up sequence.
BOOKED, REPLIED, STOPPED, COMPLETED, SUPERSEDED = "booked", "replied", "stopped", "completed", "superseded"
ACTIVE = "active"


@dataclass(frozen=True)
class Lead:
    id: int
    email: str
    name: Optional[str]
    company: Optional[str]
    phone: Optional[str]
    message: Optional[str]
    source: Optional[str]
    sms_consent: bool
    sms_opted_out: bool
    received_at: float
    first_reply_at: Optional[float]
    first_subject: Optional[str]
    first_body: Optional[str]
    status: str
    followups_sent: int
    next_followup_at: Optional[float]

    @property
    def first_name(self) -> str:
        return (self.name or "").strip().split(" ")[0] or "there"

    @property
    def can_text(self) -> bool:
        return bool(self.phone) and self.sms_consent and not self.sms_opted_out


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _lead(row: sqlite3.Row) -> Lead:
        d = dict(row)
        d["sms_consent"] = bool(d["sms_consent"])
        d["sms_opted_out"] = bool(d["sms_opted_out"])
        return Lead(**d)

    def create_lead(
        self,
        *,
        email: str,
        name: Optional[str],
        company: Optional[str],
        phone: Optional[str],
        message: Optional[str],
        source: Optional[str],
        sms_consent: bool,
        received_at: float,
    ) -> Lead:
        with self._conn() as c:
            # A new enquiry from the same person replaces any sequence still running.
            c.execute(
                "UPDATE leads SET status=?, next_followup_at=NULL WHERE lower(email)=lower(?) AND status=?",
                (SUPERSEDED, email, ACTIVE),
            )
            cur = c.execute(
                """INSERT INTO leads (email, name, company, phone, message, source, sms_consent, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (email, name, company, phone, message, source, int(sms_consent), received_at),
            )
            row = c.execute("SELECT * FROM leads WHERE id=?", (cur.lastrowid,)).fetchone()
        return self._lead(row)

    def get(self, lead_id: int) -> Optional[Lead]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return self._lead(row) if row else None

    def latest_by_phone(self, phone: str) -> Optional[Lead]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM leads WHERE phone=? ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
        return self._lead(row) if row else None

    def record_first_reply(self, lead_id: int, at: float, subject: str, body: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE leads SET first_reply_at=?, first_subject=?, first_body=? WHERE id=?",
                (at, subject, body, lead_id),
            )

    def schedule(self, lead_id: int, next_at: Optional[float], followups_sent: int) -> None:
        """Set the next follow-up time; None completes the sequence."""
        with self._conn() as c:
            if next_at is None:
                c.execute(
                    "UPDATE leads SET next_followup_at=NULL, followups_sent=?, status=? WHERE id=? AND status=?",
                    (followups_sent, COMPLETED, lead_id, ACTIVE),
                )
            else:
                c.execute(
                    "UPDATE leads SET next_followup_at=?, followups_sent=? WHERE id=? AND status=?",
                    (next_at, followups_sent, lead_id, ACTIVE),
                )

    def end_sequence(self, lead_id: int, status: str) -> bool:
        """Stop follow-ups for one lead. Returns True if it was still active."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE leads SET status=?, next_followup_at=NULL WHERE id=? AND status=?",
                (status, lead_id, ACTIVE),
            )
        return cur.rowcount > 0

    def end_sequences_for_email(self, email: str, status: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE leads SET status=?, next_followup_at=NULL WHERE lower(email)=lower(?) AND status=?",
                (status, email, ACTIVE),
            )
        return cur.rowcount

    def mark_sms_opted_out(self, phone: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE leads SET sms_opted_out=1 WHERE phone=?", (phone,))

    def claim_due(self, now: float) -> list[Lead]:
        """Return active leads whose next follow-up is due, clearing their due time so
        a crash mid-send never sends the same step twice."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM leads WHERE status=? AND next_followup_at IS NOT NULL AND next_followup_at<=? ORDER BY id",
                (ACTIVE, now),
            ).fetchall()
            c.executemany("UPDATE leads SET next_followup_at=NULL WHERE id=?", [(r["id"],) for r in rows])
        return [self._lead(r) for r in rows]

    def log_message(
        self, lead_id: int, kind: str, channel: str, body: str, ok: bool, error: Optional[str] = None, step: int = 0
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO messages (lead_id, kind, channel, step, body, ok, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (lead_id, kind, channel, step, body, int(ok), error, time.time()),
            )

    def messages_for(self, lead_id: int) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM messages WHERE lead_id=? ORDER BY id", (lead_id,)).fetchall()

    def leads_since(self, since: float) -> list[Lead]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM leads WHERE received_at>=? ORDER BY id", (since,)).fetchall()
        return [self._lead(r) for r in rows]
