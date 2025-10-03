"""Fallback summariser for grant descriptions."""
from __future__ import annotations

from typing import Dict, List

from logs.status_logger import logger

_MAX_SUMMARY_LENGTH = 320


def _summarise_text(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= _MAX_SUMMARY_LENGTH:
        return cleaned
    return f"{cleaned[:_MAX_SUMMARY_LENGTH].rstrip()}..."


def description_summarizer(records: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Attach a short summary to each grant record."""
    if records is None:
        logger("error", "No records provided for summarisation")
        return []

    summarised = []
    for record in records:
        description = str(record.get("FUNDING_DESCRIPTION", ""))
        summary = _summarise_text(description)
        enriched = dict(record)
        enriched["SUMMARY"] = summary
        summarised.append(enriched)

    logger("info", f"Generated summaries for {len(summarised)} records")
    return summarised
