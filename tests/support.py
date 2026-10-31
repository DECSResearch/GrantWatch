"""In-memory stand-ins for the notify step's database and email transport, and a scripted cursor for SQL tests."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import replace
from email.message import EmailMessage
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from grants import sql_utils
from notifications.planner import CHANGED, GrantState, Personal, Recipient, SentRecord


class MemoryStore:
    """Behaves like PostgresStore, including the unique index on notifications.

    ``personal`` is each member's own scores and votes by address (what ``PostgresStore.personal``
    reads: every score stored for them, whatever its key). A recipient listed there has a
    profile of their own. ``personal_fails`` makes the read raise, as a database error would.
    """

    def __init__(self, recipients: Iterable[Recipient] = (), grants: Iterable[GrantState] = (), personal: Optional[Mapping[str, Personal]] = None):
        self.people: List[Recipient] = list(recipients)
        self.grant_list: List[GrantState] = list(grants)
        self.personal_data: Dict[str, Personal] = dict(personal or {})
        self.personal_fails = False
        self.personal_reads: List[List[str]] = []  # the addresses asked for, one entry per read
        self.rows: List[SentRecord] = []
        self.locked = False
        self.synced: List[str] = []

    def ensure_schema(self) -> None:
        pass

    def sync_recipients(self, emails: Sequence[str]) -> int:
        self.synced.extend(emails)
        return 0

    def recipients(self) -> List[Recipient]:
        return [replace(p, has_profile=True) if p.email in self.personal_data else p for p in self.people]

    def personal(self, emails: Sequence[str]) -> Dict[str, Personal]:
        self.personal_reads.append(list(emails))
        if self.personal_fails:
            raise RuntimeError("member_scores cannot be read")
        return {email: self.personal_data.get(email, Personal()) for email in emails}

    def grants(self) -> List[GrantState]:
        return list(self.grant_list)

    def sent(self, recipient_ids: Sequence[int]) -> List[SentRecord]:
        return [row for row in self.rows if row.recipient_id in set(recipient_ids)]

    def record(self, rows: Sequence[SentRecord]) -> int:
        keys = {row.key() for row in self.rows if row.kind != CHANGED}
        added = 0
        for row in rows:
            if row.kind != CHANGED and row.key() in keys:
                continue  # ON CONFLICT DO NOTHING
            self.rows.append(row)
            keys.add(row.key())
            added += 1
        return added

    def acquire_lock(self, minutes: int = 30) -> bool:
        if self.locked:
            return False
        self.locked = True
        return True

    def release_lock(self) -> None:
        self.locked = False


class CaptureTransport:
    name = "capture"

    def __init__(self) -> None:
        self.messages: List[EmailMessage] = []
        self.failing: set = set()

    def send(self, message: EmailMessage) -> None:
        if message["To"] in self.failing:
            raise RuntimeError("mail server unavailable")
        self.messages.append(message)


_SECTION = re.compile(r"^([A-Z][^-].*) \((\d+)(?: of \d+)?\)$")


def plain_text(message: EmailMessage) -> str:
    return message.get_body(("plain",)).get_content()


def sections(message: EmailMessage) -> Dict[str, List[str]]:
    """Section title -> opportunity ids listed under it (titles are 'Grant <id>')."""
    out: Dict[str, List[str]] = defaultdict(list)
    current = None
    for line in plain_text(message).splitlines():
        match = _SECTION.match(line)
        if match:
            current = match.group(1)
        elif line.startswith("- [") and current:
            out[current].append(line.split("] ", 1)[1].removeprefix("Grant "))
    return dict(out)


# What fetch_member_state says of a member on the team's profile.
TEAM_MEMBER = {"has_profile": False, "summary": None, "updated_at": None, "has_scores": False}


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.db.executed.append((sql, params))
        self.result = self.db.script[len(self.db.executed) - 1]

    def fetchone(self):
        return self.result[0]

    def fetchall(self):
        return self.result[1]


class FakeDb:
    """Answers the nth query with the nth (fetchone, fetchall) pair of ``script``."""

    def __init__(self, *script):
        self.executed, self.script = [], script

    def connection(self):
        db = self

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self, cursor_factory=None):
                return FakeCursor(db)

            def close(self):
                pass

        return Conn()


def patched(monkeypatch, *script):
    db = FakeDb(*script)
    monkeypatch.setattr(sql_utils, "get_connection", db.connection)
    return db
