import os

import psycopg2
from psycopg2.extras import RealDictCursor


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value else default


def get_connection():
    return psycopg2.connect(
        dbname=_env("POSTGRES_DB", "your_db"),
        user=_env("POSTGRES_USER", "your_user"),
        password=_env("POSTGRES_PASSWORD", "your_password"),
        host=_env("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
    )


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
