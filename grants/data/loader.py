"""Insert grants data into PostgreSQL."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from notifications.gmail_notifier import send_grant_notification
from sql_utils import get_connection, get_subscribers_for_fields

from logs.status_logger import logger

_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
]

_SPLIT_PATTERN = re.compile(r"[;,/]+")



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



def _serialise_categories(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        raw = value
    elif isinstance(value, (list, tuple, set)):
        raw = ";".join(str(segment) for segment in value)
    else:
        raw = str(value)
    parts = [segment.strip() for segment in _SPLIT_PATTERN.split(raw) if segment.strip()]
    cleaned = "; ".join(parts)
    return cleaned if cleaned else None



def _extract_fields(opportunity_category: Any, funding_categories: str | None) -> List[tuple[str, str]]:
    fields: Dict[str, str] = {}

    def _add(raw: Any) -> None:
        if raw in (None, ""):
            return
        label = str(raw).strip()
        if not label:
            return
        key = label.lower()
        fields.setdefault(key, label)

    _add(opportunity_category)

    if funding_categories:
        for chunk in _SPLIT_PATTERN.split(funding_categories):
            _add(chunk)

    return list(fields.items())



def _format_date(value: datetime | None) -> str:
    if not value:
        return "N/A"
    return value.strftime("%b %d, %Y")



def _notify_subscribers(field_grants: Dict[str, List[Dict[str, Any]]], field_labels: Dict[str, str]) -> None:
    subscribers_map = get_subscribers_for_fields(field_grants.keys())
    if not any(subscribers_map.values()):
        return

    email_payload: Dict[str, Dict[str, Any]] = {}
    for field_key, grants in field_grants.items():
        subscribers = subscribers_map.get(field_key)
        if not subscribers:
            continue
        label = field_labels.get(field_key, field_key.title())
        for email in subscribers:
            payload = email_payload.setdefault(email, {"fields": set(), "grants": {}})
            payload["fields"].add(label)
            grant_map = payload["grants"]
            for grant in grants:
                entry = grant_map.setdefault(
                    grant["opp_id"],
                    {
                        "opp_id": grant["opp_id"],
                        "title": grant["title"],
                        "stage": grant.get("stage"),
                        "close_date": grant.get("close_date"),
                        "post_date": grant.get("post_date"),
                        "url": grant.get("url"),
                        "agency": grant.get("agency"),
                        "matched_fields": set(),
                    },
                )
                entry["matched_fields"].add(label)

    if not email_payload:
        return

    sent = 0
    for email, data in email_payload.items():
        fields_sorted = sorted(data["fields"])
        subject_focus = ", ".join(fields_sorted[:2])
        if len(fields_sorted) > 2:
            subject_focus += " + more"
        subject = f"GrantWatch: new grants in {subject_focus or 'your fields'}"

        grants = list(data["grants"].values())
        grants.sort(key=lambda item: (item["close_date"] is None, item["close_date"] or item["post_date"] or datetime.max))

        lines = [
            "Hi there,",
            "",
            f"You asked to hear about new grants in: {', '.join(fields_sorted)}.",
            "",
            "Here are the latest opportunities:",
            "",
        ]

        for grant in grants:
            lines.append(f"- {grant['title']} (ID: {grant['opp_id']})")
            meta_parts: List[str] = []
            if grant.get("close_date"):
                meta_parts.append(f"Due {_format_date(grant['close_date'])}")
            if grant.get("post_date"):
                meta_parts.append(f"Posted {_format_date(grant['post_date'])}")
            if grant.get("stage"):
                meta_parts.append(f"Stage: {grant['stage'].title()}")
            if grant.get("matched_fields"):
                meta_parts.append(f"Matches: {', '.join(sorted(grant['matched_fields']))}")
            if meta_parts:
                lines.append(f"  {' | '.join(meta_parts)}")
            if grant.get("agency"):
                lines.append(f"  Agency: {grant['agency']}")
            if grant.get("url"):
                lines.append(f"  {grant['url']}")
            lines.append("")

        lines.extend(
            [
                "--",
                "Update your subscription preferences any time from the GrantWatch dashboard.",
            ]
        )

        body = "\n".join(lines).strip()
        if send_grant_notification(subject, body, [email]):
            sent += 1

    logger("info", f"Dispatched subscriber updates to {sent} recipients")



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
    field_grants: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    field_labels: Dict[str, str] = {}

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
            opportunity_category = record.get("OPPORTUNITY_CATEGORY")
            funding_categories = _serialise_categories(record.get("FUNDING_CATEGORIES"))

            cur.execute(
                """
                INSERT INTO grants (opp_id, title, stage, opportunity_status,
                                    opportunity_category, funding_categories,
                                    post_date, close_date, archive_date, description)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (opp_id) DO NOTHING;
                """,
                (
                    opp_id,
                    title,
                    stage,
                    status,
                    opportunity_category,
                    funding_categories,
                    post_date,
                    close_date,
                    archive_date,
                    description,
                ),
            )
            if cur.rowcount:
                inserted += cur.rowcount
                for key, label in _extract_fields(opportunity_category, funding_categories):
                    field_labels.setdefault(key, label)
                    field_grants[key].append(
                        {
                            "opp_id": opp_id,
                            "title": title,
                            "stage": stage,
                            "close_date": close_date,
                            "post_date": post_date,
                            "agency": record.get("AGENCY"),
                            "url": record.get("OPPORTUNITY_URL"),
                        }
                    )

    if field_grants:
        _notify_subscribers(field_grants, field_labels)

    logger("info", f"Inserted {inserted} grants into the database")
    return inserted



def load_grants_from_json(path: str) -> int:
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)

    if not isinstance(data, list):
        raise ValueError("Expected a list of grants in the JSON file")

    return load_grants_from_records(data)
