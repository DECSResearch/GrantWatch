"""Rules for showing Grants.gov data without passing off placeholders as facts.

Pure functions over plain data (no database, no web framework), so the API,
the email planner and the tests share one definition of each rule.

Grants.gov fills fields it has no answer for with far-future dates (a close
date in 2076) and 999,999,999 for an award ceiling. Both look like facts.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

# A close date further out than this is a placeholder, not a deadline.
FIRM_DEADLINE_DAYS = 5 * 365
# A day count further out than this is left out of the email; the date says enough.
COUNTDOWN_DAYS = 365
# An award ceiling at or above this is a placeholder, not an amount.
PLACEHOLDER_CEILING = 999_999_999


def days_until_deadline(close: Optional[date], today: date) -> Optional[int]:
    """Days to a firm deadline, or None when there is none (no date, or a placeholder)."""
    if close is None:
        return None
    days = (close - today).days
    return days if days <= FIRM_DEADLINE_DAYS else None


def firm_deadline(close: Optional[date], today: date) -> bool:
    return days_until_deadline(close, today) is not None


def real_ceiling(value: Optional[float]) -> Optional[float]:
    """The award ceiling, or None when it is missing or the Grants.gov placeholder."""
    if value is None or value >= PLACEHOLDER_CEILING:
        return None
    return value
