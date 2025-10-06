import os
from typing import Any, Dict, Iterable, List, Tuple, Optional

import psycopg2
from psycopg2.extras import RealDictCursor


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
    value = os.getenv(name)
    return value if value else default


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
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
        "port": int(os.getenv("POSTGRES_PORT") or os.getenv("PGPORT") or "5432"),
    }

    sslmode = os.getenv("POSTGRES_SSLMODE") or os.getenv("PGSSLMODE")
    ssl_domain = os.getenv("POSTGRES_SSL_DOMAIN", "neon.tech")
    if not sslmode and ssl_domain and ssl_domain in host:
        sslmode = "require"

    if sslmode:
        kwargs["sslmode"] = sslmode

    return kwargs



def get_connection():
    return psycopg2.connect(**_connection_kwargs())



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

    with get_connection() as conn, conn.cursor() as cur:
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

    with get_connection() as conn, conn.cursor() as cur:
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

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(query, (list(normalised),))
        rows = cur.fetchall()

    subscribers: Dict[str, List[str]] = {}
    for field, email in rows:
        subscribers.setdefault(field, []).append(email)
    return subscribers



def fetch_upcoming(stage=None, days=30):
    query = """
        SELECT opp_id, title, stage, close_date, opportunity_status
        FROM grants
        WHERE opportunity_status = 'Posted'
          AND close_date BETWEEN NOW() AND NOW() + %s::interval
    """
    params = [f'{days} days']

    if stage:
        query += " AND stage = %s"
        params.append(stage)

    query += " ORDER BY close_date;"

    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, params)
        return cur.fetchall()
