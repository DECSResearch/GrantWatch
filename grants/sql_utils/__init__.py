import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

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


def _normalise(value: str) -> str:
    return value.strip().lower()



def available_subscription_fields(limit: int = 200) -> List[Tuple[str, str]]:
    query = """
        WITH funding AS (
            SELECT TRIM(value) AS label
            FROM (
                SELECT unnest(string_to_array(funding_categories, ';')) AS value
                FROM grants
                WHERE funding_categories IS NOT NULL
            ) expanded
            WHERE TRIM(value) <> ''
        ),
        categories AS (
            SELECT TRIM(opportunity_category) AS label
            FROM grants
            WHERE opportunity_category IS NOT NULL
        ),
        merged AS (
            SELECT label FROM funding
            UNION ALL
            SELECT label FROM categories
        ),
        prepared AS (
            SELECT LOWER(label) AS field_key, label
            FROM merged
            WHERE label <> ''
        )
        SELECT field_key, MIN(label) AS display_label
        FROM prepared
        GROUP BY field_key
        ORDER BY display_label
        LIMIT %s;
    """

    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, (limit,))
        return [(row[0], row[1]) for row in cur.fetchall()]



def add_subscription(email: str, field: str) -> bool:
    email_clean = _normalise(email)
    field_clean = _normalise(field)
    if not email_clean or '@' not in email_clean or not field_clean:
        raise ValueError("email and field are required")

    query = """
        INSERT INTO grant_subscriptions (email, field)
        VALUES (%s, %s)
        ON CONFLICT (email, field)
        DO UPDATE SET created_at = NOW();
    """

    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, (email_clean, field_clean))
        return cur.rowcount > 0



def get_subscribers_for_fields(fields: Iterable[str]) -> Dict[str, List[str]]:
    normalised = {_normalise(field) for field in fields if field and field.strip()}
    if not normalised:
        return {}

    query = """
        SELECT field, email
        FROM grant_subscriptions
        WHERE field = ANY(%s);
    """

    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, (list(normalised),))
        rows = cur.fetchall()

    subscribers: Dict[str, List[str]] = {}
    for field, email in rows:
        subscribers.setdefault(field, []).append(email)
    return subscribers



def fetch_relevance(profile_fingerprint: str) -> Dict[str, Dict[str, Any]]:
    """Scores already stored for this profile, keyed by opportunity number."""
    query = """
        SELECT opp_id, relevance_score, relevance_reason, summary, relevance_model, relevance_scored_at
        FROM grants
        WHERE relevance_profile = %s AND relevance_score IS NOT NULL;
    """
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, (profile_fingerprint,))
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
        "g.close_date >= CURRENT_DATE",
        "g.close_date < CURRENT_DATE + (%s || ' days')::interval",
    ]
    params: List[Any] = [int(days)]

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
