"""Date-based filtering for grants records."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
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
    logger("warning", f"Unable to parse date '{value}'")
    return None


def date_filter_json_data(records: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Keep grants posted within the configured lookback window."""
    lookback_days = int(os.getenv("GRANTS_GOV_LOOKBACK_DAYS", "90"))
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)

    filtered: List[Dict[str, object]] = []
    for record in records:
        posted = _parse_date(str(record.get("POSTED_DATE", "")))
        if posted and posted >= cutoff:
            filtered.append(record)

    logger(
        "info",
        f"Filtered grants by date: kept {len(filtered)} of {len(records)} within {lookback_days} days"
    )
    return filtered
