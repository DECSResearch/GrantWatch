"""Download grants data from Grants.gov and persist as JSON."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from logs.status_logger import logger

_DEFAULT_EXPORT_URL = "https://micro.grants.gov/rest/opportunities/search_export_Mark2"
_DATA_DIR = Path(__file__).resolve().parent / "grants_json_data"
_TIMEOUT = int(os.getenv("GRANTS_GOV_TIMEOUT", "60"))
_DEFAULT_ROWS = 5000
_DEFAULT_SORT = "openDate|desc"
_DEFAULT_STATUSES = "forecasted|posted"

_BASE_HEADERS: Dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://grants.gov",
    "Referer": "https://grants.gov/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.6778.140 Safari/537.36"
    ),
}


def _headers() -> Dict[str, str]:
    headers = dict(_BASE_HEADERS)
    user_agent = os.getenv("GRANTS_GOV_USER_AGENT")
    if user_agent:
        headers["User-Agent"] = user_agent
    return headers


def _env_or_none(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value and value.strip():
        return value.strip()
    return None


def _build_payload(rows: int) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "keyword": _env_or_none("GRANTS_GOV_QUERY"),
        "cfda": _env_or_none("GRANTS_GOV_CFDA"),
        "agencies": _env_or_none("GRANTS_GOV_AGENCIES"),
        "sortBy": os.getenv("GRANTS_GOV_SORT_BY", _DEFAULT_SORT),
        "rows": rows,
        "eligibilities": _env_or_none("GRANTS_GOV_ELIGIBILITIES"),
        "fundingCategories": _env_or_none("GRANTS_GOV_FUNDING_CATEGORIES"),
        "fundingInstruments": _env_or_none("GRANTS_GOV_FUNDING_INSTRUMENTS"),
        "dateRange": _env_or_none("GRANTS_GOV_DATE_RANGE"),
        "oppStatuses": os.getenv("GRANTS_GOV_OPP_STATUSES", _DEFAULT_STATUSES),
    }

    cleaned = {key: value for key, value in payload.items() if value is not None}
    filters = {k: v for k, v in cleaned.items() if k not in {"rows", "sortBy"}}
    if filters and not (len(filters) == 1 and filters.get("oppStatuses") == _DEFAULT_STATUSES):
        logger("info", f"Export payload filters: {filters}")
    cleaned["rows"] = rows
    cleaned["sortBy"] = cleaned.get("sortBy", _DEFAULT_SORT)
    return cleaned


def _write_output(records: list[dict[str, Any]]) -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    destination = _DATA_DIR / f"grants_{timestamp}.json"
    with destination.open("w", encoding="utf-8") as fp:
        json.dump(records, fp, ensure_ascii=False, indent=2)
    return destination


def gen_grants(rows: Optional[int] = None) -> bool:
    """Download grants data and store it locally."""

    gen_grants.last_download_path = None  # type: ignore[attr-defined]

    export_url = os.getenv("GRANTS_GOV_EXPORT_URL", _DEFAULT_EXPORT_URL)
    try:
        requested_rows = rows or int(os.getenv("GRANTS_GOV_ROWS", str(_DEFAULT_ROWS)))
    except ValueError:
        requested_rows = _DEFAULT_ROWS
        logger("warning", f"Invalid GRANTS_GOV_ROWS value; defaulting to {requested_rows}")

    logger("info", f"Downloading Grants.gov export from {export_url} (rows={requested_rows})")

    payload = _build_payload(requested_rows)

    try:
        response = requests.post(export_url, headers=_headers(), json=payload, timeout=_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger("error", f"Failed to download grants data: {exc}")
        return False

    try:
        records = response.json()
    except ValueError as exc:
        logger("error", f"Unexpected response format from Grants.gov: {exc}")
        return False

    if not isinstance(records, list):
        logger("error", "Export payload was not a list; aborting")
        return False

    if not records:
        logger("warning", "Grants.gov export returned no records")
        return False

    destination = _write_output(records)
    logger("info", f"Stored {len(records)} grants into {destination}")
    gen_grants.last_download_path = destination  # type: ignore[attr-defined]
    return True


gen_grants.last_download_path = None  # type: ignore[attr-defined]


