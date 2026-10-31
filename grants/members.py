"""A team member's own research description, and the personal profile scored from it.

A member without a row in ``member_profiles`` is scored against the team profile.
A member with one is scored against the team profile with its summary replaced
(``personal_profile``): institution, applicant type, agency exclusions and horizons
stay the team's. Their scores live in ``member_scores``, never on ``grants``.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Optional

from grants.feedback import clean_text
from grants.profile import Profile
from grants.sql_utils import db_connection

MIN_SUMMARY_CHARS = 40
MAX_SUMMARY_CHARS = 2000


class ProfileError(ValueError):
    """A description that cannot be stored; the message says why, in words for the person who wrote it."""


def clean_summary(value: Any) -> str:
    """``value`` as one line of clean text of 40 to 2,000 characters, else ``ProfileError``.

    It goes into a model prompt, so control and format characters are dropped and whitespace
    is folded (``clean_text``). Too short or too long is refused, not cut: half a description
    would be scored as if it were the whole. The placeholder text of the team's template would
    make ``Profile.is_configured`` false and silently turn scoring off, so it is refused too.
    """
    text = clean_text(value)
    if text and not Profile(summary=text).is_configured:  # the template's "REPLACE ME"
        raise ProfileError("Replace the placeholder with a description of your own research.")
    if len(text) < MIN_SUMMARY_CHARS:
        raise ProfileError(f"Write at least {MIN_SUMMARY_CHARS} characters about what you work on.")
    if len(text) > MAX_SUMMARY_CHARS:
        raise ProfileError(f"Keep it to {MAX_SUMMARY_CHARS:,} characters or fewer; this is {len(text):,}.")
    return text


def personal_profile(team: Profile, summary: str) -> Profile:
    """The team profile with ``summary`` in place of the team's own; ``team`` is not changed."""
    return replace(team, summary=summary)


def fetch_member_profile(email: str) -> Optional[Dict[str, Any]]:
    """``{"summary", "updated_at"}`` of this member's own profile, or None when they use the team's."""
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT summary, updated_at FROM member_profiles WHERE email = %s;", (email,))
        row = cur.fetchone()
    return {"summary": row[0], "updated_at": row[1]} if row else None


def save_member_profile(email: str, summary: str) -> str:
    """Store this member's own description and return the cleaned text that was stored.

    ``ProfileError`` when it cannot be stored. When the text changes, their scores are deleted in
    the same transaction: they were for the old text, and the next run scores the new one. The
    same text again changes nothing. ``email`` must be a recipient's (a foreign key; psycopg2's
    IntegrityError otherwise).
    """
    text = clean_summary(summary)
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT summary FROM member_profiles WHERE email = %s FOR UPDATE;", (email,))
        row = cur.fetchone()
        if row and row[0] == text:
            return text
        cur.execute(
            """
            INSERT INTO member_profiles (email, summary) VALUES (%s, %s)
            ON CONFLICT (email) DO UPDATE SET summary = EXCLUDED.summary, updated_at = NOW();
            """,
            (email, text),
        )
        cur.execute("DELETE FROM member_scores WHERE email = %s;", (email,))
    return text


def delete_member_profile(email: str) -> bool:
    """Back to the team profile: the profile goes, and its scores with it (foreign key). False when there was none."""
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM member_profiles WHERE email = %s;", (email,))
        return cur.rowcount > 0
