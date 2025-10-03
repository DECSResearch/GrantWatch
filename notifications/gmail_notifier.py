"""Send grant notifications via the Gmail API."""
from __future__ import annotations

import base64
import os
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable, List, Sequence

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from logs.status_logger import logger

_SCOPES: Sequence[str] = ("https://www.googleapis.com/auth/gmail.send",)


def _load_credentials() -> Credentials | None:
    token_path = os.getenv("GMAIL_TOKEN_FILE")
    if not token_path:
        logger("warning", "GMAIL_TOKEN_FILE is not configured; skipping email notification")
        return None

    path = Path(token_path)
    if not path.exists():
        logger("error", f"Gmail token file not found at {path}")
        return None

    creds = Credentials.from_authorized_user_file(str(path), scopes=_SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            path.write_text(creds.to_json(), encoding="utf-8")
        except Exception as exc:  # pragma: no cover - network/IO heavy
            logger("error", f"Failed to refresh Gmail token: {exc}")
            return None

    if not creds.valid:
        logger("error", "Loaded Gmail credentials are invalid; cannot send email")
        return None

    return creds


def _normalise_recipients(recipients: Iterable[str]) -> List[str]:
    return [recipient.strip() for recipient in recipients if recipient and recipient.strip()]


def _build_message(sender: str, to: Sequence[str], subject: str, body: str) -> dict[str, str]:
    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = ", ".join(to)
    mime["From"] = sender
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
    return {"raw": raw}


def send_grant_notification(subject: str, body: str, recipients: Iterable[str]) -> bool:
    """Send a Gmail notification; returns True if the message was dispatched."""
    recipients_list = _normalise_recipients(recipients)
    if not recipients_list:
        logger("info", "No Gmail recipients configured; skipping notification")
        return False

    sender = os.getenv("GMAIL_SENDER_EMAIL") or recipients_list[0]

    creds = _load_credentials()
    if creds is None:
        return False

    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        message = _build_message(sender, recipients_list, subject, body)
        service.users().messages().send(userId="me", body=message).execute()
        logger("info", f"Sent Gmail notification to {', '.join(recipients_list)}")
        return True
    except HttpError as exc:
        logger("error", f"Gmail API error while sending notification: {exc}")
    except Exception as exc:
        logger("error", f"Unexpected failure sending Gmail notification: {exc}")

    return False


def notify_grant_release(grants: Sequence[dict[str, object]], csv_path: str | None) -> None:
    recipients_env = os.getenv("GMAIL_NOTIFY_RECIPIENTS", "")
    recipients = _normalise_recipients(recipients_env.split(","))
    if not recipients:
        logger("info", "GMAIL_NOTIFY_RECIPIENTS not configured; no email will be sent")
        return

    if not grants:
        logger("info", "No grants to include in email notification; skipping")
        return

    top_grants = grants[:5]
    lines = [
        "New Grants.gov opportunities that match your filters:",
        "",
    ]
    for grant in top_grants:
        title = str(grant.get("OPPORTUNITY_TITLE", "Untitled"))
        close_date = grant.get("CLOSE_DATE") or "N/A"
        agency = grant.get("AGENCY") or "Unknown agency"
        url = grant.get("OPPORTUNITY_URL") or ""
        line = f"- {title} | Close: {close_date} | Agency: {agency}"
        if url:
            line += f"\n  {url}"
        lines.append(line)
        lines.append("")

    if csv_path:
        lines.append(f"Full export: {csv_path}")

    subject = os.getenv("GMAIL_SUBJECT", "GrantWatch: new opportunities posted")
    body = "\n".join(lines).strip()

    send_grant_notification(subject, body, recipients)
