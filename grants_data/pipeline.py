"""End-to-end grants data pipeline."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from grants.data.loader import load_grants_from_records

from grants_data.date_filter_data import date_filter_json_data
from grants_data.download_json import gen_grants
from grants_data.filter_with_forecast import filter_forecasted_data
from grants_data.get_file_path import get_latest_file_path
from grants_data.get_json_data import process_json_data
from grants_data.keyword_filter_data import filter_grants_by_keywords
from llm_utils.gpt_summarizer import description_summarizer
from llm_utils.keywords_gen import keyword_extractor
from logs.status_logger import logger

_CSV_DIR = Path(__file__).resolve().parent / "grants_csv_data"
_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
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
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    destination = destination_dir / f"grants_{timestamp}.csv"

    fieldnames = [
        "OPPORTUNITY_NUMBER",
        "OPPORTUNITY_TITLE",
        "OPPORTUNITY_STATUS",
        "POSTED_DATE",
        "CLOSE_DATE",
        "ARCHIVE_DATE",
        "OPPORTUNITY_CATEGORY",
        "FUNDING_CATEGORIES",
        "AGENCY",
        "OPPORTUNITY_URL",
        "MATCHED_KEYWORDS",
        "SUMMARY",
        "FUNDING_DESCRIPTION",
    ]

    with destination.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({field: _serialise_value(record.get(field)) for field in fieldnames})

    logger("info", f"Wrote filtered grants to {destination}")
    return destination


def _parse_sort_date(value: object) -> datetime:
    if value in (None, ""):
        return datetime.min
    text = str(value)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.min


def _sort_key(record: Dict[str, object]) -> Tuple[datetime, str]:
    return _parse_sort_date(record.get("POSTED_DATE")), str(record.get("OPPORTUNITY_NUMBER", ""))


def onlyTheGoodStuff() -> Tuple[bool, List[Dict[str, object]]]:
    success = gen_grants()
    if not success:
        logger("error", "Failed to generate grants data.")
        onlyTheGoodStuff.last_csv_path = None  # type: ignore[attr-defined]
        return success, []

    latest_file_path = getattr(gen_grants, "last_download_path", None)
    if latest_file_path is None:
        latest_file_path = get_latest_file_path()

    if latest_file_path is None:
        logger("error", "No latest file path found.")
        onlyTheGoodStuff.last_csv_path = None  # type: ignore[attr-defined]
        return False, []

    whole_json_data = process_json_data(latest_file_path)
    length_initial = len(whole_json_data)
    if length_initial == 0:
        logger("error", "Failed to process JSON data.")
        onlyTheGoodStuff.last_csv_path = None  # type: ignore[attr-defined]
        return False, []

    date_sorted_data = date_filter_json_data(whole_json_data)
    if len(date_sorted_data) == 0:
        logger("warning", "No data found after date filtering.")
        onlyTheGoodStuff.last_csv_path = None  # type: ignore[attr-defined]
        return False, []

    keywords, threshold, forecast = keyword_extractor()
    if keywords is None or len(keywords) == 0:
        logger("error", "Failed to extract keywords.")
        onlyTheGoodStuff.last_csv_path = None  # type: ignore[attr-defined]
        return False, []

    if not forecast:
        logger("info", "Forecast is set to False. Filtering grants with OPPORTUNITY_STATUS = 'Forecasted'")
        status_sorted_data = filter_forecasted_data(date_sorted_data)
        if len(status_sorted_data) == 0:
            logger("info", "No data found after status filtering.")
            onlyTheGoodStuff.last_csv_path = None  # type: ignore[attr-defined]
            return False, []
    else:
        logger("info", "Forecast is set to True. Keeping all data.")
        status_sorted_data = date_sorted_data

    keyword_json_data = filter_grants_by_keywords(
        status_sorted_data,
        "FUNDING_DESCRIPTION",
        keywords,
        threshold,
    )
    if len(keyword_json_data) == 0:
        logger("warning", "No data found after keyword filtering.")
        onlyTheGoodStuff.last_csv_path = None  # type: ignore[attr-defined]
        return False, []
    logger("info", f"Filtered keyword length: {len(keyword_json_data)}")

    summarized_json_data = description_summarizer(keyword_json_data)
    if summarized_json_data is None or len(summarized_json_data) == 0:
        logger("error", "Failed to summarize descriptions.")
        onlyTheGoodStuff.last_csv_path = None  # type: ignore[attr-defined]
        return False, []

    final_json_data = list(summarized_json_data)
    final_json_data.sort(key=_sort_key, reverse=True)
    logger("info", "Sorted JSON data")

    csv_path = _write_csv(final_json_data)

    try:
        inserted = load_grants_from_records(final_json_data)
        logger("info", f"Database insert complete (rows affected: {inserted})")
    except Exception as exc:
        logger("error", f"Failed to load data into the database: {exc}")

    final_length = len(final_json_data)
    retained_pct = (final_length / length_initial) * 100 if length_initial else 0
    logger("info", f"Initial by final length: {length_initial} / {final_length}")
    logger("info", f"Percentage of data retained: {round(retained_pct, 2)}%")

    onlyTheGoodStuff.last_csv_path = csv_path  # type: ignore[attr-defined]
    return True, final_json_data


onlyTheGoodStuff.last_csv_path = None  # type: ignore[attr-defined]

