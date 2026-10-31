"""The personal step: members take turns scoring their own profile, chunk by chunk, until done or stopped."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Dict, List

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from grants import personal
from grants.members import personal_profile
from grants.personal import CHUNK, Member, Totals, score_people, why_skipped
from grants.profile import Profile
from llm_utils.relevance import LLMSettings, ScoringOutcome, close_order, personal_key

TEAM = Profile(summary="The team studies cybersecurity.", institution="UND", applicant_type="Public university")
LLM = LLMSettings(model="model-one", batch_size=3, workers=1, max_per_run=1500)
DAY0 = datetime(2026, 11, 1)
OUTAGE = "cannot reach http://127.0.0.1:1: Connection error."
TOKENS = "the model used all 2048 completion tokens before answering (a reasoning model thinking too long)"
SLOW = dict(deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


def member(name: str) -> Member:
    return Member(f"{name}@und.edu", f"{name} studies a particular corner of power-grid research, in detail.")


def record(i: int, days=None):
    """Candidate number G<i>; closes ``days`` after DAY0 (or on a day chosen from i), or never when days is -1."""
    days = (i * 7) % 90 if days is None else days
    return {"OPPORTUNITY_NUMBER": f"G{i:03d}", "OPPORTUNITY_TITLE": f"Grant {i}", "CLOSE_DATE": None if days < 0 else (DAY0 + timedelta(days=days)).strftime("%m/%d/%Y")}


class World:
    """Everything the loop touches outside itself: a clock, a model, the vote table and the member_scores table."""

    def __init__(self, members, fail=(), aborts=None, mute=(), clock_step=1.0, saves=None):
        self.members = list(members)
        self.now = 0.0
        self.clock_step = clock_step
        self.calls: List[SimpleNamespace] = []  # one per chunk: who, what, when it began
        self.db: Dict[tuple, str] = {}  # (email, number) -> score key
        self.fail = set(fail)  # numbers the model never answers
        self.aborts = dict(aborts or {})  # email -> the abort reason their chunks end with
        self.mute = set(mute)  # emails the model gives no answer at all
        self.saves = saves  # a function (member, key, entries) -> rows written, to replace the default
        self.votes_asked: List[list] = []
        self.by_summary = {m.summary: m.email for m in members}

    def clock(self):
        return self.now

    def score(self, records, profile, *, already_scored, settings, client, feedback, personal):
        email = self.by_summary[profile.summary]
        assert len(self.calls) < 5000, "the loop never ends"
        self.calls.append(SimpleNamespace(email=email, numbers=[r["OPPORTUNITY_NUMBER"] for r in records], began=self.now, profile=profile,
                                          already_scored=already_scored, settings=settings, feedback=feedback, personal=personal))
        self.now += self.clock_step
        answered = [r for r in records if r["OPPORTUNITY_NUMBER"] not in self.fail and email not in self.mute]
        entries = {r["OPPORTUNITY_NUMBER"]: {"score": 3, "summary": "s", "reason": "r", "model": settings.model, "scored_at": "2026-11-01T00:00:00+00:00"} for r in answered}
        return ScoringOutcome(records=[], enabled=True, scored=len(entries), failed=len(records) - len(entries), new_entries=entries, abort_reason=self.aborts.get(email))

    def write(self, who, key, entries):
        if self.saves:
            return self.saves(who, key, entries)
        for number in entries:
            self.db[(who.email, number)] = key
        return len(entries)

    def scored_numbers(self, email, key):
        return {number for (who, number), stored in self.db.items() if who == email and stored == key}

    def votes_of(self, emails):
        self.votes_asked.append(list(emails))
        return [{"email": e, "opp_id": "V"} for e in emails]

    def run(self, candidates, deadline=float("inf"), chunk=CHUNK):
        return score_people(self.members, candidates, TEAM, LLM, deadline=deadline, clock=self.clock, score=self.score, scored_numbers=self.scored_numbers,
                            votes_of=self.votes_of, write=self.write, chunk=chunk)

    def taken_by(self, email):
        return [number for call in self.calls if call.email == email for number in call.numbers]


@st.composite
def passes(draw):
    """People, their candidates and how much of it each already has under the current key."""
    n_people = draw(st.integers(1, 5))
    n_grants = draw(st.integers(0, 120))
    members = [member(f"p{i}") for i in range(n_people)]
    candidates = [record(i, draw(st.integers(-1, 90))) for i in range(n_grants)]
    done = {m.email: set(draw(st.sets(st.sampled_from([r["OPPORTUNITY_NUMBER"] for r in candidates]), max_size=n_grants))) if candidates else set() for m in members}
    return members, candidates, done, draw(st.integers(1, 25))


def seeded(world, candidates, done):
    for m in world.members:
        key = personal_key(personal_profile(TEAM, m.summary), LLM.model)
        for number in done.get(m.email, ()):
            world.db[(m.email, number)] = key


class TestTurns:
    def test_every_pending_grant_is_scored_for_every_member_and_nothing_twice(self):
        world = World([member("a"), member("b")])
        candidates = [record(i) for i in range(130)]
        totals = world.run(candidates)
        assert totals == Totals(people=2, scored=260, failed=0, pending=0, stopped=None)
        assert sorted(world.taken_by("a@und.edu")) == sorted(world.taken_by("b@und.edu")) == sorted(r["OPPORTUNITY_NUMBER"] for r in candidates)
        assert [len(call.numbers) for call in world.calls] == [60, 60, 60, 60, 10, 10]  # a, b, a, b, a, b

    def test_a_member_scores_with_the_scorer_as_it_is_and_only_their_own_votes(self):
        world = World([member("a"), member("b")])
        world.run([record(1), record(2)])
        for call in world.calls:
            assert call.personal is True and call.already_scored is None and call.settings.max_per_run == len(call.numbers)
            assert call.profile == personal_profile(TEAM, next(m.summary for m in world.members if m.email == call.email))
            assert call.profile.institution == "UND" and call.feedback == [{"email": call.email, "opp_id": "V"}]
        assert world.votes_asked == [["a@und.edu"], ["b@und.edu"]]  # once each, however many turns

    def test_each_turn_is_soonest_deadline_first_and_undated_last(self):
        candidates = [record(0, 50), record(1, -1), record(2, 3), record(3, 3), record(4, 20)]
        world = World([member("a")])
        world.run(candidates, chunk=2)
        assert [call.numbers for call in world.calls] == [["G002", "G003"], ["G004", "G000"], ["G001"]]

    def test_what_is_already_scored_under_the_current_key_is_not_pending_but_an_older_key_is(self):
        a = member("a")
        world = World([a])
        current = personal_key(personal_profile(TEAM, a.summary), LLM.model)
        world.db[("a@und.edu", "G000")] = current
        world.db[("a@und.edu", "G001")] = "old-fingerprint:v2:model-one:p1"  # for the text they had before
        totals = world.run([record(0), record(1), record(2)])
        assert world.taken_by("a@und.edu") == ["G001", "G002"] and totals.scored == 2
        assert world.db[("a@und.edu", "G001")] == current  # rescored in place

    def test_a_record_listed_twice_or_without_a_number_is_scored_once_or_not_at_all(self):
        world = World([member("a")])
        world.run([record(1), record(1), {"CLOSE_DATE": None}, {"OPPORTUNITY_NUMBER": ""}])
        assert world.taken_by("a@und.edu") == ["G001"]

    def test_nobody_with_nothing_pending_takes_a_turn(self):
        world = World([member("a")])
        assert world.run([]) == Totals(people=1)
        assert world.calls == [] and world.votes_asked == []

    def test_a_grant_the_model_never_answers_is_tried_once_a_run_and_left_pending(self):
        world = World([member("a")], fail={"G001"})
        totals = world.run([record(i) for i in range(5)], chunk=2)
        assert world.taken_by("a@und.edu").count("G001") == 1
        assert totals == Totals(people=1, scored=4, failed=1, pending=1, stopped=None)
        assert ("a@und.edu", "G001") not in world.db
        assert World([member("a")]).run([record(1)]).pending == 0  # and the next run is a fresh try

    def test_the_people_who_are_given_take_turns_in_the_order_given(self):
        world = World([member("b"), member("a")])
        world.run([record(i) for i in range(4)], chunk=2)
        assert [call.email for call in world.calls] == ["b@und.edu", "a@und.edu", "b@und.edu", "a@und.edu"]


class TestStops:
    def test_no_turn_begins_at_or_after_the_deadline_and_the_rest_stays_pending(self):
        world = World([member("a"), member("b")])
        totals = world.run([record(i) for i in range(100)], deadline=3.0, chunk=10)
        assert len(world.calls) == 3 and all(call.began < 3.0 for call in world.calls)
        assert totals == Totals(people=2, scored=30, failed=0, pending=170, stopped="time limit")
        assert len(world.db) == 30

    def test_a_deadline_that_has_already_passed_starts_nothing(self):
        world = World([member("a")])
        assert world.run([record(1)], deadline=0.0) == Totals(people=1, scored=0, failed=0, pending=1, stopped="time limit")
        assert world.calls == []

    def test_the_time_limit_is_not_reported_when_nothing_was_left_to_do(self):
        world = World([member("a")])
        assert world.run([record(1)], deadline=1.0).stopped is None  # the one turn began at 0.0 and finished the work

    def test_an_outage_stops_everyone_after_saving_what_that_chunk_got(self):
        world = World([member("a"), member("b"), member("c")], aborts={"b@und.edu": OUTAGE}, fail={"G001"})
        totals = world.run([record(i) for i in range(5)], chunk=2)
        assert [call.email for call in world.calls] == ["a@und.edu", "b@und.edu"]  # c never began, a did not go again
        assert totals.stopped == f"the model is unavailable: {OUTAGE}"
        assert ("b@und.edu", "G000") in world.db and ("c@und.edu", "G000") not in world.db  # what b's chunk did get was saved
        assert totals.pending == 5 * 3 - totals.scored and totals.people == 3

    def test_a_failed_login_is_an_outage_too(self):
        world = World([member("a"), member("b")], aborts={"a@und.edu": "authentication failed: bad key"})
        totals = world.run([record(1)])
        assert len(world.calls) == 1 and totals.stopped.startswith("the model is unavailable: authentication failed")

    def test_any_other_abort_stops_only_that_member(self):
        world = World([member("a"), member("b")], aborts={"a@und.edu": TOKENS})
        totals = world.run([record(i) for i in range(5)], chunk=2)
        assert world.taken_by("a@und.edu") == ["G000", "G001"]  # their chunk was saved, then they stopped
        assert len(world.taken_by("b@und.edu")) == 5 and ("a@und.edu", "G000") in world.db
        assert totals.scored == 2 + 5 and totals.pending == 3
        assert totals.stopped == f"1 of 2 members stopped early: {TOKENS}"

    def test_a_chunk_nothing_came_back_for_stops_that_member_only(self, caplog):
        world = World([member("a"), member("b")], mute={"a@und.edu"})
        with caplog.at_level(logging.WARNING, logger="grantwatch"):
            totals = world.run([record(i) for i in range(5)], chunk=2)
        assert len(world.taken_by("a@und.edu")) == 2 and len(world.taken_by("b@und.edu")) == 5
        assert totals == Totals(people=2, scored=5, failed=2, pending=5, stopped="1 of 2 members stopped early: the model gave no usable answer for a chunk of 2")
        assert "gave no usable answer" in caplog.text

    def test_a_scorer_that_raises_stops_that_member_only(self):
        world = World([member("a"), member("b")])
        real = world.score

        def score(records, profile, **kw):
            if profile.summary == member("a").summary:
                raise RuntimeError("client broke")
            return real(records, profile, **kw)

        totals = score_people(world.members, [record(i) for i in range(3)], TEAM, LLM, deadline=1e9, clock=world.clock, score=score,
                              scored_numbers=world.scored_numbers, votes_of=world.votes_of, write=world.write, chunk=2)
        assert totals == Totals(people=2, scored=3, failed=2, pending=3, stopped="1 of 2 members stopped early: scoring failed: client broke")

    def test_a_write_that_fails_stops_that_member_and_counts_the_chunk_as_failed(self):
        def saves(who, key, entries):
            if who.email == "a@und.edu":
                raise RuntimeError("disk full")
            return len(entries)

        world = World([member("a"), member("b")], saves=saves)
        totals = world.run([record(i) for i in range(4)], chunk=2)
        assert totals == Totals(people=2, scored=4, failed=2, pending=4, stopped="1 of 2 members stopped early: could not save scores: disk full")

    def test_a_profile_deleted_or_edited_mid_run_writes_nothing_and_stops_that_member(self):
        world = World([member("a"), member("b")], saves=lambda who, key, entries: 0 if who.email == "a@und.edu" else len(entries))
        totals = world.run([record(i) for i in range(4)], chunk=2)
        assert len(world.taken_by("a@und.edu")) == 2
        assert totals.scored == 4 and totals.stopped.endswith("their profile was removed or changed while scoring")

    def test_votes_that_cannot_be_read_do_not_stop_scoring(self, caplog):
        world = World([member("a")])
        world.votes_of = lambda emails: (_ for _ in ()).throw(RuntimeError("database down"))
        with caplog.at_level(logging.WARNING, logger="grantwatch"):
            totals = world.run([record(1)])
        assert totals.scored == 1 and world.calls[0].feedback == [] and "Could not read a@und.edu's votes" in caplog.text


class TestProperties:
    @settings(max_examples=60, **SLOW)
    @given(passes(), st.one_of(st.none(), st.integers(0, 40)))
    def test_among_people_with_pending_grants_chunk_counts_never_differ_by_more_than_one(self, drawn, stop_after):
        members, candidates, done, chunk = drawn
        world = World(members)
        seeded(world, candidates, done)
        pending = {m.email: len({r["OPPORTUNITY_NUMBER"] for r in candidates} - done[m.email]) for m in members}
        world.run(candidates, deadline=float("inf") if stop_after is None else float(stop_after), chunk=chunk)
        counts = {m.email: 0 for m in members}
        left = dict(pending)
        for call in world.calls:  # replay: after every chunk, those who still have grants to score have had nearly equal turns
            counts[call.email] += 1
            left[call.email] -= len(call.numbers)
            waiting = [counts[email] for email in counts if left[email] > 0]
            assert not waiting or max(waiting) - min(waiting) <= 1

    @settings(max_examples=60, **SLOW)
    @given(passes(), st.integers(0, 30))
    def test_the_stop_starts_no_chunk_late_and_leaves_the_rest_pending_for_the_next_run(self, drawn, stop_after):
        members, candidates, done, chunk = drawn
        world = World(members)
        seeded(world, candidates, done)
        everything = {r["OPPORTUNITY_NUMBER"] for r in candidates}
        todo = {m.email: everything - done[m.email] for m in members}
        needed = sum(-(-len(numbers) // chunk) for numbers in todo.values())

        first = world.run(candidates, deadline=float(stop_after), chunk=chunk)
        assert len(world.calls) == min(stop_after, needed) and all(call.began < stop_after for call in world.calls)
        assert first.stopped == ("time limit" if stop_after < needed else None)
        written = first.scored
        assert first.scored + first.pending == sum(len(numbers) for numbers in todo.values())  # written plus pending is everything
        assert first.failed == 0 and written == sum(len(call.numbers) for call in world.calls)
        taken_first = {(call.email, number) for call in world.calls for number in call.numbers}
        assert len(taken_first) == written  # nothing twice

        calls_before = len(world.calls)
        second = world.run(candidates, chunk=chunk)  # the next run, with no deadline, finishes what is left and only that
        taken_second = {(call.email, number) for call in world.calls[calls_before:] for number in call.numbers}
        assert not taken_first & taken_second
        assert second.pending == 0 and second.stopped is None and second.scored == first.pending
        assert taken_first | taken_second == {(email, number) for email, numbers in todo.items() for number in numbers}

    @settings(max_examples=60, **SLOW)
    @given(passes(), st.integers(0, 12))
    def test_an_outage_ends_the_pass_for_everyone_whenever_it_comes(self, drawn, nth):
        members, candidates, done, chunk = drawn
        world = World(members)
        seeded(world, candidates, done)
        real = world.score

        def score(records, profile, **kw):
            out = real(records, profile, **kw)
            if len(world.calls) == nth + 1:
                out.abort_reason = OUTAGE
            return out

        totals = score_people(members, candidates, TEAM, LLM, deadline=1e9, clock=world.clock, score=score, scored_numbers=world.scored_numbers,
                              votes_of=world.votes_of, write=world.write, chunk=chunk)
        assert len(world.calls) <= nth + 1
        if len(world.calls) == nth + 1:
            assert totals.stopped == f"the model is unavailable: {OUTAGE}"

    @settings(max_examples=40, **SLOW)
    @given(st.lists(st.integers(0, 90), max_size=40), st.integers(1, 10))
    def test_a_members_turns_take_their_grants_in_close_date_order(self, days, chunk):
        candidates = [record(i, d) for i, d in enumerate(days)]
        world = World([member("a")])
        world.run(candidates, chunk=chunk)
        by_number = {r["OPPORTUNITY_NUMBER"]: r for r in candidates}
        assert [by_number[n] for n in world.taken_by("a@und.edu")] == sorted(candidates, key=close_order)


class TestWhySkipped:
    ON = dict(ingested=True, settings=LLM, team=TEAM, stats={}, candidates=[record(1)])

    def test_a_run_that_can_proceed_is_not_skipped(self):
        assert why_skipped(**self.ON) is None

    @pytest.mark.parametrize("change,text", [
        ({"ingested": False}, "ingest did not finish"),
        ({"settings": LLMSettings(model="")}, "no model is configured"),
        ({"team": Profile(summary="REPLACE ME")}, "the team profile is not configured"),
        ({"stats": {"scoring_abort": "authentication failed: bad key"}}, "the team's scoring stopped: authentication failed: bad key"),
        ({"candidates": None}, "ingest sent no grants to the model"),
        ({"candidates": []}, "ingest sent no grants to the model"),
    ])
    def test_each_reason(self, change, text):
        assert text in why_skipped(**{**self.ON, **change})

    @given(st.text(min_size=1, max_size=40))
    def test_any_abort_the_team_pass_set_skips_the_step(self, abort):
        assert why_skipped(**{**self.ON, "stats": {"scoring_abort": abort}}) is not None


class TestSettingsAndSentences:
    def test_the_stop_is_65_minutes_unless_set(self, monkeypatch):
        monkeypatch.delenv("GRANTS_PERSONAL_STOP_MINUTES", raising=False)
        assert personal.stop_minutes() == 65
        monkeypatch.setenv("GRANTS_PERSONAL_STOP_MINUTES", " 30 ")
        assert personal.stop_minutes() == 30
        monkeypatch.setenv("GRANTS_PERSONAL_STOP_MINUTES", "0")
        assert personal.stop_minutes() == 0
        monkeypatch.setenv("GRANTS_PERSONAL_STOP_MINUTES", "-5")
        assert personal.stop_minutes() == 0

    def test_a_setting_that_is_not_a_number_falls_back_with_a_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("GRANTS_PERSONAL_STOP_MINUTES", "an hour")
        with caplog.at_level(logging.WARNING, logger="grantwatch"):
            assert personal.stop_minutes() == 65
        assert "GRANTS_PERSONAL_STOP_MINUTES" in caplog.text

    def test_the_run_log_sentence(self):
        assert personal.personal_detail(Totals()) == "No member has a profile of their own yet."
        assert personal.personal_detail(Totals(people=1, scored=12, pending=0)) == "1 member: 12 newly scored, 0 still pending."
        assert personal.personal_detail(Totals(people=3, scored=1200, failed=4, pending=2500, stopped="time limit")) == \
            "3 members: 1,200 newly scored (4 failed), 2,500 still pending. Stopped: time limit."

    def test_the_totals_are_five_plain_numbers_and_a_reason(self):
        assert Totals(people=2, scored=3, failed=1, pending=4, stopped="x").as_dict() == {"people": 2, "scored": 3, "failed": 1, "pending": 4, "stopped": "x"}


class TestRun:
    def test_the_deadline_counts_from_the_start_of_the_process(self, monkeypatch):
        monkeypatch.setattr(personal, "fetch_members", lambda: [member("a")])
        seen = {}
        monkeypatch.setattr(personal, "score_people", lambda members, candidates, team, settings, *, deadline, clock: seen.update(deadline=deadline) or Totals(people=1))
        monkeypatch.setenv("GRANTS_PERSONAL_STOP_MINUTES", "2")
        personal.run([record(1)], TEAM, LLM, started=1000.0)
        assert seen["deadline"] == 1120.0

    def test_with_no_member_profiles_it_does_nothing(self, monkeypatch):
        monkeypatch.setattr(personal, "fetch_members", lambda: [])
        monkeypatch.setattr(personal, "score_people", lambda *a, **k: pytest.fail("nobody to score"))
        assert personal.run([record(1)], TEAM, LLM, started=0.0) == Totals()
