import os
from typing import Any, Dict

import psycopg2
from psycopg2.extras import RealDictCursor


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value else default


def _connection_kwargs() -> Dict[str, Any]:
    url = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
    if url:
        return {"dsn": url}

    host = _env("POSTGRES_HOST", "localhost")
    kwargs: Dict[str, Any] = {
        "dbname": _env("POSTGRES_DB", "your_db"),
        "user": _env("POSTGRES_USER", "your_user"),
        "password": _env("POSTGRES_PASSWORD", "your_password"),
        "host": host,
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
    }

    sslmode = os.getenv("POSTGRES_SSLMODE")
    if not sslmode and "neon.tech" in host:
        sslmode = "require"

    if sslmode:
        kwargs["sslmode"] = sslmode

    return kwargs


def get_connection():
    return psycopg2.connect(**_connection_kwargs())


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
