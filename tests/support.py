"""In-memory stand-ins for the notify step's database and email transport."""
from __future__ import annotations

import re
from collections import defaultdict
from email.message import EmailMessage
from typing import Dict, Iterable, List, Sequence

from notifications.planner import CHANGED, GrantState, Recipient, SentRecord


class MemoryStore:
    """Behaves like PostgresStore, including the unique index on notifications."""

    def __init__(self, recipients: Iterable[Recipient] = (), grants: Iterable[GrantState] = ()):
        self.people: List[Recipient] = list(recipients)
        self.grant_list: List[GrantState] = list(grants)
        self.rows: List[SentRecord] = []
        self.locked = False
        self.synced: List[str] = []

    def ensure_schema(self) -> None:
        pass

    def sync_recipients(self, emails: Sequence[str]) -> int:
        self.synced.extend(emails)
        return 0

    def recipients(self) -> List[Recipient]:
        return list(self.people)

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


_SECTION = re.compile(r"^([A-Z][^-].*) \((\d+)\)$")


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
