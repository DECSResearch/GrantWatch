"""Rules for showing Grants.gov data without passing off placeholders as facts.

Pure functions over plain data (no database, no web framework), so the API,
the email planner and the tests share one definition of each rule.

Grants.gov fills fields it has no answer for with far-future dates (a close
date in 2076) and 999,999,999 for an award ceiling. Both look like facts.

It also lists one program several times. A joint call is posted once per
service (DURIP by the Army, ONR and AFOSR, all closing the same day), and a
solicitation is posted again when it is renewed (Expeditions in Computing,
2020 and 2026). ``group_postings`` folds the first kind into one row, and
only notes the second: an older solicitation can still be open and worth a
look, so it is never hidden.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# A close date further out than this is a placeholder, not a deadline.
FIRM_DEADLINE_DAYS = 5 * 365
# A day count further out than this is left out of the email; the date says enough.
COUNTDOWN_DAYS = 365
# An award ceiling at or above this is a placeholder, not an amount.
PLACEHOLDER_CEILING = 999_999_999


def days_until_deadline(close: Optional[date], today: date) -> Optional[int]:
    """Days to a firm deadline, or None when there is none (no date, or a placeholder)."""
    if close is None:
        return None
    days = (close - today).days
    return days if days <= FIRM_DEADLINE_DAYS else None


def firm_deadline(close: Optional[date], today: date) -> bool:
    return days_until_deadline(close, today) is not None


def real_ceiling(value: Optional[float]) -> Optional[float]:
    """The award ceiling, or None when it is missing or the Grants.gov placeholder."""
    if value is None or value >= PLACEHOLDER_CEILING:
        return None
    return value


# Postings of one program from several services share a close date and end
# their title in the same one-word acronym, "... Program (DURIP)". Numerals and
# anything with a digit are not acronyms: a year, a fiscal-year tag, or an NIH
# activity code such as R01, which two services use for two different grants.
_ACRONYM_RE = re.compile(r"\(\s*([^\s()]+)\s*\)\s*$")
_NOT_AN_ACRONYM_RE = re.compile(r"[IVX]+|.*\d.*")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
# A solicitation is superseded by a posting this much newer (2 years).
SUPERSEDED_GAP_DAYS = 2 * 365


@dataclass(frozen=True)
class Posting:
    """The facts about one grant that the grouping rules read."""

    opp_id: str  # unique
    title: str
    agency_code: str = ""
    close: Optional[date] = None
    posted_on: Optional[date] = None
    posted: bool = True  # Posted, not Forecasted
    score: Optional[int] = None
    watched: bool = False


@dataclass(frozen=True)
class Group:
    """One row to show: the lead posting, the same program's other postings, a newer solicitation."""

    lead: str
    also: Tuple[str, ...] = ()
    newer: Optional[str] = None


def trailing_acronym(title: Optional[str]) -> Optional[str]:
    """The one-word all-caps acronym a title ends with, such as ``DURIP`` or ``S-STEM``, else None.

    A token with a digit (``R01``, ``FY27``) is a code or a year, not an acronym.
    """
    match = _ACRONYM_RE.search(title or "")
    if not match:
        return None
    token = match.group(1)
    return token if token.isupper() and len(token) > 1 and not _NOT_AN_ACRONYM_RE.fullmatch(token) else None


def base_title(title: Optional[str]) -> str:
    """The title without one trailing parenthetical, case and spacing folded."""
    return " ".join(_TRAILING_PAREN_RE.sub("", (title or "").strip()).casefold().split())


def _agency(posting: Posting) -> str:
    return posting.agency_code.strip().upper()


def _lead_key(posting: Posting) -> Tuple[bool, int, str]:
    """Tracked first, then the best score, then the opportunity number."""
    return (not posting.watched, -(posting.score if posting.score is not None else -1), posting.opp_id)


def _same_program(postings: Sequence[Posting]) -> Dict[str, List[str]]:
    """Lead opp_id -> the other postings it stands for, for each program posted by several services.

    Postings that share a close date and an acronym form a program only if every
    one has an agency code and no two share it. A bucket that breaks that (seven
    CESU cooperative agreements from one USGS office, say) is not one program
    posted by several services, so none of it is folded.
    """
    buckets: Dict[Tuple[date, str], List[Posting]] = {}
    for p in postings:
        acronym = trailing_acronym(p.title)
        if p.close is not None and acronym and _agency(p):
            buckets.setdefault((p.close, acronym), []).append(p)
    folded: Dict[str, List[str]] = {}
    for members in buckets.values():
        if len(members) < 2 or len({_agency(m) for m in members}) < len(members):
            continue
        lead = min(members, key=_lead_key)
        rest = sorted((m for m in members if m is not lead), key=lambda m: (_agency(m), m.opp_id))
        folded[lead.opp_id] = [m.opp_id for m in rest]
    return folded


def _replaceable(posting: Posting) -> Optional[Tuple[Tuple[str, str], date]]:
    """(agency and base title, posted on) for a posting that a newer one can replace, else None."""
    title = base_title(posting.title)
    if posting.posted and posting.posted_on and _agency(posting) and title:
        return (_agency(posting), title), posting.posted_on
    return None


def group_postings(postings: Sequence[Posting]) -> List[Group]:
    """One group per row to show, in the order of each group's lead in ``postings``.

    Every posting is in exactly one group, as its lead or as one of its ``also``.
    Which posting leads, who is ``also`` and what is ``newer`` do not depend on
    the order of ``postings``; only the order of the groups does.
    """
    folded = _same_program(postings)
    absorbed = {opp_id for rest in folded.values() for opp_id in rest}
    # Same agency, both Posted, the same title once one trailing parenthetical is
    # dropped: the later posting replaces the earlier one when it is 2+ years newer.
    families: Dict[Tuple[str, str], List[Tuple[date, str]]] = {}
    for p in postings:
        found = _replaceable(p)
        if found:
            families.setdefault(found[0], []).append((found[1], p.opp_id))
    groups = []
    for p in postings:
        if p.opp_id in absorbed:
            continue
        found = _replaceable(p)
        newer = [f for f in families[found[0]] if (f[0] - found[1]).days >= SUPERSEDED_GAP_DAYS] if found else []
        groups.append(Group(p.opp_id, tuple(folded.get(p.opp_id, ())), max(newer)[1] if newer else None))
    return groups


def _day(value: Any) -> Optional[date]:
    """A date from a date, a datetime or an ISO string such as the API sends."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def group_rows(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """API rows (see grants.queries.serialize_row) folded into one row per program.

    Each row is its group's lead, unchanged, plus ``also_posted`` (the other
    services' postings: opp_id, agency, url, watched) and ``superseded_by``
    ({opp_id, url} of a newer solicitation, or None). Rows keep their order.
    """
    by_id = {row["opp_id"]: row for row in rows}
    postings = [
        Posting(
            opp_id=row["opp_id"],
            title=row.get("title") or "",
            agency_code=row.get("agency_code") or "",
            close=_day(row.get("close_date")),
            posted_on=_day(row.get("post_date")),
            posted=str(row.get("opportunity_status") or "").lower() == "posted",
            score=row.get("relevance_score"),
            watched=bool(row.get("watched")),
        )
        for row in rows
    ]
    out = []
    for group in group_postings(postings):
        row = dict(by_id[group.lead])
        row["also_posted"] = [
            {"opp_id": o, "agency": by_id[o].get("agency"), "url": by_id[o].get("url"), "watched": bool(by_id[o].get("watched"))}
            for o in group.also
        ]
        row["superseded_by"] = {"opp_id": group.newer, "url": by_id[group.newer].get("url")} if group.newer else None
        out.append(row)
    return out
