"""Postgres access for recipients and the record of what each one was told."""
from __future__ import annotations

import secrets
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from psycopg2.extras import RealDictCursor, execute_values

from grants.sql_utils import db_connection, ensure_schema, listed_sql, open_grant_sql
from notifications.planner import GrantState, Recipient, SentRecord

_RECIPIENT_COLUMNS = "id, email, name, min_score, unsubscribe_token, active"


def normalise_email(value: str) -> str:
    email = (value or "").strip().lower()
    if not email or "@" not in email or " " in email:
        raise ValueError(f"'{value}' is not an email address")
    return email


def _recipient(row: Dict[str, Any]) -> Recipient:
    return Recipient(
        id=int(row["id"]),
        email=row["email"],
        name=row.get("name") or "",
        min_score=row.get("min_score"),
        unsubscribe_token=row.get("unsubscribe_token") or "",
    )


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def add_recipient(email: str, name: Optional[str] = None, min_score: Optional[int] = None) -> Recipient:
    """Add someone, or turn them back on if they had unsubscribed (an explicit admin action)."""
    query = f"""
        INSERT INTO recipients (email, name, min_score, unsubscribe_token)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (email) DO UPDATE SET
            active = TRUE,
            unsubscribed_at = NULL,
            name = COALESCE(EXCLUDED.name, recipients.name),
            min_score = COALESCE(EXCLUDED.min_score, recipients.min_score)
        RETURNING {_RECIPIENT_COLUMNS};
    """
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (normalise_email(email), name or None, min_score, secrets.token_urlsafe(24)))
        return _recipient(dict(cur.fetchone()))


def remove_recipient(email: str) -> bool:
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM recipients WHERE email = %s;", (normalise_email(email),))
        return cur.rowcount > 0


def list_recipients(active_only: bool = True) -> List[Dict[str, Any]]:
    where = "WHERE active" if active_only else ""
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT {_RECIPIENT_COLUMNS}, created_at, unsubscribed_at FROM recipients {where} ORDER BY email;")
        return [dict(row) for row in cur.fetchall()]


def active_recipient(email: str) -> Optional[Recipient]:
    try:
        key = normalise_email(email)
    except ValueError:
        return None
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT {_RECIPIENT_COLUMNS} FROM recipients WHERE email = %s AND active;", (key,))
        row = cur.fetchone()
    return _recipient(dict(row)) if row else None


def recipient_for_token(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT email, active FROM recipients WHERE unsubscribe_token = %s;", (token,))
        row = cur.fetchone()
    return dict(row) if row else None


def unsubscribe(token: str) -> Optional[str]:
    """Turn off the recipient holding ``token``; returns their address."""
    if not token:
        return None
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE recipients SET active = FALSE, unsubscribed_at = COALESCE(unsubscribed_at, NOW())
            WHERE unsubscribe_token = %s RETURNING email;
            """,
            (token,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def claim_login_email(email: str, cooldown_seconds: int = 60) -> Optional[Recipient]:
    """The active recipient for ``email`` if a sign-in link may be sent now, else None."""
    try:
        key = normalise_email(email)
    except ValueError:
        return None
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            UPDATE recipients SET login_sent_at = NOW()
            WHERE email = %s AND active
              AND (login_sent_at IS NULL OR login_sent_at < NOW() - (%s || ' seconds')::interval)
            RETURNING {_RECIPIENT_COLUMNS};
            """,
            (key, int(cooldown_seconds)),
        )
        row = cur.fetchone()
    return _recipient(dict(row)) if row else None


class PostgresStore:
    """What the notify step needs from the database (tests use an in-memory twin)."""

    def ensure_schema(self) -> None:
        ensure_schema()

    def sync_recipients(self, emails: Iterable[str]) -> int:
        """Add addresses from the environment; never re-activates someone who unsubscribed."""
        cleaned = sorted({normalise_email(e) for e in emails if e and e.strip()})
        if not cleaned:
            return 0
        with db_connection() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO recipients (email, unsubscribe_token) VALUES %s ON CONFLICT (email) DO NOTHING;",
                [(email, secrets.token_urlsafe(24)) for email in cleaned],
            )
            return cur.rowcount

    def recipients(self) -> List[Recipient]:
        return [_recipient(row) for row in list_recipients(active_only=True)]

    def grants(self) -> List[GrantState]:
        # Tracked grants are loaded even when closed off early, so the planner
        # can say they were withdrawn.
        query = f"""
            SELECT g.opp_id, g.title, g.agency, g.agency_code, g.url, g.close_date, g.archive_date,
                   g.opportunity_status, g.relevance_score, g.relevance_reason, g.summary,
                   g.matched_keywords,
                   (w.opp_id IS NOT NULL) AS watched, {listed_sql("g")} AS listed
            FROM grants g
            LEFT JOIN grant_watchlist w ON w.opp_id = g.opp_id
            WHERE ({open_grant_sql("g")}) OR w.opp_id IS NOT NULL;
        """
        with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            rows = cur.fetchall()
        return [
            GrantState(
                opp_id=row["opp_id"],
                title=row["title"] or "Untitled",
                agency=row["agency"] or "",
                agency_code=row["agency_code"] or "",
                url=row["url"] or "",
                close_date=_as_date(row["close_date"]),
                archive_date=_as_date(row["archive_date"]),
                forecasted=str(row["opportunity_status"] or "").lower() == "forecasted",
                score=row["relevance_score"],
                reason=row["relevance_reason"] or "",
                summary=row["summary"] or "",
                watched=bool(row["watched"]),
                listed=bool(row["listed"]),
                matches=len([k for k in (row["matched_keywords"] or "").split(";") if k.strip()]),
            )
            for row in rows
        ]

    def sent(self, recipient_ids: Sequence[int]) -> List[SentRecord]:
        if not recipient_ids:
            return []
        with db_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT recipient_id, opp_id, kind, threshold, ref_date, score
                FROM notifications WHERE recipient_id = ANY(%s)
                ORDER BY sent_at, id;
                """,
                (list(recipient_ids),),
            )
            return [SentRecord(r[0], r[1], r[2], r[3], r[4], r[5]) for r in cur.fetchall()]

    def record(self, rows: Sequence[SentRecord]) -> int:
        if not rows:
            return 0
        with db_connection() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO notifications (recipient_id, opp_id, kind, threshold, ref_date, score)
                VALUES %s ON CONFLICT DO NOTHING;
                """,
                [(r.recipient_id, r.opp_id, r.kind, r.threshold, r.ref_date, r.score) for r in rows],
            )
            return cur.rowcount

    def acquire_lock(self, minutes: int = 30) -> bool:
        with db_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notify_lock (id, locked_until) VALUES (1, NOW() + (%s || ' minutes')::interval)
                ON CONFLICT (id) DO UPDATE SET locked_until = EXCLUDED.locked_until
                WHERE notify_lock.locked_until < NOW()
                RETURNING id;
                """,
                (int(minutes),),
            )
            return cur.fetchone() is not None

    def release_lock(self) -> None:
        with db_connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE notify_lock SET locked_until = NOW() - INTERVAL '1 second' WHERE id = 1;")
