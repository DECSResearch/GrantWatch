"""Email sign-in for the dashboard: signed, expiring tokens with no extra dependencies.

A team member asks for a link, follows it, and gets a session cookie. Both
the link and the cookie are HMAC-signed with GRANTS_SECRET_KEY; without that
key nobody can sign in and the dashboard is read-only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

SESSION_COOKIE = "gw_session"
LINK_SECONDS = 15 * 60
SESSION_SECONDS = 30 * 24 * 3600
LOGIN = "login"
SESSION = "session"


def secret_key() -> Optional[bytes]:
    value = (os.getenv("GRANTS_SECRET_KEY") or "").strip()
    return value.encode("utf-8") if len(value) >= 16 else None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(key: bytes, purpose: str, body: str) -> str:
    return _b64(hmac.new(key, f"{purpose}.{body}".encode("ascii"), hashlib.sha256).digest())


def sign(purpose: str, email: str, seconds: int, now: Optional[float] = None) -> str:
    key = secret_key()
    if key is None:
        raise RuntimeError("GRANTS_SECRET_KEY is not set (16 characters or more)")
    expires = int(now if now is not None else time.time()) + int(seconds)
    body = _b64(json.dumps({"e": email, "x": expires}, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_signature(key, purpose, body)}"


def verify(token: Optional[str], purpose: str, now: Optional[float] = None) -> Optional[str]:
    """The email inside a valid, unexpired ``purpose`` token, else None."""
    key = secret_key()
    if key is None or not token or token.count(".") != 1:
        return None
    body, signature = token.split(".")
    if not hmac.compare_digest(signature, _signature(key, purpose, body)):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(payload, dict) or int(payload.get("x", 0)) < int(now if now is not None else time.time()):
        return None
    email = payload.get("e")
    return email if isinstance(email, str) and email else None
