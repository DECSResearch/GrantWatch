"""End-to-end grants data pipeline."""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from grants.data.loader import LoadSummary, load_grants_from_records
from grants.profile import Profile, load_profile
from grants.sql_utils import fetch_relevance

from grants_data.date_filter_data import date_filter_json_data
from grants_data.download_extract import gen_extract
from grants_data.download_json import gen_grants
from grants_data.filter_with_forecast import filter_forecasted_data
from grants_data.get_file_path import get_latest_file_path
from grants_data.get_json_data import process_json_data
from grants_data.normalize import normalize_records
from grants_data.parse_extract import process_extract_xml
from grants_data.retention import keep_limit, prune_old_files
from grants_data.keyword_filter_data import filter_grants_by_keywords
from llm_utils.gpt_summarizer import description_summarizer
from llm_utils.keywords_gen import keyword_extractor
from llm_utils.relevance import SCORE_FIELD, score_records
from llm_utils.relevance_cache import load_cached, save_cached
from logs.status_logger import logger

_CSV_DIR = Path(__file__).resolve().parent / "grants_csv_data"
_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
]

_CSV_FIELDS = [
    "OPPORTUNITY_NUMBER",
    "OPPORTUNITY_TITLE",
    "RELEVANCE_SCORE",
    "RELEVANCE_REASON",
    "OPPORTUNITY_STATUS",
    "POSTED_DATE",
    "CLOSE_DATE",
    "ARCHIVE_DATE",
    "AGENCY",
    "AGENCY_CODE",
    "OPPORTUNITY_CATEGORY",
    "FUNDING_CATEGORIES",
    "AWARD_CEILING",
    "ESTIMATED_TOTAL_FUNDING",
    "ASSISTANCE_LISTINGS",
    "OPPORTUNITY_URL",
    "MATCHED_KEYWORDS",
    "SUMMARY",
    "FUNDING_DESCRIPTION",
]


def _ensure_csv_dir() -> Path:
    _CSV_DIR.mkdir(parents=True, exist_ok=True)
    return _CSV_DIR


def _serialise_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "; ".join(_serialise_value(item) for item in value)
    return str(value)


def _write_csv(records: List[Dict[str, object]]) -> Path:
    destination_dir = _ensure_csv_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = destination_dir / f"grants_{timestamp}.csv"

    with destination.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow({field: _serialise_value(record.get(field)) for field in _CSV_FIELDS})

    logger("info", f"Wrote filtered grants to {destination}")
    prune_old_files(destination_dir, "grants_*.csv", keep_limit())
    return destination


def _parse_sort_date(value: object) -> datetime:
    if value in (None, ""):
        return datetime.max
    text = str(value)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.max


def _sort_key(record: Dict[str, object]) -> Tuple[datetime, int, str]:
    """Deadline first, then the best-scoring grants within the same day."""
    score = record.get(SCORE_FIELD)
    return (
        _parse_sort_date(record.get("CLOSE_DATE")),
        -(int(score) if score is not None else -1),
        str(record.get("OPPORTUNITY_TITLE", "")),
    )


def _load_source_records() -> List[Dict[str, object]]:
    """Fetch raw records from the configured source.

    ``GRANTS_DATA_SOURCE=extract`` downloads and parses the full daily XML
    database extract (every opportunity, no row cap); the default ``export``
    keeps the existing search_export JSON flow.
    """
    source = os.getenv("GRANTS_DATA_SOURCE", "export").strip().lower()

    if source == "extract":
        if not gen_extract():
            logger("error", "Failed to download the XML database extract.")
            return []
        return process_extract_xml(gen_extract.last_extract_path)

    if source != "export":
        logger("warning", f"Unknown GRANTS_DATA_SOURCE '{source}'; falling back to 'export'")

    if gen_grants():
        latest_file_path = getattr(gen_grants, "last_download_path", None)
        if latest_file_path is None:
            latest_file_path = get_latest_file_path()
    else:
        logger("error", "Failed to download grants data; looking for a cached export.")
        latest_file_path = get_latest_file_path()
        if latest_file_path is not None:
            logger("warning", f"Using cached export at {latest_file_path}")

    if latest_file_path is None:
        logger("error", "No latest file path found.")
        return []

    return process_json_data(latest_file_path)


def _drop_excluded_agencies(records: List[Dict[str, Any]], profile: Profile) -> List[Dict[str, Any]]:
    if not profile.agencies_exclude:
        return records
    kept = [r for r in records if not profile.excludes_agency(r.get("AGENCY_CODE"), r.get("AGENCY"))]
    logger("info", f"Agency exclusions: kept {len(kept)} of {len(records)} records")
    return kept


def _score_relevance(records: List[Dict[str, Any]], profile: Profile) -> List[Dict[str, Any]]:
    fingerprint = profile.fingerprint()
    cached = load_cached(fingerprint)
    try:
        cached.update(fetch_relevance(fingerprint))
    except Exception as exc:
        logger("warning", f"Could not read stored relevance scores from the database: {exc}")
    outcome = score_records(records, profile, already_scored=cached)
    if outcome.new_entries:
        save_cached(fingerprint, outcome.new_entries)
    return outcome.records


def _finish(success: bool, records: List[Dict[str, object]], csv_path: Optional[Path] = None) -> Tuple[bool, List[Dict[str, object]]]:
    onlyTheGoodStuff.last_csv_path = csv_path  # type: ignore[attr-defined]
    return success, records


def onlyTheGoodStuff() -> Tuple[bool, List[Dict[str, object]]]:
    onlyTheGoodStuff.last_new_ids = None  # type: ignore[attr-defined]
    profile = load_profile()
    onlyTheGoodStuff.last_profile = profile  # type: ignore[attr-defined]

    whole_json_data = normalize_records(_load_source_records())
    length_initial = len(whole_json_data)
    if length_initial == 0:
        logger("error", "Failed to process JSON data.")
        return _finish(False, [])

    date_sorted_data = date_filter_json_data(whole_json_data)
    if len(date_sorted_data) == 0:
        logger("warning", "No data found after date filtering.")
        return _finish(True, [])

    keywords, threshold, include_forecast = keyword_extractor(profile)

    if include_forecast:
        logger("info", "Keeping forecasted opportunities (estimated deadlines).")
        status_sorted_data = date_sorted_data
    else:
        logger("info", "Dropping forecasted opportunities (GRANTS_INCLUDE_FORECAST is off).")
        status_sorted_data = filter_forecasted_data(date_sorted_data)
        if len(status_sorted_data) == 0:
            logger("info", "No data found after status filtering.")
            return _finish(True, [])

    agency_filtered = _drop_excluded_agencies(status_sorted_data, profile)

    keyword_json_data = filter_grants_by_keywords(agency_filtered, "FUNDING_DESCRIPTION", keywords, threshold)
    if len(keyword_json_data) == 0:
        logger("warning", "No data found after keyword filtering.")
        return _finish(True, [])
    logger("info", f"Filtered keyword length: {len(keyword_json_data)}")

    scored_data = _score_relevance(keyword_json_data, profile)

    summarized_json_data = description_summarizer(scored_data)
    if summarized_json_data is None or len(summarized_json_data) == 0:
        logger("error", "Failed to summarize descriptions.")
        return _finish(False, [])

    final_json_data = list(summarized_json_data)
    final_json_data.sort(key=_sort_key)
    logger("info", "Sorted records by deadline, then relevance")

    csv_path = _write_csv(final_json_data)

    try:
        summary: LoadSummary = load_grants_from_records(final_json_data)
        onlyTheGoodStuff.last_new_ids = summary.new_ids  # type: ignore[attr-defined]
    except Exception as exc:
        logger("error", f"Failed to load data into the database: {exc}")

    final_length = len(final_json_data)
    retained_pct = (final_length / length_initial) * 100 if length_initial else 0
    logger("info", f"Initial by final length: {length_initial} / {final_length}")
    logger("info", f"Percentage of data retained: {round(retained_pct, 2)}%")

    return _finish(True, final_json_data, csv_path)


onlyTheGoodStuff.last_csv_path = None  # type: ignore[attr-defined]
onlyTheGoodStuff.last_new_ids = None  # type: ignore[attr-defined]
onlyTheGoodStuff.last_profile = None  # type: ignore[attr-defined]
