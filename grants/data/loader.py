"""Insert grants data into PostgreSQL."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from sql_utils import get_connection

from logs.status_logger import logger

_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
]


def derive_stage(title: str, description: str) -> str:
    """Basic heuristic: mark concept vs full proposal."""
    text = (title + " " + description).lower()
    if any(word in text for word in ["concept", "pre-proposal", "preproposal", "letter of intent", "loi"]):
        return "concept"
    return "full"


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value)
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except ValueError:
            continue
    logger("warning", f"Unable to parse timestamp '{text}'")
    return None


def _legacy_to_record(grant: Dict[str, Any]) -> Dict[str, Any]:
    if "OPPORTUNITY_NUMBER" in grant:
        return grant
    return {
        "OPPORTUNITY_NUMBER": grant.get("opportunityNumber"),
        "OPPORTUNITY_TITLE": grant.get("title", ""),
        "FUNDING_DESCRIPTION": grant.get("description", ""),
        "OPPORTUNITY_STATUS": grant.get("opportunityStatus", "Posted"),
        "POSTED_DATE": grant.get("postDate"),
        "CLOSE_DATE": grant.get("closeDate"),
        "ARCHIVE_DATE": grant.get("archiveDate"),
    }


def load_grants_from_records(records: Iterable[Dict[str, Any]]) -> int:
    inserted = 0
    with get_connection() as conn, conn.cursor() as cur:
        for grant in records:
            record = _legacy_to_record(grant)
            opp_id = record.get("OPPORTUNITY_NUMBER")
            if not opp_id:
                logger("warning", "Skipping grant without OPPORTUNITY_NUMBER")
                continue

            title = record.get("OPPORTUNITY_TITLE", "")
            description = record.get("SUMMARY") or record.get("FUNDING_DESCRIPTION", "")
            stage = derive_stage(str(title), str(description))
            status = record.get("OPPORTUNITY_STATUS", "Posted")
            post_date = _parse_timestamp(record.get("POSTED_DATE"))
            close_date = _parse_timestamp(record.get("CLOSE_DATE"))
            archive_date = _parse_timestamp(record.get("ARCHIVE_DATE"))

            cur.execute(
                """
                INSERT INTO grants (opp_id, title, stage, opportunity_status,
                                    post_date, close_date, archive_date, description)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (opp_id) DO NOTHING;
                """,
                (opp_id, title, stage, status, post_date, close_date, archive_date, description),
            )
            inserted += cur.rowcount

    logger("info", f"Inserted {inserted} grants into the database")
    return inserted


def load_grants_from_json(path: str) -> int:
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)

    if not isinstance(data, list):
        raise ValueError("Expected a list of grants in the JSON file")

    return load_grants_from_records(data)
