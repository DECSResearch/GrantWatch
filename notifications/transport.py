"""Build email messages and hand them to SMTP, the Gmail API, or an outbox folder.

The transport is picked by GRANTS_EMAIL_TRANSPORT (smtp, gmail, or outbox).
When it is unset, the first configured one wins: GRANTS_SMTP_HOST, then
GMAIL_TOKEN_FILE / GMAIL_TOKEN_JSON, then GRANTS_EMAIL_OUTBOX.

    GRANTS_EMAIL_FROM        sender address (falls back to GMAIL_SENDER_EMAIL, then GRANTS_SMTP_USER)
    GRANTS_SMTP_HOST         e.g. smtp.gmail.com, email-smtp.us-east-1.amazonaws.com, smtp.office365.com
    GRANTS_SMTP_PORT         default 587
    GRANTS_SMTP_SECURITY     starttls (default), ssl (port 465), or none
    GRANTS_SMTP_USER / GRANTS_SMTP_PASSWORD
    GRANTS_EMAIL_OUTBOX      folder that receives .eml files instead of sending them
"""
from __future__ import annotations

import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Optional, Protocol

from notifications import gmail_notifier


class Transport(Protocol):
    name: str

    def send(self, message: EmailMessage) -> None:
        """Deliver ``message``; raise on failure."""


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def sender_address() -> str:
    return _env("GRANTS_EMAIL_FROM") or _env("GMAIL_SENDER_EMAIL") or _env("GRANTS_SMTP_USER")


def build_message(sender: str, to: str, subject: str, text: str, html: str, unsubscribe_url: str = "") -> EmailMessage:
    message = EmailMessage()
    if sender:
        message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    domain = sender.rsplit("@", 1)[-1].strip("> ") if "@" in sender else None
    message["Message-ID"] = make_msgid(domain=domain)
    if unsubscribe_url:
        # RFC 8058 one-click unsubscribe: mail clients POST to this URL.
        message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    # Quoted-printable keeps the encoded size close to the text size, which is
    # what Gmail's ~102 KB clipping limit is measured against.
    message.set_content(text, subtype="plain", charset="utf-8", cte="quoted-printable")
    if html:
        message.add_alternative(html, subtype="html", charset="utf-8", cte="quoted-printable")
    return message


class SmtpTransport:
    name = "smtp"

    def __init__(self, host: str, port: int, security: str, user: str, password: str, timeout: float = 30.0):
        self.host, self.port, self.security = host, port, security
        self.user, self.password, self.timeout = user, password, timeout

    @classmethod
    def from_env(cls) -> "SmtpTransport":
        security = _env("GRANTS_SMTP_SECURITY", "starttls").lower()
        default_port = "465" if security == "ssl" else "587"
        return cls(
            host=_env("GRANTS_SMTP_HOST"),
            port=int(_env("GRANTS_SMTP_PORT", default_port)),
            security=security,
            user=_env("GRANTS_SMTP_USER"),
            password=os.getenv("GRANTS_SMTP_PASSWORD") or "",
        )

    def send(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self.security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        with server:
            if self.security == "starttls":
                server.starttls(context=context)
            if self.user:
                server.login(self.user, self.password)
            server.send_message(message)


class GmailTransport:
    name = "gmail"

    def send(self, message: EmailMessage) -> None:
        gmail_notifier.send_raw(message.as_bytes())


class OutboxTransport:
    """Writes each message to a folder as an .eml file; for dry runs and tests."""

    name = "outbox"

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.count = 0

    def send(self, message: EmailMessage) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.count += 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        to = re.sub(r"[^A-Za-z0-9@._-]", "_", str(message["To"] or "unknown"))
        (self.folder / f"{stamp}-{self.count:03d}-{to}.eml").write_bytes(message.as_bytes())


def get_transport() -> Optional[Transport]:
    choice = _env("GRANTS_EMAIL_TRANSPORT").lower()
    if not choice:
        if _env("GRANTS_SMTP_HOST"):
            choice = "smtp"
        elif gmail_notifier.gmail_configured():
            choice = "gmail"
        elif _env("GRANTS_EMAIL_OUTBOX"):
            choice = "outbox"
        else:
            return None
    if choice == "smtp":
        if not _env("GRANTS_SMTP_HOST"):
            raise ValueError("GRANTS_EMAIL_TRANSPORT=smtp needs GRANTS_SMTP_HOST")
        return SmtpTransport.from_env()
    if choice == "gmail":
        return GmailTransport()
    if choice == "outbox":
        return OutboxTransport(Path(_env("GRANTS_EMAIL_OUTBOX", "outbox")))
    raise ValueError(f"Unknown GRANTS_EMAIL_TRANSPORT '{choice}' (use smtp, gmail, or outbox)")
