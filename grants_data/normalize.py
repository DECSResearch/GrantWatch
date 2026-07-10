"""Normalize raw grant records into the canonical pipeline schema.

The search_export JSON endpoint and the XML extract use different field
names. Downstream code (CSV writer, DB loader, notifier) expects the
canonical keys AGENCY, OPPORTUNITY_URL, FUNDING_CATEGORIES, and
OPPORTUNITY_CATEGORY; the export instead sends AGENCY_NAME,
OPPORTUNITY_NUMBER_LINK, and CATEGORY_OF_FUNDING_ACTIVITY. Without this
mapping those CSV columns stay empty and, because funding_categories is
always NULL in the database, subscriber notifications never fire.
"""
from __future__ import annotations

import html
import re
from typing import Dict, List

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# canonical key -> fallback keys tried in order when the canonical key is empty
_KEY_FALLBACKS = {
    "AGENCY": ("AGENCY_NAME", "AGENCY_CODE"),
    "OPPORTUNITY_URL": ("OPPORTUNITY_NUMBER_LINK", "LINK_TO_ADDITIONAL_INFORMATION"),
    "FUNDING_CATEGORIES": ("CATEGORY_OF_FUNDING_ACTIVITY",),
}


def strip_html(value: object) -> str:
    """Return plain text: tags removed, entities decoded, whitespace collapsed."""
    if value in (None, ""):
        return ""
    text = _TAG_RE.sub(" ", str(value))
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _is_empty(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def normalize_records(records: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """Fill canonical keys from source-specific aliases; leaves originals intact."""
    normalized: List[Dict[str, object]] = []
    for record in records:
        merged = dict(record)
        for canonical, fallbacks in _KEY_FALLBACKS.items():
            if not _is_empty(merged.get(canonical)):
                continue
            for fallback in fallbacks:
                value = merged.get(fallback)
                if not _is_empty(value):
                    merged[canonical] = value
                    break
        normalized.append(merged)
    return normalized
