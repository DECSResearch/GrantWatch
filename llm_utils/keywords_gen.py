"""Utility to derive keyword filters for grants."""
from __future__ import annotations

import os
from typing import List, Tuple

from logs.status_logger import logger

_DEFAULT_KEYWORDS = [
    "research",
    "education",
    "innovation",
    "technology",
    "infrastructure",
]


def _parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def keyword_extractor() -> Tuple[List[str], int, bool]:
    """Return configured keywords, match threshold, and forecast flag."""
    raw_keywords = os.getenv("GRANTS_KEYWORDS")
    if raw_keywords:
        keywords = [keyword.strip() for keyword in raw_keywords.split(",") if keyword.strip()]
        if not keywords:
            keywords = _DEFAULT_KEYWORDS
            logger("warning", "GRANTS_KEYWORDS env var provided but no valid keywords parsed; using defaults")
    else:
        keywords = _DEFAULT_KEYWORDS

    threshold_env = os.getenv("GRANTS_KEYWORD_THRESHOLD")
    try:
        threshold = int(threshold_env) if threshold_env else 1
    except ValueError:
        threshold = 1
        logger("warning", f"Invalid GRANTS_KEYWORD_THRESHOLD '{threshold_env}'; defaulting to 1")

    forecast = _parse_bool(os.getenv("GRANTS_INCLUDE_FORECAST"))

    logger(
        "info",
        "Keyword configuration -> keywords=%s threshold=%s forecast=%s"
        % (", ".join(keywords), threshold, forecast),
    )

    return keywords, threshold, forecast
