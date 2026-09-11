"""FastAPI app serving the GrantWatch deadline dashboard."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv

load_dotenv()

from doc_checker.config import get_settings
from .document_checker_routes import router as document_checker_router

from grants.profile import Profile, load_profile
from grants.queries import GrantQuery, data_status, get_grant, query_grants
from grants.sql_utils import (
    add_subscription,
    add_to_watchlist,
    available_subscription_fields,
    list_watchlist,
    remove_from_watchlist,
)

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


class SubscriptionPayload(BaseModel):
    email: EmailStr
    field: str


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


@app.get("/api/grants/{opp_id}")
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
def watch(payload: WatchPayload) -> Dict[str, Any]:
    opp_id = payload.opp_id.strip()
    if not opp_id:
        raise HTTPException(status_code=400, detail="opp_id is required.")
    try:
        add_to_watchlist(opp_id, payload.note)
    except Exception as exc:
        raise _db_error("saving the watchlist", exc) from exc
    return {"opp_id": opp_id, "watched": True}


@app.delete("/api/watchlist/{opp_id}")
def unwatch(opp_id: str) -> Dict[str, Any]:
    try:
        removed = remove_from_watchlist(opp_id)
    except Exception as exc:
        raise _db_error("updating the watchlist", exc) from exc
    return {"opp_id": opp_id, "watched": False, "removed": removed}


@app.get("/api/subscription-fields")
def subscription_fields(limit: int = Query(default=200, ge=1, le=500)) -> Dict[str, List[Dict[str, str]]]:
    try:
        options = available_subscription_fields(limit=limit)
    except Exception as exc:
        raise _db_error("loading subscription fields", exc) from exc
    if not options:
        options = [("concept", "Concept stage"), ("full", "Full stage")]

    fields: List[Dict[str, str]] = []
    seen = set()
    for key, label in options:
        key_lower = (key or "").strip().lower()
        if not key_lower or key_lower in seen:
            continue
        seen.add(key_lower)
        display = (label or "").strip() or key_lower.replace("_", " ").title()
        fields.append({"key": key_lower, "label": display})

    return {"fields": fields}


@app.post("/api/subscriptions")
def create_subscription(payload: SubscriptionPayload) -> Dict[str, Dict[str, str]]:
    field_key = payload.field.strip().lower()
    if not field_key:
        raise HTTPException(status_code=400, detail="Field selection is required.")

    try:
        available = {key.lower(): (label or key) for key, label in available_subscription_fields(limit=500)}
    except Exception as exc:
        raise _db_error("validating the subscription field", exc) from exc
    label = available.get(field_key)
    if not label:
        if field_key in {"concept", "full"}:
            label = "Concept stage" if field_key == "concept" else "Full stage"
        else:
            raise HTTPException(status_code=400, detail="Unknown field selection.")

    try:
        add_subscription(payload.email, field_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _db_error("saving the subscription", exc) from exc

    return {"field": {"key": field_key, "label": label}}
