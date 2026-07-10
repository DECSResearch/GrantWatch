"""Keyword filtering helpers for grants records."""
from __future__ import annotations

import re
from typing import Dict, Iterable, List

from grants_data.normalize import strip_html
from logs.status_logger import logger


def filter_grants_by_keywords(
    records: List[Dict[str, object]],
    field: str,
    keywords: Iterable[str],
    threshold: int,
) -> List[Dict[str, object]]:
    """Keep grants where ``threshold`` or more keywords appear in ``field``.

    Matches on word boundaries so short keywords do not fire inside longer
    words (e.g. "art" no longer matches "particular").
    """
    keyword_patterns = {
        keyword: re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE)
        for keyword in keywords
    }
    filtered: List[Dict[str, object]] = []

    for record in records:
        haystack = strip_html(record.get(field, ""))
        matches = [keyword for keyword, pattern in keyword_patterns.items() if pattern.search(haystack)]
        if len(matches) >= threshold:
            enriched = dict(record)
            enriched["MATCHED_KEYWORDS"] = matches
            filtered.append(enriched)

    logger(
        "info",
        f"Keyword filtering on {field}: kept {len(filtered)} of {len(records)} records"
    )
    return filtered
