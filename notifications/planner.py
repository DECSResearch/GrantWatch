"""Decide what each recipient should be told today.

Pure functions over plain data: the grants in the database, what this
recipient has already been sent, and today's date. Nothing here touches the
network or the database, so the rules below can be checked by simulating
weeks of daily runs (see tests/test_planner_properties.py).

The rules:

* A grant is announced once, the first run it is open and scores at or above
  the recipient's threshold. Whether it was inserted today or a month ago
  does not matter, so a grant scored late (model down, per-run cap) is still
  announced. Tracked grants are not announced; the team already has them.
  Until the model has scored any grant, every grant that meets the profile's
  keyword threshold (at least one match) counts, ranked by how many keywords
  it mentions. The database can hold grants that matched nothing, because a
  model scores every open grant, so a model outage never announces those.
  A rule's 0 for a grant that Grants.gov's eligibility codes rule out is not
  a model score: it neither ends that fallback nor lets the grant in.
* New grants are listed with those closing within URGENT_DAYS first, soonest
  deadline first, then the rest by score (or keyword matches) and deadline.
  The email's cap cuts that list from the end, so an urgent grant is never
  held back while a less urgent one is shown.
* A program posted by several services (see grants.display) is one row. It
  counts as announced once any of its postings was, so every posting is
  recorded when the row is shown, and its score is its best posting's. The
  row is led by the best scored posting the eligibility rules did not rule
  out, or by a tracked one; a ruled-out posting only follows it.
* The team's votes (see grants.feedback) are summed over a program's postings.
  A program the team marked Not for us is left out of the new grants and the
  overview, unless it is tracked. One marked Relevant counts as meeting the
  threshold, whatever its score; a tie is no verdict.
* A tracked grant gets one reminder each time its deadline passes one of the
  reminder marks. A new deadline resets the reminders and is announced once.
* A tracked grant that is archived early or drops out of the extract is
  announced once as withdrawn.
* The overview of everything relevant by deadline goes out on the overview
  day (or on the next run after a missed one).
* Nothing is recorded here. The caller records a notice only after the email
  that carried it was sent, so a failed send is retried on the next run.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from grants.display import Posting, days_until_deadline, firm_deadline, group_postings
from grants.feedback import net_verdict

NEW = "new"
REMINDER = "reminder"
CHANGED = "changed"
REMOVED = "removed"
OVERVIEW = "overview"

# New grants closing within this many days are listed first, whatever their score.
URGENT_DAYS = 30

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass(frozen=True)
class Recipient:
    id: int
    email: str
    name: str = ""
    min_score: Optional[int] = None
    unsubscribe_token: str = ""


@dataclass(frozen=True)
class GrantState:
    opp_id: str
    title: str
    agency: str = ""
    url: str = ""
    close_date: Optional[date] = None
    archive_date: Optional[date] = None
    forecasted: bool = False
    score: Optional[int] = None
    by_rule: bool = False  # the score is a rule's 0 (the eligibility pre-check), not the model's
    reason: str = ""
    summary: str = ""
    watched: bool = False
    listed: bool = True  # present in a recent extract
    matches: int = 0  # how many profile keywords it mentions
    agency_code: str = ""
    net_votes: int = 0  # the team's Relevant votes minus its Not for us votes
    also: Tuple["GrantState", ...] = ()  # the same program's other postings; the planner fills this in

    def days_left(self, today: date) -> Optional[int]:
        """None without a firm deadline: no date, or a placeholder years away."""
        return days_until_deadline(self.close_date, today)

    def firm_deadline(self, today: date) -> bool:
        return firm_deadline(self.close_date, today)

    def urgent(self, today: date) -> bool:
        days = self.days_left(today)
        return days is not None and 0 <= days <= URGENT_DAYS

    def closed(self, today: date) -> bool:
        return self.close_date is not None and self.close_date < today

    def archived(self, today: date) -> bool:
        return self.archive_date is not None and self.archive_date < today

    def is_open(self, today: date) -> bool:
        return not self.closed(today) and not self.archived(today) and self.listed

    def withdrawn(self, today: date) -> bool:
        """Still before its deadline, but no longer taking applications."""
        return not self.closed(today) and (self.archived(today) or not self.listed)


@dataclass(frozen=True)
class SentRecord:
    """One row of the notifications table."""

    recipient_id: int
    opp_id: str
    kind: str
    threshold: int = -1
    ref_date: Optional[date] = None
    score: Optional[int] = None

    def key(self) -> Tuple[int, str, str, int, Optional[date]]:
        return (self.recipient_id, self.opp_id, self.kind, self.threshold, self.ref_date)


@dataclass(frozen=True)
class Notice:
    kind: str
    grant: GrantState
    threshold: int = -1
    previous_date: Optional[date] = None  # the old deadline, for CHANGED

    def records(self, recipient_id: int) -> List[SentRecord]:
        """One row for the grant, and one for each other posting of the same program."""
        return [
            SentRecord(recipient_id, g.opp_id, self.kind, self.threshold, None if self.kind == REMOVED else g.close_date, g.score)
            for g in (self.grant, *self.grant.also)
        ]


@dataclass
class NotifySettings:
    min_score: int = 4
    reminder_days: Sequence[int] = (30, 14, 7, 2)
    horizons: Sequence[int] = (7, 30, 60, 90)
    overview_day: str = "monday"
    include_forecasted: bool = True
    keyword_threshold: int = 1
    max_new: int = 25
    max_per_bucket: int = 10
    max_bytes: int = 90_000
    dashboard_url: str = ""
    profile_line: str = ""


@dataclass
class Plan:
    recipient: Recipient
    reminders: List[Notice] = field(default_factory=list)
    changes: List[Notice] = field(default_factory=list)
    removed: List[Notice] = field(default_factory=list)
    new: List[Notice] = field(default_factory=list)  # every unannounced program, urgent first, then best first
    overview: List[Tuple[str, List[GrantState]]] = field(default_factory=list)
    overview_due: bool = False
    scoring_active: bool = False
    min_score: int = 4
    has_tracked: bool = False

    @property
    def empty(self) -> bool:
        return not (self.reminders or self.changes or self.removed or self.new or any(m for _, m in self.overview))


def overview_due(last: Optional[date], today: date, day: str) -> bool:
    day = (day or "monday").lower()
    if day == "never":
        return False
    if last is not None and last >= today:
        return False
    if day == "daily" or last is None:
        return True
    if (today - last).days >= 7:
        return True  # the overview day was missed; catch up
    return today.weekday() == _WEEKDAYS.index(day if day in _WEEKDAYS else "monday")


def bucket_label(lower: int, upper: int) -> str:
    if lower == 0:
        return f"Closes within {upper} days"
    return f"Closes in {lower + 1} to {upper} days"


def _date_key(value: Optional[date]) -> date:
    return value or date.max


def plan_email(
    recipient: Recipient,
    grants: Iterable[GrantState],
    sent: Iterable[SentRecord],
    today: date,
    settings: NotifySettings,
) -> Plan:
    """Everything this recipient should hear about today, before any cap."""
    mine = [s for s in sent if s.recipient_id == recipient.id]
    announced = {s.opp_id for s in mine if s.kind == NEW}
    reminded = {(s.opp_id, s.threshold, s.ref_date) for s in mine if s.kind == REMINDER}
    withdrawn_sent = {s.opp_id for s in mine if s.kind == REMOVED}
    last_known: Dict[str, date] = {}
    last_overview: Optional[date] = None
    for s in mine:  # oldest first, so the last deadline mentioned wins
        if s.kind in (NEW, REMINDER, CHANGED) and s.ref_date is not None:
            last_known[s.opp_id] = s.ref_date
        elif s.kind == OVERVIEW and s.ref_date is not None:
            last_overview = max(last_overview or s.ref_date, s.ref_date)

    min_score = settings.min_score if recipient.min_score is None else recipient.min_score
    pool = [g for g in grants if settings.include_forecasted or not g.forecasted or g.watched]
    open_grants = [g for g in pool if g.is_open(today)]
    scoring_active = any(g.score is not None and not g.by_rule for g in open_grants)
    plan = Plan(recipient=recipient, scoring_active=scoring_active, min_score=min_score, has_tracked=any(g.watched for g in pool))

    # With nothing scored by the model yet, only keyword matches count (see the fallback below).
    needed = max(1, settings.keyword_threshold)

    def relevant(g: GrantState) -> bool:
        if g.watched:
            return True
        if not scoring_active:
            return not g.by_rule and g.matches >= needed
        return g.score is not None and g.score >= min_score

    marks = sorted({int(m) for m in settings.reminder_days if int(m) >= 0})
    for g in open_grants:
        if not g.watched or g.close_date is None:
            continue
        previous = last_known.get(g.opp_id)
        if previous is not None and previous != g.close_date:
            plan.changes.append(Notice(CHANGED, g, previous_date=previous))
        days = g.days_left(today)
        crossed = [m for m in marks if days is not None and days <= m]
        if crossed and (g.opp_id, crossed[0], g.close_date) not in reminded:
            plan.reminders.append(Notice(REMINDER, g, threshold=crossed[0]))

    for g in pool:
        if g.watched and g.withdrawn(today) and g.opp_id not in withdrawn_sent:
            plan.removed.append(Notice(REMOVED, g))

    # One program per row: the postings of a joint call fold into one row, led by the best scored
    # posting the rules did not rule out (or a tracked one).
    by_id = {g.opp_id: g for g in open_grants}
    postings = [
        Posting(opp_id=g.opp_id, title=g.title, agency_code=g.agency_code, close=g.close_date,
                posted=not g.forecasted, score=g.score, watched=g.watched)
        for g in open_grants
    ]
    programs = [[by_id[group.lead], *(by_id[o] for o in group.also)] for group in group_postings(postings)]

    def row(members: List[GrantState]) -> GrantState:
        # A posting the eligibility codes ruled out (score 0 by rule) ranks above an unscored one
        # but must not stand for the program: lead with one that may apply, unless it is tracked.
        lead = next((g for g in members if g.watched or not g.by_rule), members[0])
        return replace(lead, also=tuple(g for g in members if g is not lead)) if len(members) > 1 else lead

    def verdict(members: List[GrantState]) -> int:
        """What the team thinks of the program: 1 Relevant, -1 Not for us, 0 no verdict (or a tie)."""
        return net_verdict(sum(g.net_votes for g in members))

    def declined(members: List[GrantState]) -> bool:
        return verdict(members) < 0 and not any(g.watched for g in members)

    # A program is announced once any of its postings was, and a tracked posting means the
    # team already has it. Its score is its best posting's.
    fresh: List[Tuple[int, GrantState]] = []
    for members in programs:
        if any(g.watched or g.opp_id in announced for g in members) or declined(members):
            continue
        if scoring_active:
            rank = max((g.score for g in members if g.score is not None), default=None)
            qualifies = rank is not None and rank >= min_score
        else:
            # The model has scored nothing yet: announce the keyword matches, most hits first. A
            # model that scores every open grant also loads ones that matched nothing, so the
            # threshold is checked here (and at least one match is always needed). A grant the
            # eligibility codes rule out has matches too, but is never announced.
            rank = max((g.matches for g in members if not g.by_rule), default=0)
            qualifies = rank >= needed
        if verdict(members) > 0:  # the team said yes: as good as the threshold
            rank, qualifies = max(rank or 0, min_score if scoring_active else needed), True
        if qualifies:
            fresh.append((rank, row(members)))
    # Those closing within URGENT_DAYS first, soonest first; then the rest, best first.
    urgent = sorted((f for f in fresh if f[1].urgent(today)), key=lambda f: (_date_key(f[1].close_date), -f[0], f[1].title, f[1].opp_id))
    later = sorted((f for f in fresh if not f[1].urgent(today)), key=lambda f: (-f[0], _date_key(f[1].close_date), f[1].title, f[1].opp_id))
    plan.new = [Notice(NEW, g) for _, g in urgent + later]

    plan.reminders.sort(key=lambda n: (_date_key(n.grant.close_date), n.grant.title, n.grant.opp_id))
    plan.changes.sort(key=lambda n: (_date_key(n.grant.close_date), n.grant.title, n.grant.opp_id))
    plan.removed.sort(key=lambda n: (n.grant.title, n.grant.opp_id))

    plan.overview_due = overview_due(last_overview, today, settings.overview_day)
    if plan.overview_due:
        horizons = sorted({int(h) for h in settings.horizons if int(h) > 0}) or [7, 30, 60, 90]
        candidates = [
            row(m) for m in programs
            if (any(relevant(g) for g in m) or verdict(m) > 0) and not declined(m) and m[0].firm_deadline(today)
        ]
        lower = -1
        for upper in horizons:
            members = [g for g in candidates if lower < (g.days_left(today) or 0) <= upper]
            members.sort(key=lambda g: (_date_key(g.close_date), -(g.score if g.score is not None else -1), g.title))
            if members:
                plan.overview.append((bucket_label(max(lower, 0), upper), members))
            lower = upper
    return plan
