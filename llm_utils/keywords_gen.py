"""Keyword filter settings, sourced from the research profile."""
from __future__ import annotations

from typing import List, Tuple

from grants.profile import Profile, load_profile
from logs.status_logger import logger


def keyword_extractor(profile: Profile | None = None) -> Tuple[List[str], int, bool]:
    """Return (keywords, threshold, include_forecasted) for the pipeline.

    Values come from ``config/research_profile.yml``; the ``GRANTS_KEYWORDS``,
    ``GRANTS_KEYWORD_THRESHOLD`` and ``GRANTS_INCLUDE_FORECAST`` environment
    variables still override them (see ``grants.profile.load_profile``).
    """
    profile = profile or load_profile()
    logger(
        "info",
        "Keyword configuration -> keywords=%s threshold=%s forecast=%s"
        % (", ".join(profile.keywords), profile.keyword_threshold, profile.include_forecasted),
    )
    return list(profile.keywords), profile.keyword_threshold, profile.include_forecasted
