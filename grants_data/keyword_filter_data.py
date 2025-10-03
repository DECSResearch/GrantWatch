"""Keyword filtering helpers for grants records."""
from __future__ import annotations

from typing import Dict, Iterable, List

from logs.status_logger import logger


def filter_grants_by_keywords(
    records: List[Dict[str, object]],
    field: str,
    keywords: Iterable[str],
    threshold: int,
) -> List[Dict[str, object]]:
    """Keep grants where ``threshold`` or more keywords appear in ``field``."""
    keyword_map = {keyword.lower(): keyword for keyword in keywords}
    filtered: List[Dict[str, object]] = []

    for record in records:
        haystack = str(record.get(field, "")).lower()
        matches = [original for lowered, original in keyword_map.items() if lowered in haystack]
        if len(matches) >= threshold:
            enriched = dict(record)
            enriched["MATCHED_KEYWORDS"] = matches
            filtered.append(enriched)

    logger(
        "info",
        f"Keyword filtering on {field}: kept {len(filtered)} of {len(records)} records"
    )
    return filtered
