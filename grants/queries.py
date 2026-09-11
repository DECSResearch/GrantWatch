"""Grant queries shared by the web API and the MCP server."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

from grants.sql_utils import db_connection

_COLUMNS = """
    g.opp_id, g.title, g.stage, g.opportunity_status, g.opportunity_category,
    g.funding_categories, g.agency, g.agency_code, g.url,
    g.post_date, g.close_date, g.archive_date,
    g.summary, g.matched_keywords, g.award_ceiling, g.estimated_total_funding,
    g.relevance_score, g.relevance_reason, g.relevance_model, g.relevance_scored_at,
    g.first_seen_at, g.last_seen_at,
    (w.opp_id IS NOT NULL) AS watched, w.note AS watch_note
"""


@dataclass
class GrantQuery:
    q: str = ""
    min_score: Optional[int] = None
    closing_within: Optional[int] = None
    include_forecasted: bool = True
    include_closed: bool = False
    watched_only: bool = False
    stage: Optional[str] = None
    due_from: Optional[date] = None
    due_to: Optional[date] = None
    limit: int = 300


def build_grants_sql(query: GrantQuery, *, with_description: bool = False) -> Tuple[str, List[Any]]:
    conditions: List[str] = []
    params: List[Any] = []

    if not query.include_closed:
        conditions.append("(g.close_date IS NULL OR g.close_date >= CURRENT_DATE)")
    if query.closing_within is not None:
        conditions.append("g.close_date IS NOT NULL")
        conditions.append("g.close_date < CURRENT_DATE + (%s || ' days')::interval")
        params.append(int(query.closing_within))
    if not query.include_forecasted:
        conditions.append("g.opportunity_status <> 'Forecasted'")
    if query.watched_only:
        conditions.append("w.opp_id IS NOT NULL")
    if query.min_score is not None and query.min_score > 0:
        conditions.append("(g.relevance_score >= %s OR w.opp_id IS NOT NULL)")
        params.append(int(query.min_score))
    if query.stage:
        conditions.append("g.stage = %s")
        params.append(query.stage)
    if query.due_from:
        conditions.append("g.close_date >= %s")
        params.append(query.due_from)
    if query.due_to:
        conditions.append("g.close_date <= %s")
        params.append(query.due_to)
    text = (query.q or "").strip()
    if text:
        like = f"%{text}%"
        conditions.append("(g.title ILIKE %s OR g.agency ILIKE %s OR g.summary ILIKE %s OR g.opp_id ILIKE %s)")
        params.extend([like, like, like, like])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    columns = _COLUMNS + (", g.description" if with_description else "")
    sql = f"""
        SELECT {columns}
        FROM grants g
        LEFT JOIN grant_watchlist w ON w.opp_id = g.opp_id
        {where}
        ORDER BY (g.close_date IS NULL), g.close_date, g.relevance_score DESC NULLS LAST, g.post_date DESC, g.title
        LIMIT %s
    """
    params.append(max(1, min(int(query.limit), 1000)))
    return sql, params


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def serialize_row(row: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """JSON-safe dict with a computed ``days_left``."""
    today = today or date.today()
    close = row.get("close_date")
    close_day = close.date() if isinstance(close, datetime) else close
    days_left = (close_day - today).days if isinstance(close_day, date) else None
    status = str(row.get("opportunity_status") or "")
    out = {
        "opp_id": row.get("opp_id"),
        "title": row.get("title"),
        "stage": row.get("stage"),
        "opportunity_status": status,
        "forecasted": status.lower() == "forecasted",
        "opportunity_category": row.get("opportunity_category"),
        "funding_categories": row.get("funding_categories"),
        "agency": row.get("agency"),
        "agency_code": row.get("agency_code"),
        "url": row.get("url"),
        "post_date": _iso(row.get("post_date")),
        "close_date": _iso(row.get("close_date")),
        "archive_date": _iso(row.get("archive_date")),
        "days_left": days_left,
        "summary": row.get("summary"),
        "matched_keywords": row.get("matched_keywords"),
        "award_ceiling": _number(row.get("award_ceiling")),
        "estimated_total_funding": _number(row.get("estimated_total_funding")),
        "relevance_score": row.get("relevance_score"),
        "relevance_reason": row.get("relevance_reason"),
        "relevance_model": row.get("relevance_model"),
        "relevance_scored_at": _iso(row.get("relevance_scored_at")),
        "first_seen_at": _iso(row.get("first_seen_at")),
        "last_seen_at": _iso(row.get("last_seen_at")),
        "watched": bool(row.get("watched")),
        "watch_note": row.get("watch_note"),
    }
    if "description" in row:
        out["description"] = row.get("description")
    return out


def query_grants(query: GrantQuery) -> List[Dict[str, Any]]:
    sql, params = build_grants_sql(query)
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    today = date.today()
    return [serialize_row(dict(row), today) for row in rows]


def get_grant(opp_id: str) -> Optional[Dict[str, Any]]:
    sql = f"""
        SELECT {_COLUMNS}, g.description
        FROM grants g
        LEFT JOIN grant_watchlist w ON w.opp_id = g.opp_id
        WHERE g.opp_id = %s
    """
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, ((opp_id or "").strip(),))
        row = cur.fetchone()
    return serialize_row(dict(row)) if row else None


def data_status() -> Dict[str, Any]:
    """Freshness and coverage numbers for the UI header and the MCP server."""
    sql = """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE close_date IS NULL OR close_date >= CURRENT_DATE) AS open,
               COUNT(relevance_score) AS scored,
               MAX(last_seen_at) AS last_seen_at,
               (SELECT COUNT(*) FROM grant_watchlist) AS watched
        FROM grants
    """
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        row = dict(cur.fetchone() or {})
    return {
        "total": int(row.get("total") or 0),
        "open": int(row.get("open") or 0),
        "scored": int(row.get("scored") or 0),
        "watched": int(row.get("watched") or 0),
        "data_as_of": _iso(row.get("last_seen_at")),
    }
