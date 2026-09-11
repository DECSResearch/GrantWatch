"""Build the deadline-first digest (plain text and HTML) from grant records.

Pure functions: nothing here touches the network or the database, so the
digest is easy to test and the same text goes to the console and to email.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

_DATE_FORMATS = ["%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"]


def _to_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


@dataclass
class DigestItem:
    opp_id: str
    title: str
    agency: str = ""
    url: str = ""
    close_date: Optional[date] = None
    forecasted: bool = False
    score: Optional[int] = None
    reason: str = ""
    summary: str = ""
    watched: bool = False
    is_new: bool = False

    @classmethod
    def from_record(cls, record: Dict[str, Any], *, is_new: bool = False, watched: bool = False) -> "DigestItem":
        """Build from a pipeline record (UPPER_SNAKE keys)."""
        score = record.get("RELEVANCE_SCORE")
        return cls(
            opp_id=str(record.get("OPPORTUNITY_NUMBER") or ""),
            title=str(record.get("OPPORTUNITY_TITLE") or "Untitled"),
            agency=str(record.get("AGENCY") or ""),
            url=str(record.get("OPPORTUNITY_URL") or ""),
            close_date=_to_date(record.get("CLOSE_DATE")),
            forecasted=str(record.get("OPPORTUNITY_STATUS") or "").lower() == "forecasted",
            score=int(score) if score is not None else None,
            reason=str(record.get("RELEVANCE_REASON") or ""),
            summary=str(record.get("SUMMARY") or ""),
            watched=watched,
            is_new=is_new,
        )

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "DigestItem":
        """Build from a database row (lower_snake keys, see fetch_upcoming)."""
        score = row.get("relevance_score")
        return cls(
            opp_id=str(row.get("opp_id") or ""),
            title=str(row.get("title") or "Untitled"),
            agency=str(row.get("agency") or ""),
            url=str(row.get("url") or ""),
            close_date=_to_date(row.get("close_date")),
            forecasted=str(row.get("opportunity_status") or "").lower() == "forecasted",
            score=int(score) if score is not None else None,
            reason=str(row.get("relevance_reason") or ""),
            summary=str(row.get("summary") or ""),
            watched=bool(row.get("watched")),
            is_new=False,
        )

    def days_left(self, today: date) -> Optional[int]:
        if self.close_date is None:
            return None
        return (self.close_date - today).days


@dataclass
class Digest:
    subject: str
    text: str
    html: str
    new_count: int
    deadline_count: int


def merge_items(*groups: Iterable[DigestItem]) -> List[DigestItem]:
    """Later groups override earlier ones; ``is_new`` and ``watched`` survive from any."""
    merged: Dict[str, DigestItem] = {}
    for group in groups:
        for item in group:
            if not item.opp_id:
                continue
            previous = merged.get(item.opp_id)
            if previous is not None:
                item.is_new = item.is_new or previous.is_new
                item.watched = item.watched or previous.watched
                if item.score is None:
                    item.score, item.reason = previous.score, previous.reason
                if not item.summary:
                    item.summary = previous.summary
            merged[item.opp_id] = item
    return list(merged.values())


def _relevant(item: DigestItem, min_score: int, scoring_active: bool) -> bool:
    if not scoring_active:
        return True  # nothing has a score yet: list every keyword match
    return item.watched or (item.score is not None and item.score >= min_score)


def _bucket_label(lower: int, upper: int) -> str:
    if lower == 0:
        return f"Closes within {upper} days"
    return f"Closes in {lower + 1} to {upper} days"


def _fmt_date(value: Optional[date]) -> str:
    return value.strftime("%b %d, %Y") if value else "no date"


def _score_text(score: Optional[int]) -> str:
    return f"{score}/5" if score is not None else "unscored"


def _line_meta(item: DigestItem, today: date) -> str:
    days = item.days_left(today)
    parts = [item.agency or "Unknown agency"]
    if item.close_date:
        when = f"closes {_fmt_date(item.close_date)}"
        if days is not None:
            when += f" ({days} day{'s' if days != 1 else ''} left)"
        if item.forecasted:
            when += ", estimated"
        parts.append(when)
    else:
        parts.append("no deadline listed")
    if item.watched:
        parts.append("tracked")
    return "; ".join(parts)


def _text_entry(item: DigestItem, today: date) -> List[str]:
    lines = [f"- [{_score_text(item.score)}] {item.title}", f"  {_line_meta(item, today)}"]
    detail = item.reason or item.summary
    if detail:
        lines.append(f"  {detail}")
    if item.url:
        lines.append(f"  {item.url}")
    lines.append("")
    return lines


def _html_entry(item: DigestItem, today: date) -> str:
    days = item.days_left(today)
    urgency = "#8a1f1a" if days is not None and days <= 7 else "#7a4e00" if days is not None and days <= 30 else "#15221c"
    title = html.escape(item.title)
    if item.url:
        title = f'<a href="{html.escape(item.url, quote=True)}" style="color:#1e5a44;text-decoration:none">{title}</a>'
    squares = "".join(
        f'<span style="display:inline-block;width:8px;height:8px;margin-right:2px;background:{"#1e5a44" if item.score is not None and i < item.score else "#d6dbd7"}"></span>'
        for i in range(5)
    )
    detail = html.escape(item.reason or item.summary)
    return (
        '<tr><td style="padding:10px 0;border-top:1px solid #d6dbd7;vertical-align:top;font-family:Public Sans,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.45;color:#15221c">'
        f'<div style="font-weight:600">{title}</div>'
        f'<div style="color:{urgency};font-size:13px;margin-top:2px">{html.escape(_line_meta(item, today))}</div>'
        f'<div style="margin-top:4px">{squares}<span style="font-size:12px;color:#5b6660;margin-left:6px">{html.escape(_score_text(item.score))}</span></div>'
        + (f'<div style="margin-top:4px;color:#3b4642">{detail}</div>' if detail else "")
        + "</td></tr>"
    )


def build_digest(
    items: Sequence[DigestItem],
    *,
    today: date,
    horizons: Sequence[int],
    min_score: int,
    profile_line: str = "",
    max_new: int = 30,
    max_per_bucket: int = 40,
) -> Optional[Digest]:
    """Return the digest, or None when there is nothing worth sending."""
    horizons = sorted({int(h) for h in horizons if int(h) > 0}) or [7, 30, 60, 90]
    scoring_active = any(item.score is not None for item in items)
    relevant = [item for item in items if _relevant(item, min_score, scoring_active)]

    new_items = sorted(
        (item for item in relevant if item.is_new),
        key=lambda i: (-(i.score if i.score is not None else -1), i.close_date or date.max, i.title),
    )

    buckets: List[tuple[str, List[DigestItem]]] = []
    lower = 0
    for upper in horizons:
        members = [
            item
            for item in relevant
            if item.days_left(today) is not None and lower <= item.days_left(today) <= upper  # type: ignore[operator]
        ]
        members.sort(key=lambda i: (i.close_date or date.max, -(i.score if i.score is not None else -1), i.title))
        if members:
            buckets.append((_bucket_label(lower, upper), members))
        lower = upper

    deadline_count = sum(len(members) for _, members in buckets)
    if not new_items and not deadline_count:
        return None

    urgent = sum(1 for item in relevant if item.days_left(today) is not None and 0 <= item.days_left(today) <= horizons[0])  # type: ignore[operator]
    subject_parts = []
    if new_items:
        subject_parts.append(f"{len(new_items)} new")
    if urgent:
        subject_parts.append(f"{urgent} closing within {horizons[0]} days")
    if not subject_parts:
        subject_parts.append(f"{deadline_count} deadline{'s' if deadline_count != 1 else ''} in the next {horizons[-1]} days")
    subject = "GrantWatch: " + ", ".join(subject_parts)

    text_lines: List[str] = [f"GrantWatch digest for {today.strftime('%A, %b %d, %Y')}"]
    if profile_line:
        text_lines.append(profile_line)
    text_lines.append("")
    html_sections: List[str] = []

    def section(title: str, members: List[DigestItem], cap: int) -> None:
        shown, hidden = members[:cap], max(0, len(members) - cap)
        text_lines.append(f"{title} ({len(members)})")
        text_lines.append("")
        for item in shown:
            text_lines.extend(_text_entry(item, today))
        if hidden:
            text_lines.append(f"  and {hidden} more in the dashboard.")
            text_lines.append("")
        rows = "".join(_html_entry(item, today) for item in shown)
        more = f'<p style="font-family:Public Sans,Helvetica,Arial,sans-serif;font-size:13px;color:#5b6660;margin:8px 0 0">and {hidden} more in the dashboard.</p>' if hidden else ""
        html_sections.append(
            f'<h2 style="font-family:Public Sans,Helvetica,Arial,sans-serif;font-size:17px;font-weight:600;color:#1e5a44;margin:28px 0 4px">{html.escape(title)} <span style="color:#5b6660;font-weight:400">({len(members)})</span></h2>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse">{rows}</table>{more}'
        )

    if new_items:
        section("New this run", new_items, max_new)
    for label, members in buckets:
        section(label, members, max_per_bucket)

    if scoring_active:
        footnote = f"Scores are relative to the research profile; grants below {min_score}/5 are left out unless tracked."
    else:
        footnote = "No relevance scores yet, so every keyword match is listed. Fill in the research profile and set GRANTS_LLM_MODEL to rank them."
    text_lines.append(footnote)
    text = "\n".join(text_lines).rstrip() + "\n"

    html_doc = (
        '<div style="max-width:640px;margin:0 auto;padding:24px 16px;background:#f7f8f6">'
        f'<div style="font-family:Public Sans,Helvetica,Arial,sans-serif;font-size:22px;font-weight:600;color:#15221c">GrantWatch digest</div>'
        f'<div style="font-family:Public Sans,Helvetica,Arial,sans-serif;font-size:14px;color:#5b6660;margin-top:2px">{html.escape(today.strftime("%A, %b %d, %Y"))}'
        + (f" &#183; {html.escape(profile_line)}" if profile_line else "")
        + "</div>"
        + "".join(html_sections)
        + f'<p style="font-family:Public Sans,Helvetica,Arial,sans-serif;font-size:12px;color:#5b6660;margin-top:28px">{html.escape(footnote)}</p>'
        "</div>"
    )
    return Digest(subject=subject, text=text, html=html_doc, new_count=len(new_items), deadline_count=deadline_count)
