"""Grant queries shared by the web API and the MCP server."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

from grants.display import days_until_deadline, firm_deadline, group_rows, public_row, real_ceiling
from grants.sql_utils import db_connection, open_grant_sql
from llm_utils.gpt_summarizer import SUMMARY_CHARS, cut_summary
from llm_utils.relevance import RULE_MODEL

_TEAM_SCORES = "g.relevance_score, g.relevance_reason, g.relevance_model, g.relevance_scored_at,"

# A member's own scores, read through a join with member_scores as ``s`` for them. A grant the
# eligibility pre-check ruled out (the team's score came from the rule) is 0 with the rule's reason
# for everyone; one the member's profile has not scored has no score and says so, which fails
# any score floor.
NOT_SCORED_YET = "Not scored for your profile yet"
_RULED_OUT = f"g.relevance_model = '{RULE_MODEL}'"
_OWN_SCORE = f"CASE WHEN {_RULED_OUT} THEN 0 ELSE s.score END"
_OWN_REASON = f"CASE WHEN {_RULED_OUT} THEN g.relevance_reason WHEN s.score IS NULL THEN '{NOT_SCORED_YET}' ELSE s.reason END"
_OWN_SCORES = (
    f"{_OWN_SCORE} AS relevance_score, {_OWN_REASON} AS relevance_reason,"
    f" CASE WHEN {_RULED_OUT} THEN g.relevance_model ELSE s.model END AS relevance_model,"
    f" CASE WHEN {_RULED_OUT} THEN g.relevance_scored_at ELSE s.scored_at END AS relevance_scored_at,"
)
_OWN_JOIN = "LEFT JOIN member_scores s ON s.opp_id = g.opp_id AND s.email = %s"


def _columns(own: bool) -> str:
    return f"""
    g.opp_id, g.title, g.stage, g.opportunity_status, g.opportunity_category,
    g.funding_categories, g.agency, g.agency_code, g.url,
    g.post_date, g.close_date, g.archive_date,
    g.summary, g.matched_keywords, g.award_ceiling, g.estimated_total_funding,
    {_OWN_SCORES if own else _TEAM_SCORES}
    g.first_seen_at, g.last_seen_at,
    (w.opp_id IS NOT NULL) AS watched, w.note AS watch_note
"""


_COLUMNS = _columns(own=False)


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
    team_relevant: Tuple[str, ...] = ()  # postings marked Relevant by whose votes decide (grants.feedback.relevant_postings); they pass min_score
    today: Optional[date] = None  # the day the window counts from; a caller fixes it once so every part of a request agrees
    scores_for: Optional[str] = None  # a member's address: their own scores in place of the team's (see ``_OWN_SCORES``)


def _score(query: GrantQuery) -> str:
    """The SQL for the score a request ranks and filters by: the team's, or the member's own."""
    return _OWN_SCORE if query.scores_for else "g.relevance_score"


def _source(own: bool) -> str:
    return "FROM grants g LEFT JOIN grant_watchlist w ON w.opp_id = g.opp_id" + (f" {_OWN_JOIN}" if own else "")


def _from(query: GrantQuery) -> Tuple[str, List[Any]]:
    """FROM and its joins, with the parameters the joins take (the member's address comes first in every statement)."""
    return _source(bool(query.scores_for)), ([query.scores_for] if query.scores_for else [])


def _where(query: GrantQuery) -> Tuple[str, List[Any]]:
    conditions: List[str] = []
    params: List[Any] = []

    if not query.include_closed:
        conditions.append(open_grant_sql("g"))
    if query.closing_within is not None:
        # Inclusive, as in the email and the dashboard's groups: a grant closing
        # exactly N days out is within N days.
        conditions.append("g.close_date IS NOT NULL")
        conditions.append("g.close_date <= %s")
        params.append((query.today or date.today()) + timedelta(days=int(query.closing_within)))
    if not query.include_forecasted:
        conditions.append("g.opportunity_status <> 'Forecasted'")
    if query.watched_only:
        conditions.append("w.opp_id IS NOT NULL")
    if query.min_score is not None and query.min_score > 0:
        # A tracked grant passes whatever it scores, and so does one the team marked Relevant,
        # which the email treats as meeting the threshold.
        passes = [f"{_score(query)} >= %s", "w.opp_id IS NOT NULL"]
        params.append(int(query.min_score))
        if query.team_relevant:
            passes.append("g.opp_id = ANY(%s)")
            params.append(list(query.team_relevant))
        conditions.append(f"({' OR '.join(passes)})")
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

    return (f"WHERE {' AND '.join(conditions)}" if conditions else ""), params


def build_grants_sql(query: GrantQuery, *, with_description: bool = False) -> Tuple[str, List[Any]]:
    where, where_params = _where(query)
    source, params = _from(query)
    params = params + where_params
    columns = _columns(bool(query.scores_for)) + (", g.description" if with_description else "")
    sql = f"""
        SELECT {columns}
        {source}
        {where}
        ORDER BY (g.close_date IS NULL), g.close_date, {_score(query)} DESC NULLS LAST, g.post_date DESC, g.title
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
    """JSON-safe dict with a computed ``days_left``.

    A placeholder close date (see grants.display) has no ``days_left`` and
    ``firm_deadline`` false; a placeholder award ceiling is null.
    """
    today = today or date.today()
    close = row.get("close_date")
    close_day = close.date() if isinstance(close, datetime) else close
    if not isinstance(close_day, date):
        close_day = None
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
        "days_left": days_until_deadline(close_day, today),
        "firm_deadline": firm_deadline(close_day, today),
        "summary": row.get("summary"),
        "matched_keywords": row.get("matched_keywords"),
        "award_ceiling": real_ceiling(_number(row.get("award_ceiling"))),
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
    today = query.today or date.today()
    return [serialize_row(dict(row), today) for row in rows]


def count_grants(query: GrantQuery) -> int:
    """How many grants match, whatever the query's limit."""
    where, where_params = _where(query)
    source, params = _from(query)
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) {source} {where}", params + where_params)
        return int(cur.fetchone()[0])


def get_grant(opp_id: str, scores_for: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One grant with its description; with ``scores_for``, the score and reason are that member's own."""
    source, params = _from(GrantQuery(scores_for=scores_for))
    sql = f"""
        SELECT {_columns(bool(scores_for))}, g.description
        {source}
        WHERE g.opp_id = %s
    """
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params + [(opp_id or "").strip()])
        row = cur.fetchone()
    return serialize_row(dict(row)) if row else None


def _fit_sql(own: bool) -> str:
    """The open grants scoring at least a floor (``%s``), with what group_rows reads to fold postings of one program."""
    score = _OWN_SCORE if own else "g.relevance_score"
    return f"""
    SELECT g.opp_id, g.title, g.agency_code, g.close_date, g.post_date, g.opportunity_status,
           {score} AS relevance_score, (w.opp_id IS NOT NULL) AS watched
    {_source(own)}
    WHERE {open_grant_sql("g")} AND {score} >= %s
"""


_FIT_SQL = _fit_sql(own=False)


def data_status(min_score: Optional[int] = None, scores_for: Optional[str] = None) -> Dict[str, Any]:
    """Freshness and coverage numbers for the UI header and the MCP server.

    ``scored`` counts open grants only, so it is never more than ``open``. It counts the team's scores.

    With ``min_score``, ``fit_count`` is how many open grants score at least that
    (the team threshold), counted as the dashboard lists them: a program posted
    by several services is one. It is None without a ``min_score``. With
    ``scores_for`` it counts by that member's own scores instead of the team's.
    """
    sql = f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {open_grant_sql("g")}) AS open,
               COUNT(g.relevance_score) FILTER (WHERE {open_grant_sql("g")}) AS scored,
               MAX(g.last_seen_at) AS last_seen_at,
               (SELECT COUNT(*) FROM grant_watchlist) AS watched
        FROM grants g
    """
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        row = dict(cur.fetchone() or {})
        fit_count = None
        if min_score is not None:
            cur.execute(_fit_sql(True) if scores_for else _FIT_SQL, (scores_for, int(min_score)) if scores_for else (int(min_score),))
            fit_count = len(group_rows([dict(fit) for fit in cur.fetchall()]))
    return {
        "total": int(row.get("total") or 0),
        "open": int(row.get("open") or 0),
        "scored": int(row.get("scored") or 0),
        "watched": int(row.get("watched") or 0),
        "fit_count": fit_count,
        "data_as_of": _iso(row.get("last_seen_at")),
    }


# --- the public list ---------------------------------------------------------------------------------
#
# What a visitor who is not signed in sees is built by its own query, from Grants.gov's facts alone: no
# join with the watchlist, no relevance_* column, no model summary or matched keywords. The row that
# comes out of it is cut down again by ``public_row``, but the first line of defence is that the
# database is never asked for anything internal.

_PUBLIC_COLUMNS = f"""
    g.opp_id, g.title, g.stage, g.opportunity_status, g.opportunity_category, g.funding_categories,
    g.agency, g.agency_code, g.url, g.post_date, g.close_date, g.archive_date,
    g.award_ceiling, g.estimated_total_funding,
    LEFT(g.description, {SUMMARY_CHARS + 1}) AS description_head
"""
# By the deadline, then how new the posting is: never by a score, so a visitor cannot read the team's
# ranking from where a grant falls under ``limit`` or in a date window. ``opp_id`` makes it total.
_PUBLIC_ORDER = "g.close_date NULLS LAST, g.post_date DESC, g.title, g.opp_id"


def _public_where(query: GrantQuery) -> Tuple[str, List[Any]]:
    """The open grants the visitor's filters leave. A score floor, the watchlist and closed grants are not theirs to ask for."""
    conditions: List[str] = [open_grant_sql("g")]
    params: List[Any] = []
    if query.closing_within is not None:
        conditions.append("g.close_date IS NOT NULL")
        conditions.append("g.close_date <= %s")
        params.append((query.today or date.today()) + timedelta(days=int(query.closing_within)))
    if not query.include_forecasted:
        conditions.append("g.opportunity_status <> 'Forecasted'")
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
        # The description, not the model's summary: that is written from the team's profile.
        like = f"%{text}%"
        conditions.append("(g.title ILIKE %s OR g.agency ILIKE %s OR g.opp_id ILIKE %s OR g.description ILIKE %s)")
        params.extend([like, like, like, like])
    return f"WHERE {' AND '.join(conditions)}", params


def build_public_sql(query: GrantQuery) -> Tuple[str, List[Any]]:
    where, params = _public_where(query)
    sql = f"""
        SELECT {_PUBLIC_COLUMNS}
        FROM grants g
        {where}
        ORDER BY {_PUBLIC_ORDER}
        LIMIT %s
    """
    params.append(max(1, min(int(query.limit), 1000)))
    return sql, params


def serialize_public_row(row: Dict[str, Any], today: Optional[date] = None, *, detail: bool = False) -> Dict[str, Any]:
    """A visitor's JSON-safe row: ``serialize_row`` of Grants.gov's columns, the summary cut from the description, then ``public_row``."""
    data = dict(row)
    data["summary"] = cut_summary(data.pop("description_head", None) or "")
    return public_row(serialize_row(data, today), detail=detail)


def query_public_grants(query: GrantQuery) -> List[Dict[str, Any]]:
    """The open grants a visitor may see, one row per posting, in the public order (``group_rows`` folds them)."""
    sql, params = build_public_sql(query)
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    today = query.today or date.today()
    return [serialize_public_row(dict(row), today) for row in rows]


def count_public_grants(query: GrantQuery) -> int:
    """How many grants a visitor's filters match, whatever the limit."""
    where, params = _public_where(query)
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM grants g {where}", params)
        return int(cur.fetchone()[0])


def get_public_grant(opp_id: str) -> Optional[Dict[str, Any]]:
    """One grant as a visitor sees it: the public fields and the full Grants.gov description."""
    sql = f"SELECT {_PUBLIC_COLUMNS}, g.description FROM grants g WHERE g.opp_id = %s"
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, ((opp_id or "").strip(),))
        row = cur.fetchone()
    return serialize_public_row(dict(row), detail=True) if row else None


def public_status() -> Dict[str, Any]:
    """How many grants are open and when the list was last loaded: all a visitor is told about the data."""
    sql = f"""
        SELECT COUNT(*) FILTER (WHERE {open_grant_sql("g")}) AS open, MAX(g.last_seen_at) AS last_seen_at
        FROM grants g
    """
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        row = dict(cur.fetchone() or {})
    return {"open": int(row.get("open") or 0), "data_as_of": _iso(row.get("last_seen_at"))}


# --- a member's own profile and what has been scored for it --------------------------------------------


def fetch_member_state(email: str) -> Dict[str, Any]:
    """What decides whose scores a member sees: ``has_profile``, the profile's ``summary`` as stored and its ``updated_at``,
    and ``has_scores``, whether any score is stored for them (whatever key it was made under, as the email counts them).
    """
    sql = """
        SELECT p.summary, p.updated_at, EXISTS (SELECT 1 FROM member_scores s WHERE s.email = p.email) AS has_scores
        FROM member_profiles p WHERE p.email = %s
    """
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (email,))
        row = cur.fetchone()
    if row is None:
        return {"has_profile": False, "summary": None, "updated_at": None, "has_scores": False}
    return {"has_profile": True, "summary": row[0], "updated_at": row[1], "has_scores": bool(row[2])}


def member_coverage(email: str, key: Optional[str]) -> Dict[str, int]:
    """How many open grants are scored for this member's profile as it is now: ``{open, scored}``.

    Scored is the open grants with a score stored under ``key`` (the score key of their profile as the
    scoring pass makes it, ``grants.personal.current_key``; None when the text can never be scored),
    plus the ones the eligibility pre-check ruled out, which need no score of theirs. An older score
    does not count: it is shown until it is replaced, and the daily run still has it to do.
    """
    sql = f"""
        SELECT COUNT(*), COUNT(*) FILTER (WHERE {_RULED_OUT} OR s.score_key = %s)
        FROM grants g
        LEFT JOIN member_scores s ON s.opp_id = g.opp_id AND s.email = %s
        WHERE {open_grant_sql("g")}
    """
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (key, email))
        total, scored = cur.fetchone()
    return {"open": int(total), "scored": int(scored)}
