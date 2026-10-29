"""FastAPI app serving the GrantWatch deadline dashboard."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging

import html
import os
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv

load_dotenv()

from doc_checker.config import get_settings
from . import auth
from .document_checker_routes import router as document_checker_router

from grants.profile import Profile, load_profile
from grants.queries import GrantQuery, data_status, get_grant, query_grants
from grants.sql_utils import add_to_watchlist, list_watchlist, remove_from_watchlist
from notifications import store
from notifications.transport import build_message, get_transport, sender_address

settings = get_settings()

app = FastAPI(title="GrantWatch")

allow_origins = settings.allowed_origins or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins if "*" not in allow_origins else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

app.include_router(document_checker_router)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("grantwatch.web")


class LoginPayload(BaseModel):
    email: EmailStr


class WatchPayload(BaseModel):
    opp_id: str
    note: Optional[str] = None


@lru_cache(maxsize=1)
def _profile() -> Profile:
    return load_profile()


def _db_error(action: str, exc: Exception) -> HTTPException:
    logger.exception("Database query failed while %s", action)
    return HTTPException(status_code=500, detail=f"Database query failed while {action}.")


_TEMPLATE = Path(__file__).resolve().parent / "templates" / "index.html"
INDEX_HTML = _TEMPLATE.read_text(encoding="utf-8")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _page(title: str, body: str) -> HTMLResponse:
    """A small standalone page for the sign-in and unsubscribe flows."""
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)} · GrantWatch</title>"
        "<style>:root{color-scheme:light dark;--paper:#f7f8f6;--ink:#15221c;--mist:#5b6660;--spruce:#1e5a44}"
        "@media (prefers-color-scheme:dark){:root{--paper:#0f1613;--ink:#e6eae7;--mist:#97a39d;--spruce:#6fbf9a}}"
        "body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif}"
        "main{max-width:34rem;margin:0 auto;padding:48px 16px}h1{font-size:22px;margin:0 0 12px}a{color:var(--spruce)}"
        "button{font:inherit;font-weight:600;padding:8px 16px;border:0;border-radius:4px;background:var(--spruce);color:var(--paper);cursor:pointer}"
        "p.small{color:var(--mist);font-size:14px}</style></head>"
        f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").startswith("https")


def _public_base(request: Request) -> Optional[str]:
    """Where links in emails point. Never taken from a public request's Host header,
    which an attacker controls; only a local development server may use it."""
    configured = (os.getenv("GRANTS_DASHBOARD_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    if (urlsplit(str(request.base_url)).hostname or "") in _LOCAL_HOSTS:
        return str(request.base_url).rstrip("/")
    return None


def _session_email(request: Request) -> Optional[str]:
    email = auth.verify(request.cookies.get(auth.SESSION_COOKIE), auth.SESSION)
    if email is None:
        return None
    try:
        return email if store.active_recipient(email) else None  # removed members lose access
    except Exception as exc:
        logger.warning("Could not check the session against recipients: %s", exc)
        return None


def require_member(request: Request) -> str:
    if auth.secret_key() is None:
        raise HTTPException(status_code=503, detail="Sign-in is not configured on this server (GRANTS_SECRET_KEY).")
    email = _session_email(request)
    if email is None:
        raise HTTPException(status_code=401, detail="Sign in to change the tracked list.")
    return email


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


@app.get("/api/profile")
def profile_info() -> Dict[str, Any]:
    profile = _profile()
    try:
        status = data_status()
    except Exception as exc:
        logger.warning("Could not read data status: %s", exc)
        status = None
    first_sentence = profile.summary.split(". ")[0].strip() if profile.is_configured else ""
    return {
        "configured": profile.is_configured,
        "summary": first_sentence,
        "institution": profile.institution,
        "min_score": profile.min_score_to_notify,
        "horizons": profile.deadline_horizons,
        "include_forecasted": profile.include_forecasted,
        "data": status,
    }


@app.get("/api/grants")
def get_grants(
    q: str = Query(default=""),
    min_score: Optional[int] = Query(default=None, ge=0, le=5),
    closing_within: Optional[int] = Query(default=None, ge=1, le=3650),
    include_forecasted: bool = Query(default=True),
    include_closed: bool = Query(default=False),
    watched_only: bool = Query(default=False),
    stage: Optional[str] = Query(default=None),
    due_from: Optional[date] = Query(default=None),
    due_to: Optional[date] = Query(default=None),
    limit: int = Query(default=300, ge=1, le=1000),
) -> Dict[str, Any]:
    if stage and stage not in {"concept", "full"}:
        raise HTTPException(status_code=400, detail="Stage must be 'concept' or 'full'")
    if due_from and due_to and due_from > due_to:
        raise HTTPException(status_code=400, detail="due_from cannot be after due_to")

    query = GrantQuery(
        q=q,
        min_score=min_score,
        closing_within=closing_within,
        include_forecasted=include_forecasted,
        include_closed=include_closed,
        watched_only=watched_only,
        stage=stage,
        due_from=due_from,
        due_to=due_to,
        limit=limit,
    )
    try:
        results = query_grants(query)
    except Exception as exc:
        raise _db_error("fetching grants", exc) from exc
    return {"results": results, "count": len(results)}


@app.get("/api/grants/{opp_id:path}")
def grant_detail(opp_id: str) -> Dict[str, Any]:
    try:
        grant = get_grant(opp_id)
    except Exception as exc:
        raise _db_error("fetching the grant", exc) from exc
    if grant is None:
        raise HTTPException(status_code=404, detail="Grant not found.")
    return grant


@app.get("/api/watchlist")
def watchlist() -> Dict[str, List[Dict[str, Any]]]:
    try:
        rows = list_watchlist()
    except Exception as exc:
        raise _db_error("loading the watchlist", exc) from exc
    for row in rows:
        for key in ("created_at", "close_date"):
            if row.get(key) is not None:
                row[key] = row[key].isoformat()
    return {"results": rows}


@app.post("/api/watchlist", status_code=201)
def watch(payload: WatchPayload, _member: str = Depends(require_member)) -> Dict[str, Any]:
    opp_id = payload.opp_id.strip()
    if not opp_id:
        raise HTTPException(status_code=400, detail="opp_id is required.")
    try:
        add_to_watchlist(opp_id, payload.note)
    except Exception as exc:
        raise _db_error("saving the watchlist", exc) from exc
    return {"opp_id": opp_id, "watched": True}


@app.delete("/api/watchlist/{opp_id:path}")
def unwatch(opp_id: str, _member: str = Depends(require_member)) -> Dict[str, Any]:
    try:
        removed = remove_from_watchlist(opp_id)
    except Exception as exc:
        raise _db_error("updating the watchlist", exc) from exc
    return {"opp_id": opp_id, "watched": False, "removed": removed}


@app.get("/api/session")
def session(request: Request) -> Dict[str, Any]:
    configured = auth.secret_key() is not None
    return {"auth_configured": configured, "email": _session_email(request) if configured else None}


@app.post("/api/login")
def login(payload: LoginPayload, request: Request) -> Dict[str, str]:
    """Email a sign-in link to a team member. The reply is the same whether or not
    the address is on the team, so the endpoint cannot be used to probe it."""
    if auth.secret_key() is None:
        raise HTTPException(status_code=503, detail="Sign-in is not configured on this server (GRANTS_SECRET_KEY).")
    base = _public_base(request)
    if base is None:
        raise HTTPException(status_code=503, detail="Sign-in links need GRANTS_DASHBOARD_URL on this server.")
    try:
        transport = get_transport()
    except ValueError as exc:
        logger.error("Email transport misconfigured: %s", exc)
        transport = None
    if transport is None:
        raise HTTPException(status_code=503, detail="Email is not configured on this server.")
    reply = {"detail": "If that address is on the GrantWatch team, a sign-in link is on its way. It works for 15 minutes."}
    try:
        member = store.claim_login_email(str(payload.email))
    except Exception as exc:
        raise _db_error("checking the team list", exc) from exc
    if member is None:
        return reply
    link = f"{base}/auth?token={auth.sign(auth.LOGIN, member.email, auth.LINK_SECONDS)}"
    text = (f"Follow this link to sign in to GrantWatch:\n\n{link}\n\n"
            "It works for 15 minutes. If you did not ask for it, ignore this email.\n")
    body = (f'<p>Follow this link to sign in to GrantWatch:</p><p><a href="{html.escape(link, quote=True)}">Sign in to GrantWatch</a></p>'
            "<p>It works for 15 minutes. If you did not ask for it, ignore this email.</p>")
    try:
        transport.send(build_message(sender_address(), member.email, "Your GrantWatch sign-in link", text, body))
    except Exception:
        logger.exception("Sending a sign-in link failed")
        raise HTTPException(status_code=502, detail="Could not send the sign-in email. Try again in a minute.")
    return reply


@app.get("/auth")
def finish_login(request: Request, token: str = Query(default="")) -> Response:
    email = auth.verify(token, auth.LOGIN)
    try:
        member = store.active_recipient(email) if email else None
    except Exception as exc:
        raise _db_error("checking the team list", exc) from exc
    if member is None:
        return _page("That link has expired", '<p>Sign-in links work for 15 minutes. <a href="/">Request a new one</a>.</p>')
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, auth.sign(auth.SESSION, member.email, auth.SESSION_SECONDS),
        max_age=auth.SESSION_SECONDS, httponly=True, secure=_secure(request), samesite="lax",
    )
    return response


@app.post("/api/logout")
def logout() -> Response:
    response = Response(status_code=204)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@app.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(token: str = Query(default="")) -> HTMLResponse:
    # Only a confirmation form: mail scanners fetch links, so a GET must not unsubscribe.
    try:
        row = store.recipient_for_token(token)
    except Exception as exc:
        raise _db_error("looking up the unsubscribe link", exc) from exc
    if row is None:
        return _page("Link not recognised", "<p>This unsubscribe link is not valid. It may have been replaced by a newer one.</p>")
    email = html.escape(row["email"])
    if not row["active"]:
        return _page("Already unsubscribed", f"<p>{email} does not get GrantWatch email.</p>")
    return _page(
        "Stop GrantWatch email?",
        f'<p>{email} will stop getting new-grant alerts, deadline reminders and the weekly overview.</p>'
        f'<form method="post" action="/unsubscribe?token={html.escape(token, quote=True)}"><button type="submit">Unsubscribe</button></form>'
        '<p class="small">The team admin can add you back with <code>python main.py recipients add</code>.</p>',
    )


@app.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(token: str = Query(default="")) -> HTMLResponse:
    """Handles the confirmation form and RFC 8058 one-click requests from mail clients."""
    try:
        email = store.unsubscribe(token)
    except Exception as exc:
        raise _db_error("unsubscribing", exc) from exc
    if email is None:
        return _page("Link not recognised", "<p>This unsubscribe link is not valid.</p>")
    return _page("Unsubscribed", f"<p>{html.escape(email)} will not get GrantWatch email any more.</p>")
