"""The ``personal`` step: score each member's own profile after the team's pass.

It runs between ingest and notify (see main.py) on the candidates ingest left in
``onlyTheGoodStuff.last_candidates``: the grants the team pass sent to the model. The team's
scores are already loaded by then, so a slow or killed personal pass costs nobody the day's load
or email. Whatever it did not finish is simply pending, and the next run continues from there.

Members with a profile take turns. On a turn a member scores up to ``CHUNK`` of their pending
grants, soonest close date first, and the chunk is written to ``member_scores`` in one
transaction. No turn starts after ``GRANTS_PERSONAL_STOP_MINUTES`` from the start of the
process. A model outage stops everyone; any other trouble stops only the member it happened to.
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Set

from psycopg2.extras import execute_values

from grants.members import ProfileError, clean_summary, personal_profile
from grants.profile import Profile
from grants.sql_utils import db_connection, fetch_member_votes
from llm_utils.relevance import LLMSettings, ScoringOutcome, close_order, personal_key, score_records
from logs.status_logger import logger

# Grants a member scores per turn. Small enough that a stop (time, outage) loses little and that
# members take turns often; the model sees them 3 at a time anyway (GRANTS_LLM_BATCH_SIZE).
CHUNK = 60
DEFAULT_STOP_MINUTES = 65.0
# The beginnings of an abort reason that mean the model is down for everyone, not for one member.
_OUTAGE = ("cannot reach", "authentication failed")


@dataclass(frozen=True)
class Member:
    email: str
    summary: str  # cleaned: 40 to 2,000 characters (grants.members.clean_summary)


@dataclass
class Totals:
    """What one personal pass did, over all members. ``stopped`` says why it ended with work left, else None."""

    people: int = 0
    scored: int = 0
    failed: int = 0  # taken in a chunk and not saved: the model gave no usable answer, or the write failed
    pending: int = 0  # not scored under the member's current key when the pass ended (failed ones included)
    stopped: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"people": self.people, "scored": self.scored, "failed": self.failed, "pending": self.pending, "stopped": self.stopped}


def stop_minutes() -> float:
    """``GRANTS_PERSONAL_STOP_MINUTES``: minutes from the process start after which no turn begins. Default 65."""
    raw = (os.getenv("GRANTS_PERSONAL_STOP_MINUTES") or "").strip()
    if not raw:
        return DEFAULT_STOP_MINUTES
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger("warning", f"GRANTS_PERSONAL_STOP_MINUTES is '{raw}', not a number of minutes; using {DEFAULT_STOP_MINUTES:g}")
        return DEFAULT_STOP_MINUTES


def why_skipped(
    ingested: bool, settings: LLMSettings, team: Profile, stats: Dict[str, Any], candidates: Optional[Sequence[Any]]
) -> Optional[str]:
    """Why the step has nothing to do before it reads the database, or None. The model is down or off for everyone."""
    if not ingested:
        return "ingest did not finish"
    if not settings.enabled:
        return "no model is configured (GRANTS_LLM_MODEL)"
    if not team.is_configured:
        return "the team profile is not configured"
    if stats.get("scoring_abort"):
        return f"the team's scoring stopped: {stats['scoring_abort']}"
    if not candidates:
        return "ingest sent no grants to the model"
    return None


# --- the database ---------------------------------------------------------------------------------


def fetch_members() -> List[Member]:
    """Active members who have a profile of their own, by email. One whose stored text fails cleaning is skipped."""
    query = """
        SELECT p.email, p.summary FROM member_profiles p
        JOIN recipients r ON r.email = p.email AND r.active
        ORDER BY p.email;
    """
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    members = []
    for email, summary in rows:
        try:
            members.append(Member(email, clean_summary(summary)))
        except ProfileError as exc:
            logger("warning", f"Skipping the profile of {email}: {exc}")
    return members


def fetch_scored_numbers(email: str, key: str) -> Set[str]:
    """The grants this member already has a score for under ``key`` (their profile as scored now)."""
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT opp_id FROM member_scores WHERE email = %s AND score_key = %s;", (email, key))
        return {opp_id for (opp_id,) in cur.fetchall()}


# One statement in one transaction. The rows are written only while the member still has the profile
# they were scored for (the summary is part of the match): a profile deleted or edited meanwhile writes
# nothing, where the foreign key would otherwise raise or old-text scores would outlive the edit.
_WRITE_SQL = """
    INSERT INTO member_scores (opp_id, email, score, reason, model, score_key, scored_at)
    SELECT v.opp_id, v.email, v.score, v.reason, v.model, v.score_key, v.scored_at
    FROM (VALUES %s) AS v (opp_id, email, score, reason, model, score_key, scored_at, summary)
    WHERE EXISTS (SELECT 1 FROM member_profiles p WHERE p.email = v.email AND p.summary = v.summary)
    ON CONFLICT (opp_id, email) DO UPDATE SET
        score = EXCLUDED.score, reason = EXCLUDED.reason, model = EXCLUDED.model,
        score_key = EXCLUDED.score_key, scored_at = EXCLUDED.scored_at;
"""
_WRITE_TEMPLATE = "(%s, %s, %s::smallint, %s, %s, %s, %s, %s)"


def _scored_at(value: Any) -> datetime:
    """The scorer's ISO timestamp as the naive UTC time the column holds."""
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    return stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo else stamp


def write_scores(member: Member, key: str, entries: Dict[str, Dict[str, Any]]) -> int:
    """Save one chunk's scores (``ScoringOutcome.new_entries``) for ``member``; the number of rows written.

    0 when the member's profile is gone or has other text now: nothing is written then.
    """
    if not entries:
        return 0
    rows = [
        (opp_id, member.email, entry["score"], entry.get("reason") or "", entry.get("model"), key, _scored_at(entry.get("scored_at")), member.summary)
        for opp_id, entry in sorted(entries.items())
    ]
    with db_connection() as conn, conn.cursor() as cur:
        execute_values(cur, _WRITE_SQL, rows, template=_WRITE_TEMPLATE, page_size=len(rows))
        return cur.rowcount


# --- the loop -------------------------------------------------------------------------------------


@dataclass
class _Person:
    member: Member
    profile: Profile
    key: str
    queue: Deque[Dict[str, Any]]  # their pending grants, soonest close date first; a grant leaves it when it is taken in a chunk
    start: int  # pending when the pass began
    scored: int = 0
    failed: int = 0
    stopped: Optional[str] = None  # why this member took no more turns this run
    votes: Optional[List[Dict[str, Any]]] = field(default=None)


def _pending(member: Member, team: Profile, model: str, candidates: Sequence[Dict[str, Any]], scored_numbers: Callable[[str, str], Set[str]]) -> _Person:
    profile = personal_profile(team, member.summary)
    key = personal_key(profile, model)
    done = scored_numbers(member.email, key)
    unique = {str(record["OPPORTUNITY_NUMBER"]): record for record in candidates if record.get("OPPORTUNITY_NUMBER")}
    todo = sorted((record for number, record in unique.items() if number not in done), key=close_order)
    return _Person(member, profile, key, deque(todo), start=len(todo))


def score_people(
    members: Sequence[Member],
    candidates: Sequence[Dict[str, Any]],
    team: Profile,
    settings: LLMSettings,
    *,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    score: Callable[..., ScoringOutcome] = score_records,
    scored_numbers: Callable[[str, str], Set[str]] = fetch_scored_numbers,
    votes_of: Callable[[Sequence[str]], List[Dict[str, Any]]] = fetch_member_votes,
    write: Callable[[Member, str, Dict[str, Dict[str, Any]]], int] = write_scores,
    chunk: int = CHUNK,
    client: Any = None,
) -> Totals:
    """Take turns scoring each member's pending ``candidates`` until all are done or something stops the pass.

    A turn is the next ``chunk`` of the member's pending grants, soonest close date first, scored with
    the unchanged ``score_records`` (``personal=True``, ``already_scored=None``, ``max_per_run`` the
    chunk's size, their votes as the examples) and written in one transaction by ``write``. Every
    grant in a chunk leaves the member's queue whether or not it was scored, so one that keeps
    failing is tried again next run, not next turn. No turn begins once ``clock()`` reaches
    ``deadline``: what is left is pending, not an error. A model outage stops everyone; an abort
    for any other reason, a chunk that scored nothing, or a write that failed stops only that member.
    """
    people = [_pending(member, team, settings.model, candidates, scored_numbers) for member in members]
    totals = Totals(people=len(people))
    turns: Deque[_Person] = deque(person for person in people if person.queue)
    outage: Optional[str] = None
    timed_out = False
    while turns:
        if clock() >= deadline:
            timed_out = True
            break
        person = turns.popleft()
        outage = _turn(person, settings, score, votes_of, write, chunk, client)
        if outage:
            break
        if person.queue and not person.stopped:
            turns.append(person)

    totals.scored = sum(person.scored for person in people)
    totals.failed = sum(person.failed for person in people)
    totals.pending = sum(person.start - person.scored for person in people)
    halted = [person for person in people if person.stopped and person.start > person.scored]
    if outage:
        totals.stopped = f"the model is unavailable: {outage}"
    elif timed_out:
        totals.stopped = "time limit"
    elif halted:
        totals.stopped = f"{len(halted)} of {len(people)} members stopped early: {halted[0].stopped}"
    return totals


def _turn(person: _Person, settings: LLMSettings, score: Callable[..., ScoringOutcome], votes_of: Callable[[Sequence[str]], List[Dict[str, Any]]],
          write: Callable[[Member, str, Dict[str, Dict[str, Any]]], int], size: int, client: Any) -> Optional[str]:
    """One chunk for ``person``. Returns the abort reason when it means the model is down for everyone, else None."""
    taken = [person.queue.popleft() for _ in range(min(size, len(person.queue)))]
    if person.votes is None:
        try:
            person.votes = votes_of([person.member.email])
        except Exception as exc:
            logger("warning", f"Could not read {person.member.email}'s votes; scoring without examples: {exc}")
            person.votes = []
    try:
        outcome = score(
            taken, person.profile, already_scored=None, settings=replace(settings, max_per_run=len(taken)),
            client=client, feedback=person.votes, personal=True,
        )
    except Exception as exc:
        person.stopped = f"scoring failed: {exc}"
        person.failed += len(taken)
        logger("error", f"Personal scoring for {person.member.email} stopped: {exc}")
        return None

    kept = 0
    if outcome.new_entries:
        try:
            kept = write(person.member, person.key, outcome.new_entries)
        except Exception as exc:
            person.stopped = f"could not save scores: {exc}"
            logger("error", f"Could not save {person.member.email}'s scores; stopping them for this run: {exc}")
        else:
            if kept == 0:
                person.stopped = "their profile was removed or changed while scoring"
                logger("warning", f"{person.member.email}'s profile changed while their chunk was scored; nothing was saved")
    person.scored += kept
    person.failed += len(taken) - kept
    logger("info", f"Personal scoring for {person.member.email}: {kept} of {len(taken)} saved, {len(person.queue)} still pending")

    if outcome.abort_reason:
        if outcome.abort_reason.startswith(_OUTAGE):
            return outcome.abort_reason
        person.stopped = person.stopped or outcome.abort_reason
    elif not outcome.new_entries and not person.stopped:
        # Nothing came back (for instance every batch was too long for the server): the next chunk would fare the same.
        person.stopped = f"the model gave no usable answer for a chunk of {len(taken)}"
        logger("warning", f"Personal scoring for {person.member.email}: {person.stopped}; stopping them for this run")
    return None


def personal_detail(totals: Totals) -> str:
    """One sentence about a pass for the run log."""
    if not totals.people:
        return "No member has a profile of their own yet."
    people = f"{totals.people} member" + ("" if totals.people == 1 else "s")
    text = f"{people}: {totals.scored:,} newly scored" + (f" ({totals.failed:,} failed)" if totals.failed else "")
    text += f", {totals.pending:,} still pending"
    return f"{text}. Stopped: {str(totals.stopped).rstrip('.')}." if totals.stopped else f"{text}."


def run(candidates: Sequence[Dict[str, Any]], team: Profile, settings: LLMSettings, started: float, *, clock: Callable[[], float] = time.monotonic) -> Totals:
    """The pass for every active member with a profile. ``started`` is ``clock()`` at the start of the process."""
    members = fetch_members()
    if not members:
        return Totals()
    return score_people(members, candidates, team, settings, deadline=started + stop_minutes() * 60, clock=clock)
