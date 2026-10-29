"""Properties of the email service, checked over simulated weeks of daily runs.

Each example invents a set of grants whose state changes from day to day
(scores arrive late or never, deadlines move, grants drop out of the extract,
tracked grants come and go), sends some days fail, and some days run twice.
Every run goes through run_notify with an in-memory store, and the emails are
read back as text. The properties are the spec:

1. A grant is announced to a recipient at most once.
2. Each run announces min(backlog, max_new) of the best unannounced
   qualifying grants, so nothing qualifying is lost; repeated runs drain the
   backlog and then go quiet.
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
from notifications.planner import NEW, REMINDER, CHANGED, GrantState, NotifySettings, Recipient, plan_email
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

    def on(self, day: int) -> GrantState:
        close = self.close
        if close is not None and self.move_day is not None and day >= self.move_day:
            close += self.move_by
        return GrantState(
            opp_id=self.opp_id,
            title=f"Grant {self.opp_id}",
            agency="NSF",
            url=f"https://example.org/{self.opp_id}",
            close_date=DAY0 + timedelta(days=close) if close is not None else None,
            archive_date=DAY0 + timedelta(days=self.archive) if self.archive is not None else None,
            score=self.score if day >= self.score_day else None,
            reason="fits",
            watched=self.watched_from is not None and day >= self.watched_from,
            listed=self.drop_day is None or day < self.drop_day,
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
        max_new=draw(st.integers(1, 10)),
        dashboard_url="https://gw.example.edu",
    )
    return grants, people, days, failures, reruns, config


def _eligible(grants: List[GrantState], person: Recipient, config: NotifySettings, today: date, announced: set) -> List[GrantState]:
    """Independent statement of rule 2's backlog (not the planner's code)."""
    threshold = config.min_score if person.min_score is None else person.min_score
    open_now = [g for g in grants if g.is_open(today)]
    if not any(g.score is not None for g in open_now):
        return []
    return [g for g in open_now if not g.watched and g.score is not None and g.score >= threshold and g.opp_id not in announced]


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
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
                shown = [g for g in backlog[person.id] if g.opp_id in new_ids]
                left = [g for g in backlog[person.id] if g.opp_id not in new_ids]
                assert min(g.score for g in shown) >= max([g.score for g in left] or [0])
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


@settings(max_examples=60, deadline=None)
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
