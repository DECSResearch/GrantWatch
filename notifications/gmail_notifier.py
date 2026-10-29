"""Send mail through the Gmail API with an OAuth token (see scripts/generate_gmail_token.py)."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional, Sequence

from logs.status_logger import logger

_SCOPES: Sequence[str] = ("https://www.googleapis.com/auth/gmail.send",)


def gmail_configured() -> bool:
    return bool((os.getenv("GMAIL_TOKEN_JSON") or "").strip() or (os.getenv("GMAIL_TOKEN_FILE") or "").strip())


def _load_credentials():
    """Credentials from GMAIL_TOKEN_JSON (the token's contents) or GMAIL_TOKEN_FILE."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    inline = (os.getenv("GMAIL_TOKEN_JSON") or "").strip()
    token_path = (os.getenv("GMAIL_TOKEN_FILE") or "").strip()
    path: Optional[Path] = None
    if inline:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(inline), scopes=_SCOPES)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"GMAIL_TOKEN_JSON is not a valid token: {exc}") from exc
    elif token_path:
        path = Path(token_path)
        if not path.exists():
            raise RuntimeError(f"Gmail token file not found at {path}")
        try:
            creds = Credentials.from_authorized_user_file(str(path), scopes=_SCOPES)
        except Exception as exc:
            raise RuntimeError(f"Gmail token file at {path} is unreadable or malformed: {exc}") from exc
    else:
        raise RuntimeError("Set GMAIL_TOKEN_FILE or GMAIL_TOKEN_JSON to send through Gmail")

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # pragma: no cover - network heavy
            raise RuntimeError(
                f"Failed to refresh the Gmail token ({exc}). Tokens from an OAuth app in "
                "Testing mode expire after 7 days; publish the app or switch to SMTP."
            ) from exc
        # Persisting the refreshed token is best-effort; the in-memory
        # credentials are valid even if the write fails.
        if path is not None:
            try:
                path.write_text(creds.to_json(), encoding="utf-8")
            except OSError as exc:
                logger("warning", f"Could not persist refreshed Gmail token: {exc}")

    if not creds.valid:
        raise RuntimeError("Loaded Gmail credentials are invalid; cannot send email")
    return creds


def send_raw(message_bytes: bytes) -> None:
    """Send an RFC 822 message; raises on any failure."""
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=_load_credentials(), cache_discovery=False)
    raw = base64.urlsafe_b64encode(message_bytes).decode("ascii")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
