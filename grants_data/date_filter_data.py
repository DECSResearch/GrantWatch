"""Date-based filtering for grants records."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
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


def date_filter_json_data(records: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Keep grants posted within the configured lookback window."""
    try:
        lookback_days = int((os.getenv("GRANTS_GOV_LOOKBACK_DAYS") or "90").strip())
    except ValueError:
        logger("warning", "Invalid GRANTS_GOV_LOOKBACK_DAYS; defaulting to 90")
        lookback_days = 90
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
