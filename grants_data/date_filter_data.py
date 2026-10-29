"""Date-based filtering for grants records."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List

from logs.status_logger import logger

_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _passed(record: Dict[str, object], field: str, today: date) -> bool:
    raw = record.get(field)
    parsed = _parse_date(str(raw)) if raw not in (None, "") else None
    return parsed is not None and parsed.date() < today


def drop_closed_grants(records: List[Dict[str, object]], today: date | None = None) -> List[Dict[str, object]]:
    """Remove grants nobody can apply to any more; keep those with no dates.

    A grant is closed once its close date has passed, and also once its
    archive date has: Grants.gov stops taking applications at the archive
    date even when the close date is later, which is true of most old
    records whose close date looks open.
    """
    today = today or datetime.now(timezone.utc).date()
    kept: List[Dict[str, object]] = []
    closed = archived = 0
    for record in records:
        if _passed(record, "CLOSE_DATE", today):
            closed += 1
        elif _passed(record, "ARCHIVE_DATE", today):
            archived += 1
        else:
            kept.append(record)
    if closed or archived:
        logger("info", f"Dropped {closed} grant(s) past their deadline and {archived} already archived")
    return kept


def date_filter_json_data(records: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Keep grants posted within GRANTS_GOV_LOOKBACK_DAYS, when that is set.

    Off by default: long-running solicitations (most of NSF) were posted
    years ago and are still open, so a posted-date window hides them and
    stops refreshing their deadlines. drop_closed_grants decides what is open.
    """
    raw = (os.getenv("GRANTS_GOV_LOOKBACK_DAYS") or "").strip()
    try:
        lookback_days = int(raw) if raw else 0
    except ValueError:
        logger("warning", f"Invalid GRANTS_GOV_LOOKBACK_DAYS '{raw}'; keeping every posting date")
        lookback_days = 0
    if lookback_days <= 0:
        logger("info", f"No posted-date window; keeping all {len(records)} records for the open-grant filter")
        return list(records)
    # Naive UTC so it stays comparable with the naive parsed record dates.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=lookback_days)

    filtered: List[Dict[str, object]] = []
    unparseable = 0
    example: object = None
    for record in records:
        raw_posted = record.get("POSTED_DATE")
        if raw_posted in (None, ""):
            continue
        posted = _parse_date(str(raw_posted))
        if posted is None:
            unparseable += 1
            if example is None:
                example = raw_posted
            continue
        if posted >= cutoff:
            filtered.append(record)

    # One aggregated line instead of a warning per record: a single bad
    # extract used to add hundreds of identical lines to the log.
    if unparseable:
        logger(
            "warning",
            f"Skipped {unparseable} record(s) with unparseable POSTED_DATE (e.g. '{example}')",
        )
    logger(
        "info",
        f"Filtered grants by date: kept {len(filtered)} of {len(records)} within {lookback_days} days"
    )
    return filtered
