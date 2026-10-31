"""Fallback summariser for grant descriptions.

The relevance scorer writes a model-generated ``SUMMARY``; this fills the
field for records that were not scored, so every row has something readable.
"""
from __future__ import annotations

from typing import Dict, List

from grants_data.normalize import strip_html
from logs.status_logger import logger

SUMMARY_CHARS = 320


def cut_summary(plain: str) -> str:
    """``plain`` text (tags already removed) on one line, cut to the length of a fallback summary.

    The public grant list uses this on the stored description, which is already plain text, so
    it reads the same words the fallback below would have put in ``grants.summary``.
    """
    cleaned = " ".join(plain.split())
    if len(cleaned) <= SUMMARY_CHARS:
        return cleaned
    return f"{cleaned[:SUMMARY_CHARS].rstrip()}..."


def _summarise_text(text: str) -> str:
    return cut_summary(strip_html(text))


def description_summarizer(records: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Attach a short summary to each record that does not already have one."""
    if records is None:
        logger("error", "No records provided for summarisation")
        return []

    summarised = []
    filled = 0
    for record in records:
        enriched = dict(record)
        if not str(enriched.get("SUMMARY") or "").strip():
            enriched["SUMMARY"] = _summarise_text(str(record.get("FUNDING_DESCRIPTION", "")))
            filled += 1
        summarised.append(enriched)

    logger("info", f"Generated summaries for {len(summarised)} records ({filled} truncated fallbacks)")
    return summarised
