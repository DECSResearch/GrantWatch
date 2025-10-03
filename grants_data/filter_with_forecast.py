"""Helpers to remove forecasted opportunities."""
from __future__ import annotations

from typing import Dict, List

from logs.status_logger import logger


def filter_forecasted_data(records: List[Dict[str, object]]) -> List[Dict[str, object]]:
    filtered = [record for record in records if str(record.get("OPPORTUNITY_STATUS", "")).lower() != "forecasted"]
    logger(
        "info",
        f"Removed forecasted opportunities: kept {len(filtered)} of {len(records)} records"
    )
    return filtered
