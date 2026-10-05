"""Insert grants data into PostgreSQL."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Set

from notifications.gmail_notifier import send_grant_notification
from grants.sql_utils import db_connection, get_subscribers_for_fields
from grants_data.normalize import strip_html

from logs.status_logger import logger

_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
]

_SPLIT_PATTERN = re.compile(r"[;,/]+")
_NUMERIC_JUNK = re.compile(r"[,$\s]")


@dataclass
class LoadSummary:
    inserted: int = 0
    updated: int = 0
    new_ids: Set[str] = field(default_factory=set)


def derive_stage(title: str, description: str) -> str:
    """Basic heuristic: mark concept vs full proposal."""
    text = (title + " " + description).lower()
    if any(word in text for word in ["concept", "pre-proposal", "preproposal", "letter of intent", "loi"]):
        return "concept"
    return "full"


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value)
        parsed = None
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                logger("warning", f"Unable to parse timestamp '{text}'")
                return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_numeric(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    text = _NUMERIC_JUNK.sub("", str(value))
    if not text or text.lower() in {"none", "n/a", "na"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
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


_UPSERT = """
    INSERT INTO grants (opp_id, title, stage, opportunity_status,
                        opportunity_category, funding_categories,
                        post_date, close_date, archive_date, description,
                        summary, agency, agency_code, url, matched_keywords,
                        award_ceiling, estimated_total_funding,
                        relevance_score, relevance_reason, relevance_model,
                        relevance_profile, relevance_scored_at, last_seen_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
    ON CONFLICT (opp_id) DO UPDATE SET
        title = EXCLUDED.title,
        stage = EXCLUDED.stage,
        opportunity_status = EXCLUDED.opportunity_status,
        opportunity_category = EXCLUDED.opportunity_category,
        funding_categories = EXCLUDED.funding_categories,
        post_date = EXCLUDED.post_date,
        close_date = EXCLUDED.close_date,
        archive_date = EXCLUDED.archive_date,
        description = EXCLUDED.description,
        -- A model-written summary replaces the old one; a truncated fallback never does.
        summary = CASE WHEN EXCLUDED.relevance_score IS NOT NULL
                       THEN EXCLUDED.summary
                       ELSE COALESCE(grants.summary, EXCLUDED.summary) END,
        agency = COALESCE(EXCLUDED.agency, grants.agency),
        agency_code = COALESCE(EXCLUDED.agency_code, grants.agency_code),
        url = COALESCE(EXCLUDED.url, grants.url),
        matched_keywords = COALESCE(EXCLUDED.matched_keywords, grants.matched_keywords),
        award_ceiling = COALESCE(EXCLUDED.award_ceiling, grants.award_ceiling),
        estimated_total_funding = COALESCE(EXCLUDED.estimated_total_funding, grants.estimated_total_funding),
        -- Keep an existing score when this run could not score (no model configured).
        relevance_score = COALESCE(EXCLUDED.relevance_score, grants.relevance_score),
        relevance_reason = COALESCE(EXCLUDED.relevance_reason, grants.relevance_reason),
        relevance_model = COALESCE(EXCLUDED.relevance_model, grants.relevance_model),
        relevance_profile = COALESCE(EXCLUDED.relevance_profile, grants.relevance_profile),
        relevance_scored_at = COALESCE(EXCLUDED.relevance_scored_at, grants.relevance_scored_at),
        last_seen_at = NOW()
    RETURNING (xmax = 0) AS is_new;
"""


def load_grants_from_records(records: Iterable[Dict[str, Any]]) -> LoadSummary:
    summary = LoadSummary()
    field_grants: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    field_labels: Dict[str, str] = {}

    with db_connection() as conn, conn.cursor() as cur:
        for grant in records:
            record = _legacy_to_record(grant)
            opp_id = record.get("OPPORTUNITY_NUMBER")
            if not opp_id:
                logger("warning", "Skipping grant without OPPORTUNITY_NUMBER")
                continue

            title = record.get("OPPORTUNITY_TITLE", "")
            description = strip_html(record.get("FUNDING_DESCRIPTION", ""))
            stage = derive_stage(str(title), description)
            status = record.get("OPPORTUNITY_STATUS", "Posted")
            post_date = _parse_timestamp(record.get("POSTED_DATE"))
            close_date = _parse_timestamp(record.get("CLOSE_DATE"))
            archive_date = _parse_timestamp(record.get("ARCHIVE_DATE"))
            opportunity_category = record.get("OPPORTUNITY_CATEGORY")
            funding_categories = _serialise_categories(record.get("FUNDING_CATEGORIES"))
            matched = record.get("MATCHED_KEYWORDS")
            if isinstance(matched, (list, tuple, set)):
                matched = "; ".join(str(item) for item in matched) or None
            # Field names written by llm_utils.relevance.
            relevance_score = record.get("RELEVANCE_SCORE")

            cur.execute(
                _UPSERT,
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
                    (record.get("SUMMARY") or None),
                    record.get("AGENCY") or None,
                    record.get("AGENCY_CODE") or None,
                    record.get("OPPORTUNITY_URL") or None,
                    matched,
                    _parse_numeric(record.get("AWARD_CEILING")),
                    _parse_numeric(record.get("ESTIMATED_TOTAL_FUNDING")),
                    int(relevance_score) if relevance_score is not None else None,
                    record.get("RELEVANCE_REASON") or None,
                    record.get("RELEVANCE_MODEL") or None,
                    record.get("RELEVANCE_PROFILE") or None,
                    _parse_timestamp(record.get("RELEVANCE_SCORED_AT")),
                ),
            )
            row = cur.fetchone()
            is_new = bool(row and row[0])
            if is_new:
                summary.inserted += 1
                summary.new_ids.add(str(opp_id))
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
            else:
                summary.updated += 1

    if field_grants:
        _notify_subscribers(field_grants, field_labels)

    logger("info", f"Database load complete: {summary.inserted} new, {summary.updated} updated")
    return summary


def load_grants_from_json(path: str) -> LoadSummary:
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)

    if not isinstance(data, list):
        raise ValueError("Expected a list of grants in the JSON file")

    return load_grants_from_records(data)
