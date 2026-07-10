"""Download and unzip the daily Grants.gov XML database extract.

Grants.gov publishes a full database extract every morning (~04:00 ET) as a
ZIP containing a single XML file:

    https://prod-grants-gov-chatbot.s3.amazonaws.com/extracts/GrantsDBExtractYYYYMMDDv2.zip

Unlike the search_export endpoint used by download_json.gen_grants (capped at
``rows``), the extract contains every open + forecasted + archived opportunity,
so it is the robust source for full refreshes.
"""
from __future__ import annotations

import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from grants_data.retention import prune_old_files
from logs.status_logger import logger

_DEFAULT_EXTRACT_URL_TEMPLATE = (
    "https://prod-grants-gov-chatbot.s3.amazonaws.com/extracts/"
    "GrantsDBExtract{date}v2.zip"
)
_DATA_DIR = Path(__file__).resolve().parent / "grants_xml_data"
_TIMEOUT = int(os.getenv("GRANTS_GOV_TIMEOUT", "60"))
_CHUNK_SIZE = 1024 * 1024
_MAX_LOOKBACK_DAYS = int(os.getenv("GRANTS_GOV_EXTRACT_LOOKBACK_DAYS", "3"))


def _extract_url(date: datetime) -> str:
    template = os.getenv("GRANTS_GOV_EXTRACT_URL_TEMPLATE", _DEFAULT_EXTRACT_URL_TEMPLATE)
    return template.format(date=date.strftime("%Y%m%d"))


def _find_available_extract() -> Optional[str]:
    """Return the URL of the most recent extract that exists (today, else back a few days)."""
    for offset in range(_MAX_LOOKBACK_DAYS + 1):
        candidate_date = datetime.now(timezone.utc) - timedelta(days=offset)
        url = _extract_url(candidate_date)
        try:
            response = requests.head(url, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            logger("warning", f"HEAD request failed for {url}: {exc}")
            continue
        if response.status_code == 200:
            return url
        logger("info", f"No extract at {url} (HTTP {response.status_code})")
    return None


def _download_zip(url: str, destination: Path) -> bool:
    try:
        with requests.get(url, stream=True, timeout=_TIMEOUT) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length", 0))
            written = 0
            with destination.open("wb") as fp:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    fp.write(chunk)
                    written += len(chunk)
            if total and written != total:
                logger("error", f"Incomplete download: {written} of {total} bytes")
                destination.unlink(missing_ok=True)
                return False
    except requests.RequestException as exc:
        logger("error", f"Failed to download extract: {exc}")
        destination.unlink(missing_ok=True)
        return False
    logger("info", f"Downloaded {written / (1024 * 1024):.1f} MB to {destination}")
    return True


def _unzip(archive_path: Path) -> Optional[Path]:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            xml_names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
            if not xml_names:
                logger("error", f"No XML file found inside {archive_path}")
                return None
            extracted = archive.extract(xml_names[0], _DATA_DIR)
    except zipfile.BadZipFile as exc:
        logger("error", f"Downloaded file is not a valid ZIP: {exc}")
        return None
    return Path(extracted)


def gen_extract(keep_zip: bool = False) -> bool:
    """Download the latest daily XML extract and unzip it locally.

    On success ``gen_extract.last_extract_path`` points at the extracted XML.
    """

    gen_extract.last_extract_path = None  # type: ignore[attr-defined]

    url = _find_available_extract()
    if url is None:
        logger("error", f"No extract found in the last {_MAX_LOOKBACK_DAYS + 1} days")
        return False

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = _DATA_DIR / url.rsplit("/", 1)[-1]

    logger("info", f"Downloading Grants.gov extract from {url}")
    if not _download_zip(url, zip_path):
        return False

    xml_path = _unzip(zip_path)
    if xml_path is None:
        return False

    if not keep_zip:
        zip_path.unlink(missing_ok=True)

    # The XML runs ~300 MB per day; keep only the two most recent.
    prune_old_files(_DATA_DIR, "GrantsDBExtract*.xml", keep=2)

    logger("info", f"Extracted XML to {xml_path} ({xml_path.stat().st_size / (1024 * 1024):.1f} MB)")
    gen_extract.last_extract_path = xml_path  # type: ignore[attr-defined]
    return True


gen_extract.last_extract_path = None  # type: ignore[attr-defined]
