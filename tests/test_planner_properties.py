"""Properties of the email service, checked over simulated weeks of daily runs.

Each example invents a set of grants whose state changes from day to day
(scores arrive late or never, deadlines move, grants drop out of the extract,
tracked grants come and go), sends some days fail, and some days run twice.
Every run goes through run_notify with an in-memory store, and the emails are
read back as text. The properties are the spec:

1. A grant is announced to a recipient at most once.
2. Each run announces min(backlog, max_new) of the unannounced qualifying
   grants (by score, or by keyword matches before the model has scored
   anything, and only grants with at least max(1, keyword_threshold) of them,
   never one the eligibility rules scored 0), so nothing qualifying is lost;
   repeated runs drain the backlog and then go quiet.
   They are listed urgent first: the ones closing within 30 days, soonest
   first, then the rest best first. The cap cuts from the end of that list,
   so an urgent grant is held back only if no other kind is shown.
3. A failed send records nothing.
4. After a successful send every due reminder for a tracked grant is
   recorded, at most once per mark and deadline, and the recipient's latest
   known deadline for each tracked grant they have heard about is current.
5. A withdrawn tracked grant is announced at most once.
6. No message, encoded, exceeds Gmail's ~102 KB clipping limit.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional

from hypothesis import HealthCheck, given, settings, strategies as st

from notifications.digest import render_email
from notifications.notify import run_notify
from notifications.planner import NEW, REMINDER, CHANGED, GrantState, NotifySettings, Recipient, SentRecord, plan_email
from notifications.transport import build_message
from support import CaptureTransport, MemoryStore, sections

DAY0 = date(2026, 9, 14)
NEW_TITLE = "New grants that fit"
REMINDER_TITLE = "Deadline reminders"
REMOVED_TITLE = "Tracked grants no longer open"


@dataclass(frozen=True)
class SimGrant:
    opp_id: str
    close: Optional[int]  # days after DAY0
    archive: Optional[int]
    score: Optional[int]
    score_day: int  # the day the model gets to it
    watched_from: Optional[int]
    move_day: Optional[int]
    move_by: int
    drop_day: Optional[int]
    matches: int
    by_rule: bool = False  # the eligibility rules score it 0, not the model

    def on(self, day: int) -> GrantState:
        close = self.close
        if close is not None and self.move_day is not None and day >= self.move_day:
            close += self.move_by
        scored = self.score if day >= self.score_day else None
        by_rule = self.by_rule and scored is not None
        return GrantState(
            opp_id=self.opp_id,
            title=f"Grant {self.opp_id}",
            agency="NSF",
            url=f"https://example.org/{self.opp_id}",
            close_date=DAY0 + timedelta(days=close) if close is not None else None,
            archive_date=DAY0 + timedelta(days=self.archive) if self.archive is not None else None,
            score=0 if by_rule else scored,
            by_rule=by_rule,
            reason="fits",
            watched=self.watched_from is not None and day >= self.watched_from,
            listed=self.drop_day is None or day < self.drop_day,
            matches=self.matches,
        )


def sim_grant(index: int):
    maybe_day = st.one_of(st.none(), st.integers(0, 20))
    return st.builds(
        SimGrant,
        opp_id=st.just(f"G{index:02d}"),
        close=st.one_of(st.none(), st.integers(-3, 60)),
        archive=st.one_of(st.none(), st.integers(-3, 60)),
        score=st.one_of(st.none(), st.integers(0, 5)),
        score_day=st.integers(0, 20),
        watched_from=maybe_day,
        move_day=maybe_day,
        move_by=st.integers(-20, 40),
        drop_day=st.one_of(st.none(), st.integers(1, 25)),
        matches=st.integers(0, 5),
        by_rule=st.sampled_from([False, False, False, True]),
    )


@st.composite
def scenarios(draw):
    count = draw(st.integers(1, 24))
    grants = [draw(sim_grant(i)) for i in range(count)]
    people = [Recipient(id=1, email="a@und.edu", unsubscribe_token="ta")]
    if draw(st.booleans()):
        people.append(Recipient(id=2, email="b@und.edu", min_score=draw(st.one_of(st.none(), st.integers(0, 5))), unsubscribe_token="tb"))
    days = draw(st.integers(1, 21))
    failures = draw(st.sets(st.tuples(st.integers(0, days - 1), st.sampled_from([p.email for p in people])), max_size=6))
    reruns = draw(st.sets(st.integers(0, days - 1), max_size=4))
    config = NotifySettings(
        min_score=draw(st.integers(0, 5)),
        reminder_days=draw(st.lists(st.integers(1, 30), min_size=1, max_size=4, unique=True)),
        overview_day=draw(st.sampled_from(["monday", "thursday", "daily", "never"])),
        keyword_threshold=draw(st.integers(0, 3)),
        max_new=draw(st.integers(1, 10)),
        dashboard_url="https://gw.example.edu",
    )
    return grants, people, days, failures, reruns, config


def _model_scored(g: GrantState) -> bool:
    """The model judged it: a score that is not the eligibility rules' 0."""
    return g.score is not None and not g.by_rule


def _eligible(grants: List[GrantState], person: Recipient, config: NotifySettings, today: date, announced: set) -> List[GrantState]:
    """Independent statement of rule 2's backlog (not the planner's code)."""
    threshold = config.min_score if person.min_score is None else person.min_score
    open_now = [g for g in grants if g.is_open(today)]
    if not any(_model_scored(g) for g in open_now):
        needed = max(1, config.keyword_threshold)
        return [g for g in open_now if not g.watched and g.opp_id not in announced and not g.by_rule and g.matches >= needed]  # keyword matches
    return [g for g in open_now if not g.watched and g.score is not None and g.score >= threshold and g.opp_id not in announced]


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])  # example count from the profile in conftest.py
@given(scenarios())
def test_email_service_properties(scenario):
    sims, people, days, failures, reruns, config = scenario
    store, transport = MemoryStore(people), CaptureTransport()
    announced = {p.id: set() for p in people}
    withdrawn = Counter()
    reminders = Counter()

    def one_run(day: int, with_failures: bool = True) -> None:
        today = DAY0 + timedelta(days=day)
        grants = [s.on(day) for s in sims]
        store.grant_list = grants
        transport.failing = {email for d, email in failures if d == day and with_failures}
        before_rows = {p.id: [r for r in store.rows if r.recipient_id == p.id] for p in people}
        backlog = {p.id: _eligible(grants, p, config, today, announced[p.id]) for p in people}
        sent_before = len(transport.messages)

        code = run_notify(store=store, transport=transport, today=today, settings=config)
        assert code == 0 or transport.failing  # a recipient with nothing to send cannot fail

        by_email = {m["To"]: m for m in transport.messages[sent_before:]}
        for person in people:
            message = by_email.get(person.email)
            rows_now = [r for r in store.rows if r.recipient_id == person.id]
            if person.email in transport.failing:
                assert rows_now == before_rows[person.id]  # 3
                continue
            got = sections(message) if message else {}
            new_ids = got.get(NEW_TITLE, [])

            assert not (set(new_ids) & announced[person.id])  # 1
            expected = min(len(backlog[person.id]), config.max_new)  # 2
            assert len(new_ids) == expected
            if new_ids:
                by_id = {g.opp_id: g for g in backlog[person.id]}
                shown = [by_id[i] for i in new_ids]  # in the order the email lists them
                left = [g for g in backlog[person.id] if g.opp_id not in new_ids]
                scored = any(_model_scored(g) for g in grants if g.is_open(today))
                rank = (lambda g: g.score) if scored else (lambda g: g.matches)  # noqa: E731
                urgent = lambda g: g.close_date is not None and 0 <= (g.close_date - today).days <= 30  # noqa: E731
                flags = [urgent(g) for g in shown]
                assert flags == sorted(flags, reverse=True)  # every urgent grant before every other
                shown_urgent, left_urgent = [g for g in shown if urgent(g)], [g for g in left if urgent(g)]
                closes = [g.close_date for g in shown_urgent]
                assert closes == sorted(closes)  # soonest first
                if left_urgent:  # the cap held back an urgent grant: nothing else is shown, and it closes no sooner
                    assert len(shown_urgent) == len(shown)
                    assert max(closes) <= min(g.close_date for g in left_urgent)
                shown_rest, left_rest = [g for g in shown if not urgent(g)], [g for g in left if not urgent(g)]
                if shown_rest:  # best first among the rest
                    assert min(rank(g) for g in shown_rest) >= max([rank(g) for g in left_rest] or [0])
                    assert [rank(g) for g in shown_rest] == sorted((rank(g) for g in shown_rest), reverse=True)
            announced[person.id] |= set(new_ids)

            for opp in got.get(REMOVED_TITLE, []):  # 5
                withdrawn[(person.id, opp)] += 1
                assert withdrawn[(person.id, opp)] == 1

            marks = sorted(config.reminder_days)
            last_known = {}
            for r in rows_now:
                if r.kind in (NEW, REMINDER, CHANGED) and r.ref_date is not None:
                    last_known[r.opp_id] = r.ref_date
            keys = {(r.opp_id, r.threshold, r.ref_date) for r in rows_now if r.kind == REMINDER}
            for g in grants:  # 4
                if not (g.watched and g.is_open(today) and g.close_date is not None):
                    continue
                crossed = [m for m in marks if g.days_left(today) <= m]
                if crossed:
                    assert (g.opp_id, crossed[0], g.close_date) in keys
                if g.opp_id in last_known:
                    assert last_known[g.opp_id] == g.close_date
            for opp in got.get(REMINDER_TITLE, []):
                g = next(x for x in grants if x.opp_id == opp)
                reminders[(person.id, opp, g.close_date, [m for m in marks if g.days_left(today) <= m][0])] += 1

            if message is not None:
                assert len(message.as_bytes()) <= 102_400  # 6

    for day in range(days):
        one_run(day)
        if day in reruns:
            one_run(day)

    assert all(count == 1 for count in reminders.values())  # 4

    # 2, liveness: with nothing changing, repeated runs drain every backlog and then go quiet.
    last_day = days - 1
    for _ in range(len(sims) // config.max_new + 3):
        before = len(transport.messages)
        one_run(last_day, with_failures=False)
        if len(transport.messages) == before:
            break
    else:
        raise AssertionError("runs never went quiet")
    final = [s.on(last_day) for s in sims]
    for person in people:
        assert _eligible(final, person, config, DAY0 + timedelta(days=last_day), announced[person.id]) == []


_TEXT = st.text(alphabet=st.characters(codec="utf-8", exclude_categories=["Cs", "Cc"]), max_size=400)


@settings(max_examples=max(20, settings.default.max_examples // 5), deadline=None)
@given(
    titles=st.lists(_TEXT, min_size=1, max_size=300),
    reason=_TEXT,
    max_bytes=st.integers(2_000, 90_000),
)
def test_any_email_fits_under_the_clipping_limit(titles, reason, max_bytes):
    grants = [
        GrantState(opp_id=f"G{i}", title=t or "x", reason=reason, url=f"https://example.org/{i}",
                   close_date=DAY0 + timedelta(days=i % 120), score=5, watched=i % 7 == 0)
        for i, t in enumerate(titles)
    ]
    config = NotifySettings(max_new=300, max_per_bucket=300, max_bytes=max_bytes, dashboard_url="https://gw.example.edu",
                            profile_line=reason)
    person = Recipient(id=1, email="a@und.edu", unsubscribe_token="t")
    plan = plan_email(person, grants, [], DAY0, config)
    email = render_email(plan, config, DAY0)
    if email is None:
        return
    assert email.size <= max_bytes
    message = build_message("GrantWatch <gw@und.edu>", person.email, email.subject, email.text, email.html, email.unsubscribe_url)
    assert len(message.as_bytes()) <= max_bytes + 2_000  # headers and MIME boundaries
    new_shown = [n.grant.opp_id for n in email.shown if n.kind == NEW]
    assert new_shown == [n.grant.opp_id for n in plan.new[: len(new_shown)]]


def _urgent(g: GrantState) -> bool:
    """Closing within 30 days (written out here, not read from the planner)."""
    return g.close_date is not None and 0 <= (g.close_date - DAY0).days <= 30


_CLOSING = st.one_of(st.none(), st.sampled_from([0, 1, 29, 30, 31, 32, 400, 3000]), st.integers(0, 120))


@st.composite
def new_candidates(draw):
    count = draw(st.integers(1, 40))
    return [
        GrantState(
            opp_id=f"G{i:02d}", title=draw(st.sampled_from(["Alpha", "Beta", "Gamma"])), url="https://example.org",
            close_date=None if (offset := draw(_CLOSING)) is None else DAY0 + timedelta(days=offset),
            score=draw(st.one_of(st.none(), st.integers(0, 5))), matches=draw(st.integers(0, 4)),
        )
        for i in range(count)
    ]


@given(grants=new_candidates(), min_score=st.integers(0, 5), threshold=st.integers(0, 3), max_new=st.integers(1, 12))
def test_new_grants_list_urgent_first_and_the_cap_never_holds_one_back_for_a_later_grant(grants, min_score, threshold, max_new):
    config = NotifySettings(min_score=min_score, keyword_threshold=threshold, max_new=max_new, overview_day="never")
    person = Recipient(id=1, email="a@und.edu", unsubscribe_token="t")
    plan = plan_email(person, grants, [], DAY0, config)
    scored = any(g.score is not None for g in grants)
    rank = (lambda g: g.score) if scored else (lambda g: g.matches)  # noqa: E731

    listed = [n.grant for n in plan.new]
    flags = [_urgent(g) for g in listed]
    assert flags == sorted(flags, reverse=True)  # every urgent grant before every other
    urgent = [g for g in listed if _urgent(g)]
    assert [g.close_date for g in urgent] == sorted(g.close_date for g in urgent)  # soonest first
    rest = [g for g in listed if not _urgent(g)]
    best_first = [(-rank(g), g.close_date or date.max) for g in rest]
    assert best_first == sorted(best_first)

    email = render_email(plan, config, DAY0)
    shown = [n.grant for n in email.shown if n.kind == NEW] if email else []
    assert shown == listed[: len(shown)] and len(shown) == min(max_new, len(listed))
    if any(not _urgent(g) for g in shown):
        assert all(g in shown for g in urgent)  # the cap never drops an urgent grant while a later one is shown


@st.composite
def mixed_pools(draw):
    """Grants scored by the model, scored 0 by the eligibility rules, or not scored; each with keyword matches."""
    kinds = draw(st.lists(st.tuples(st.sampled_from(["model", "rule", "none"]), st.integers(0, 5), st.integers(0, 5), st.integers(1, 80)), max_size=16))
    return [
        GrantState(
            opp_id=f"G{i:02d}", title=f"Grant {i:02d}", agency_code=f"AG-{i}", close_date=DAY0 + timedelta(days=days),
            score={"model": score, "rule": 0, "none": None}[kind], by_rule=kind == "rule", matches=matches,
        )
        for i, (kind, score, matches, days) in enumerate(kinds)
    ]


@given(grants=mixed_pools(), min_score=st.integers(1, 5), threshold=st.integers(0, 3))
def test_only_the_models_scores_decide_whether_scoring_is_active_and_a_rules_zero_is_never_announced(grants, min_score, threshold):
    """With any model score, grants are judged by score; with none, by keyword matches. A rule's 0 does neither."""
    config = NotifySettings(min_score=min_score, keyword_threshold=threshold, overview_day="daily")
    plan = plan_email(Recipient(id=1, email="a@und.edu", unsubscribe_token="t"), grants, [], DAY0, config)
    modelled = [g for g in grants if g.by_rule is False and g.score is not None]

    assert plan.scoring_active is bool(modelled)
    if modelled:
        expected = {g.opp_id for g in modelled if g.score >= min_score}
    else:
        expected = {g.opp_id for g in grants if g.score is None and g.matches >= max(1, threshold)}
    assert {n.grant.opp_id for n in plan.new} == expected
    # Every deadline here is within 90 days, so the overview lists the same grants.
    assert {g.opp_id for _, members in plan.overview for g in members} == expected


@st.composite
def rule_programs(draw):
    """One program posted by one to four services: model-scored, scored 0 by the eligibility rules, or not scored; some tracked."""
    close = DAY0 + timedelta(days=draw(st.integers(1, 80)))
    kinds = draw(st.lists(st.tuples(st.sampled_from(["model", "rule", "none"]), st.integers(0, 5), st.integers(0, 3), st.booleans()), min_size=1, max_size=4))
    return [
        GrantState(
            opp_id=f"P{j}", title="Program (PRG)", agency=f"Service {j}", agency_code=f"AG-{j}", url=f"https://example.org/{j}", close_date=close,
            score={"model": score, "rule": 0, "none": None}[kind], by_rule=kind == "rule", matches=matches, watched=watched and draw(st.booleans()),
        )
        for j, (kind, score, matches, watched) in enumerate(kinds)
    ]


@given(members=rule_programs(), min_score=st.integers(0, 5), threshold=st.integers(0, 3))
def test_a_posting_the_rules_ruled_out_never_stands_for_a_program_that_has_another_unless_it_is_tracked(members, min_score, threshold):
    """Whether or not the model has scored anything, in the announcement and in the overview alike."""
    config = NotifySettings(min_score=min_score, keyword_threshold=threshold, overview_day="daily")
    plan = plan_email(Recipient(id=1, email="a@und.edu", unsubscribe_token="t"), members, [], DAY0, config)
    rows = [n.grant for n in plan.new] + [g for _, listed in plan.overview for g in listed]

    for row in rows:
        assert {row.opp_id, *(a.opp_id for a in row.also)} == {g.opp_id for g in members}  # the whole program, once
        assert row.watched or not row.by_rule or all(g.by_rule for g in members)
    if all(g.by_rule for g in members):
        assert plan.new == []  # nothing that may apply: the program is not announced


@st.composite
def programs(draw):
    """Several programs, each posted by one to three services (distinct agency codes, same close date)."""
    grants, announced = [], set()
    for i in range(draw(st.integers(1, 8))):
        close = DAY0 + timedelta(days=draw(st.integers(0, 90)))
        for j in range(draw(st.integers(1, 3))):
            grants.append(GrantState(
                opp_id=f"P{i}-{j}", title=f"Program {i} (PRG{chr(65 + i)})", agency=f"Service {j}", agency_code=f"AG-{j}",
                url=f"https://example.org/{i}/{j}", close_date=close, score=draw(st.one_of(st.none(), st.integers(0, 5))),
                matches=draw(st.integers(0, 3)), watched=draw(st.integers(0, 9)) == 0,
            ))
            if draw(st.integers(0, 3)) == 0:
                announced.add(grants[-1].opp_id)
    return grants, announced


@given(programs(), st.integers(0, 5), st.integers(1, 6))
def test_a_program_is_one_row_announced_once_any_posting_was(scenario, min_score, max_new):
    grants, announced = scenario
    config = NotifySettings(min_score=min_score, max_new=max_new, overview_day="never")
    person = Recipient(id=1, email="a@und.edu", unsubscribe_token="t")
    sent = [SentRecord(1, opp, NEW, -1, DAY0, None) for opp in announced]
    plan = plan_email(person, grants, sent, DAY0, config)
    scored = any(g.score is not None for g in grants)

    expected = []  # the member ids of each program that should be announced
    for prefix in sorted({g.opp_id.split("-")[0] for g in grants}):
        members = [g for g in grants if g.opp_id.startswith(prefix + "-")]
        if any(g.watched or g.opp_id in announced for g in members):
            continue
        best = max((g.score for g in members if g.score is not None), default=None) if scored else max(g.matches for g in members)
        if best is not None and best >= (min_score if scored else 1):
            expected.append({g.opp_id for g in members})
    listed = [{n.grant.opp_id, *(a.opp_id for a in n.grant.also)} for n in plan.new]
    assert sorted(listed, key=sorted) == sorted(expected, key=sorted)  # one row per program, no member twice

    email = render_email(plan, config, DAY0)
    shown = [n for n in email.shown if n.kind == NEW] if email else []
    assert len(shown) == min(max_new, len(expected))  # the cap counts programs
    recorded = [r.opp_id for r in email.records(1, DAY0) if r.kind == NEW] if email else []
    assert sorted(recorded) == sorted(i for n in shown for i in [n.grant.opp_id, *(a.opp_id for a in n.grant.also)])
    assert len(recorded) == len(set(recorded))  # every posting once
