import psycopg2
from psycopg2.extras import RealDictCursor

def get_connection():
    return psycopg2.connect(
        dbname="your_db",
        user="your_user",
        password="your_password",
        host="localhost",
        port=5432
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
