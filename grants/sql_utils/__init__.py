import os
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor

from grants.display import Posting, program_of
from grants.feedback import Votes, clean_text, relevant_postings

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


_DSN_ENV_VARS = (
    "POSTGRES_PRISMA_URL",
    "POSTGRES_URL",
    "POSTGRES_URL_NON_POOLING",
    "DATABASE_URL",
    "DATABASE_URL_UNPOOLED",
    "NEON_DATABASE_URL",
    "VERCEL_POSTGRES_URL",
    "PGDATABASE_URL",
)


def _env(name: str, default: str) -> str:
    # Strip whitespace: env vars pasted into dashboards (e.g. Vercel) often
    # carry a trailing newline, which Postgres rejects inside DSN values.
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _connection_kwargs() -> Dict[str, Any]:
    url = _first_env(*_DSN_ENV_VARS)
    if url:
        return {"dsn": url}

    host = _env("POSTGRES_HOST", _env("PGHOST", "localhost"))
    kwargs: Dict[str, Any] = {
        "dbname": _env("POSTGRES_DB", _env("PGDATABASE", "your_db")),
        "user": _env("POSTGRES_USER", _env("PGUSER", "your_user")),
        "password": _env("POSTGRES_PASSWORD", _env("PGPASSWORD", "your_password")),
        "host": host,
        "port": int(_env("POSTGRES_PORT", _env("PGPORT", "5432"))),
    }

    sslmode = os.getenv("POSTGRES_SSLMODE") or os.getenv("PGSSLMODE")
    ssl_domain = os.getenv("POSTGRES_SSL_DOMAIN", "neon.tech")
    if not sslmode and ssl_domain and ssl_domain in host:
        sslmode = "require"

    if sslmode:
        kwargs["sslmode"] = sslmode

    return kwargs



def get_connection():
    # A short connect timeout keeps an unreachable database from hanging a run.
    timeout = int(_env("POSTGRES_CONNECT_TIMEOUT", "10"))
    return psycopg2.connect(**_connection_kwargs(), connect_timeout=timeout)


@contextmanager
def db_connection():
    """Connection that commits/rolls back like ``with conn`` but also closes.

    psycopg2's ``with conn`` only ends the transaction; without an explicit
    close every serverless request leaks a TLS connection to the database.
    """
    conn = get_connection()
    try:
        with conn:
            yield conn
    finally:
        conn.close()



def ensure_schema() -> None:
    """Apply schema.sql (fully idempotent) so fresh databases just work."""
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(sql)


# How long a grant may be missing from the extract before it counts as
# withdrawn. Measured from the newest load, not from today, so a few days of
# failed downloads do not hide everything.
_STALE_AFTER = "3 days"


def listed_sql(alias: str = "g") -> str:
    """SQL condition for a grant that was in a recent extract."""
    return (
        f"({alias}.last_seen_at IS NULL OR {alias}.last_seen_at >="
        f" (SELECT MAX(last_seen_at) FROM grants) - INTERVAL '{_STALE_AFTER}')"
    )


def open_grant_sql(alias: str = "g") -> str:
    """SQL condition for a grant someone can still apply to.

    Not past its close date, not archived (Grants.gov stops taking
    applications at the archive date even when the close date is later), and
    still present in a recent extract. Every query that lists open grants uses
    this so the dashboard, MCP server and email agree.
    """
    a = alias
    return (
        f"({a}.close_date IS NULL OR {a}.close_date >= CURRENT_DATE)"
        f" AND ({a}.archive_date IS NULL OR {a}.archive_date >= CURRENT_DATE)"
        f" AND {listed_sql(a)}"
    )


def fetch_relevance(key: str) -> Dict[str, Dict[str, Any]]:
    """Scores already stored under this score key (``relevance.score_key``), keyed by opportunity number."""
    query = """
        SELECT opp_id, relevance_score, relevance_reason, summary, relevance_model, relevance_scored_at
        FROM grants
        WHERE relevance_profile = %s AND relevance_score IS NOT NULL;
    """
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, (key,))
        rows = cur.fetchall()
    return {
        opp_id: {
            "score": score,
            "reason": reason or "",
            "summary": summary or "",
            "model": model or "",
            "scored_at": scored_at.isoformat() if scored_at else None,
        }
        for opp_id, score, reason, summary, model, scored_at in rows
    }


def fetch_upcoming(
    days: int = 30,
    *,
    min_score: Optional[int] = None,
    include_forecasted: bool = True,
    include_watched: bool = True,
    stage: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Grants closing within ``days`` that clear ``min_score`` or are on the watchlist."""
    conditions = [
        open_grant_sql("g"),
        "g.close_date IS NOT NULL",
        "g.close_date <= %s",  # inclusive: a grant closing exactly `days` out is within `days`
    ]
    params: List[Any] = [date.today() + timedelta(days=int(days))]

    if not include_forecasted:
        conditions.append("g.opportunity_status <> 'Forecasted'")
    if stage:
        conditions.append("g.stage = %s")
        params.append(stage)
    if min_score is not None:
        if include_watched:
            conditions.append("(g.relevance_score >= %s OR w.opp_id IS NOT NULL)")
        else:
            conditions.append("g.relevance_score >= %s")
        params.append(int(min_score))

    query = f"""
        SELECT g.opp_id, g.title, g.stage, g.agency, g.agency_code, g.url,
               g.opportunity_status, g.post_date, g.close_date,
               g.relevance_score, g.relevance_reason, g.summary,
               g.first_seen_at, (w.opp_id IS NOT NULL) AS watched, w.note AS watch_note
        FROM grants g
        LEFT JOIN grant_watchlist w ON w.opp_id = g.opp_id
        WHERE {" AND ".join(conditions)}
        ORDER BY g.close_date, g.relevance_score DESC NULLS LAST, g.title
        LIMIT %s;
    """
    params.append(int(limit))

    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def add_to_watchlist(opp_id: str, note: Optional[str] = None) -> bool:
    key = (opp_id or "").strip()
    if not key:
        raise ValueError("opp_id is required")
    query = """
        INSERT INTO grant_watchlist (opp_id, note)
        VALUES (%s, %s)
        ON CONFLICT (opp_id) DO UPDATE SET note = COALESCE(EXCLUDED.note, grant_watchlist.note);
    """
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, (key, (note or "").strip() or None))
        return cur.rowcount > 0


def remove_from_watchlist(opp_id: str) -> bool:
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM grant_watchlist WHERE opp_id = %s;", ((opp_id or "").strip(),))
        return cur.rowcount > 0


def list_watchlist() -> List[Dict[str, Any]]:
    query = """
        SELECT w.opp_id, w.note, w.created_at, g.title, g.agency, g.url,
               g.close_date, g.opportunity_status, g.relevance_score
        FROM grant_watchlist w
        LEFT JOIN grants g ON g.opp_id = w.opp_id
        ORDER BY g.close_date NULLS LAST, w.created_at;
    """
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query)
        return [dict(row) for row in cur.fetchall()]


# The grants that share a close date with one grant: every posting it could be folded with.
_PROGRAM_SQL = """
    SELECT opp_id, title, agency_code, close_date::date
    FROM grants
    WHERE close_date::date = (SELECT close_date::date FROM grants WHERE opp_id = %s);
"""


def _program_opp_ids(cur: Any, opp_id: str) -> List[str]:
    """``opp_id`` and the other postings of its program, whatever filter the person was looking through.

    Read from every grant, not from the rows the dashboard showed, so a search or a score
    floor that hides a sibling does not leave that sibling's vote behind.
    """
    cur.execute(_PROGRAM_SQL, (opp_id,))
    postings = [Posting(opp_id=o, title=t or "", agency_code=a or "", close=c) for o, t, a, c in cur.fetchall()]
    return list(program_of(opp_id, postings))


def record_vote(opp_id: str, email: str, verdict: int) -> bool:
    """Store ``email``'s verdict (1 or -1) on a grant, replacing their earlier one; False when there is no such grant.

    A person has one vote per program, so their votes on the program's other postings
    are removed in the same transaction. The title, agency and summary are copied from
    the grants row, cleaned, so the vote can still serve as an example after the grant
    leaves the extract.
    """
    query = """
        INSERT INTO grant_feedback (opp_id, email, verdict, title, agency, summary)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (opp_id, email) DO UPDATE SET verdict = EXCLUDED.verdict, created_at = NOW();
    """
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT title, agency, summary FROM grants WHERE opp_id = %s;", (opp_id,))
        row = cur.fetchone()
        if row is None:
            return False
        title, agency, summary = (clean_text(value) or None for value in row)
        others = [other for other in _program_opp_ids(cur, opp_id) if other != opp_id]
        if others:
            cur.execute("DELETE FROM grant_feedback WHERE email = %s AND opp_id = ANY(%s);", (email, others))
        cur.execute(query, (opp_id, email, verdict, title, agency, summary))
    return True


def clear_vote(opp_id: str, email: str) -> bool:
    """Take back ``email``'s own vote on a grant's program, whichever posting it sits on; False when they had none."""
    with db_connection() as conn, conn.cursor() as cur:
        ids = _program_opp_ids(cur, opp_id)
        cur.execute("DELETE FROM grant_feedback WHERE email = %s AND opp_id = ANY(%s);", (email, ids))
        return cur.rowcount > 0


def fetch_votes() -> List[Dict[str, Any]]:
    """Every vote, with the grant's title, agency and summary as they were when it was cast."""
    query = "SELECT opp_id, email, verdict, title, agency, summary, created_at FROM grant_feedback;"
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query)
        return [dict(row) for row in cur.fetchall()]


def fetch_vote_counts(opp_ids: Sequence[str], email: Optional[str] = None) -> Dict[str, Votes]:
    """The votes on these grants, and ``email``'s own verdict on each; a grant nobody voted on is left out."""
    if not opp_ids:
        return {}
    query = """
        SELECT opp_id,
               COUNT(*) FILTER (WHERE verdict = 1),
               COUNT(*) FILTER (WHERE verdict = -1),
               MAX(verdict) FILTER (WHERE email = %s)
        FROM grant_feedback
        WHERE opp_id = ANY(%s)
        GROUP BY opp_id;
    """
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, (email, list(opp_ids)))
        return {opp_id: Votes(int(up), int(down), mine) for opp_id, up, down, mine in cur.fetchall()}


# The grants that could be folded with a posting: those that share its close date, and the posting
# itself, which is a program of one when it has no date.
_PROGRAMS_OF_SQL = """
    SELECT opp_id, title, agency_code, close_date::date
    FROM grants
    WHERE opp_id = ANY(%s)
       OR close_date::date IN (SELECT close_date::date FROM grants WHERE opp_id = ANY(%s));
"""


def fetch_relevant_ids() -> List[str]:
    """Every posting of each program the team marked Relevant on balance (see ``relevant_postings``).

    A program's votes are summed over all its postings, so a Not for us on a sibling counts.
    Raises ``UndefinedTable`` until the first run has created grant_feedback.
    """
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT opp_id, SUM(verdict)::int FROM grant_feedback GROUP BY opp_id;")
        nets = {opp_id: int(net) for opp_id, net in cur.fetchall()}
        liked = [opp_id for opp_id, net in nets.items() if net > 0]
        if not liked:
            return []  # a program with no posting on the plus side cannot come out ahead
        cur.execute(_PROGRAMS_OF_SQL, (liked, liked))
        postings = [Posting(opp_id=o, title=t or "", agency_code=a or "", close=c) for o, t, a, c in cur.fetchall()]
    return relevant_postings(postings, nets)
