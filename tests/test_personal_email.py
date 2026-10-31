"""A member with a profile of their own is emailed from their own scores and votes.

Examples run through run_notify with an in-memory store; the properties below them check the rules
over random grants, programs, scores and votes.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, timedelta
from typing import Dict, List, Optional

from hypothesis import HealthCheck, given, settings, strategies as st

from notifications.notify import plan_all, run_notify
from notifications.planner import (
    CHANGED, NEW, OVERVIEW, OWN, PENDING, REMINDER, REMOVED, TEAM, UNREAD,
    GrantState, NotifySettings, OwnScore, Personal, Recipient, SentRecord, mode_of, personalise, plan_email,
)
from support import CaptureTransport, MemoryStore, plain_text, sections
from test_digest import _grant

MONDAY = date(2026, 9, 14)
TEAM_R = Recipient(id=1, email="team@und.edu", unsubscribe_token="t1")
ALICE = Recipient(id=2, email="alice@und.edu", unsubscribe_token="t2")
BOB = Recipient(id=3, email="bob@und.edu", unsubscribe_token="t3")
QUIET = NotifySettings(overview_day="never")
NEW_TITLE = "New grants that fit"


def own(scores=None, votes=None) -> Personal:
    """A member's own data: ``scores`` maps a grant to a score or a (score, reason); ``votes`` to 1 or -1."""
    return Personal(
        scores={k: v if isinstance(v, OwnScore) else OwnScore(*v) if isinstance(v, tuple) else OwnScore(v) for k, v in (scores or {}).items()},
        votes=dict(votes or {}),
    )


def _run(store, out, day=MONDAY, config=QUIET):
    return run_notify(store=store, transport=out, today=day, settings=config)


def mails(out: CaptureTransport) -> Dict[str, object]:
    return {m["To"]: m for m in out.messages}


def program(*scores, rule=()):
    """One program posted by several services: D0, D1, ... with these team scores (None for unscored)."""
    return [
        replace(_grant(f"D{i}", 20, score=0 if i in rule else score, by_rule=i in rule), title=f"Grant D{i} (DURIP)", agency_code=f"DOD-{i}")
        for i, score in enumerate(scores)
    ]


class TestTheirOwnScores:
    def test_their_score_and_reason_replace_the_teams(self):
        grants = [_grant("A", 40, score=5), _grant("B", 41, score=1)]
        store = MemoryStore([TEAM_R, ALICE], grants, {ALICE.email: own({"A": (2, "no fit for me"), "B": (5, "fits me well")})})
        out = CaptureTransport()
        assert _run(store, out) == 0
        got = mails(out)
        assert sections(got[TEAM_R.email])[NEW_TITLE] == ["A"]
        assert sections(got[ALICE.email])[NEW_TITLE] == ["B"]
        text = plain_text(got[ALICE.email])
        assert "[5/5] Grant B" in text and "fits me well" in text and "why B" not in text
        assert "[5/5] Grant A" in plain_text(got[TEAM_R.email]) and "fits me well" not in plain_text(got[TEAM_R.email])

    def test_their_threshold_is_the_one_on_the_recipient_as_for_anyone(self):
        picky = replace(ALICE, min_score=5)
        store = MemoryStore([picky], [_grant("FOUR", 40, score=1), _grant("FIVE", 41, score=1)], {picky.email: own({"FOUR": 4, "FIVE": 5})})
        out = CaptureTransport()
        _run(store, out)
        assert sections(out.messages[-1])[NEW_TITLE] == ["FIVE"]

    def test_a_grant_with_no_score_for_them_is_not_announced_until_it_has_one(self):
        grants = [_grant("A", 40, score=5), _grant("LATE", 40, score=5)]
        store = MemoryStore([TEAM_R, ALICE], grants, {ALICE.email: own({"A": 5})})
        out = CaptureTransport()
        _run(store, out)
        assert sections(mails(out)[TEAM_R.email])[NEW_TITLE] == ["A", "LATE"] and sections(mails(out)[ALICE.email])[NEW_TITLE] == ["A"]
        store.personal_data[ALICE.email] = own({"A": 5, "LATE": (4, "scored overnight")})
        _run(store, out, MONDAY + timedelta(days=1))
        assert sections(out.messages[-1])[NEW_TITLE] == ["LATE"] and out.messages[-1]["To"] == ALICE.email

    def test_a_rules_zero_wins_over_a_score_stored_for_them(self):
        ruled_out = replace(_grant("R", 40, score=0, by_rule=True), reason="Only states may apply")
        store = MemoryStore([ALICE], [ruled_out, _grant("A", 41, score=1)], {ALICE.email: own({"R": 5, "A": 5})})
        out = CaptureTransport()
        _run(store, out)
        assert sections(out.messages[-1])[NEW_TITLE] == ["A"]
        assert personalise([ruled_out], own({"R": 5}))[0] == ruled_out

    def test_the_keyword_fallback_is_never_theirs_even_when_their_scores_sit_on_grants_that_are_gone(self):
        # Nothing open is scored by the team, so the team falls back to keyword matches. A member whose
        # only scores are for a closed grant is still judged by score, and a keyword match is not one.
        grants = [_grant("OPEN", 40, score=None, matches=3), _grant("OLD", -5, score=5)]
        store = MemoryStore([TEAM_R, ALICE], grants, {ALICE.email: own({"OLD": 5})})
        out = CaptureTransport()
        _run(store, out)
        assert list(mails(out)) == [TEAM_R.email] and sections(out.messages[-1])[NEW_TITLE] == ["OPEN"]
        assert [r.opp_id for r in store.rows if r.recipient_id == ALICE.id] == []
        assert plan_email(replace(ALICE, has_profile=True), grants, [], MONDAY, QUIET, own({"OLD": 5})).scoring_active
        assert not plan_email(TEAM_R, grants, [], MONDAY, QUIET).scoring_active

    def test_a_grant_they_marked_relevant_waits_for_a_score_of_theirs_and_then_it_carries_it_past_the_threshold(self):
        store = MemoryStore([ALICE], [_grant("A", 40, score=5), _grant("MARKED", 41, score=5)], {ALICE.email: own({"A": 5}, {"MARKED": 1})})
        out = CaptureTransport()
        _run(store, out)
        assert sections(out.messages[-1]) == {NEW_TITLE: ["A"]}
        store.personal_data[ALICE.email] = own({"A": 5, "MARKED": 1}, {"MARKED": 1})
        _run(store, out, MONDAY + timedelta(days=1))
        assert sections(out.messages[-1]) == {NEW_TITLE: ["MARKED"]}

    def test_the_overview_leaves_out_a_grant_with_no_score_of_theirs_unless_it_is_tracked(self):
        grants = [_grant("A", 20, score=5), _grant("UNSCORED", 21, score=5), _grant("TRACKED", 50, score=5, watched=True)]
        store = MemoryStore([ALICE], grants, {ALICE.email: own({"A": 5}, {"UNSCORED": 1})})
        store.rows += [SentRecord(ALICE.id, g.opp_id, NEW, -1, g.close_date, None) for g in grants]  # told already, so only the overview lists them
        out = CaptureTransport()
        _run(store, out, config=NotifySettings(overview_day="daily"))
        assert sections(out.messages[-1]) == {"Closes in 8 to 30 days": ["A"], "Closes in 31 to 60 days": ["TRACKED"]}


class TestTheirOwnVotes:
    def test_their_votes_decide_for_them_and_the_teams_do_not(self):
        grants = [
            _grant("X", 40, score=5, net_votes=2),  # the team says Relevant; their score is low
            _grant("Y", 40, score=5, net_votes=-1),  # the team says Not for us; they score it high
            _grant("Z", 40, score=5),  # they score it high and said Not for us
            _grant("W", 40, score=5),  # they score it low and said Relevant
            _grant("V", 40, score=5),  # they said Relevant and it has no score of theirs
        ]
        mine = own({"X": 1, "Y": 5, "Z": 5, "W": 1}, {"Z": -1, "W": 1, "V": 1})
        store = MemoryStore([TEAM_R, ALICE], grants, {ALICE.email: mine})
        out = CaptureTransport()
        _run(store, out)
        got = mails(out)
        assert sorted(sections(got[TEAM_R.email])[NEW_TITLE]) == ["V", "W", "X", "Z"]  # Y is the team's Not for us
        assert sorted(sections(got[ALICE.email])[NEW_TITLE]) == ["W", "Y"]

    def test_their_vote_applies_while_nothing_is_scored_for_them_and_they_see_the_teams_scores(self):
        grants = [_grant("A", 40, score=5), _grant("X", 40, score=5), _grant("T", 40, score=5, net_votes=-1)]
        store = MemoryStore([ALICE], grants, {ALICE.email: own(votes={"X": -1})})
        out = CaptureTransport()
        _run(store, out)
        assert sorted(sections(out.messages[-1])[NEW_TITLE]) == ["A", "T"]  # not X: their Not for us; T: the team's mark is not theirs

    def test_what_a_member_marked_never_changes_another_recipients_email(self):
        grants = [_grant("A", 40, score=5), _grant("B", 40, score=5)]
        a, b = MemoryStore([TEAM_R, ALICE], grants, {ALICE.email: own({"A": 5, "B": 5}, {"B": -1})}), MemoryStore([TEAM_R, ALICE], grants, {ALICE.email: own({"A": 5, "B": 5})})
        first, second = CaptureTransport(), CaptureTransport()
        _run(a, first), _run(b, second)
        assert sections(mails(first)[TEAM_R.email]) == sections(mails(second)[TEAM_R.email]) == {NEW_TITLE: ["A", "B"]}
        assert sections(mails(first)[ALICE.email]) == {NEW_TITLE: ["A"]} and sections(mails(second)[ALICE.email]) == {NEW_TITLE: ["A", "B"]}


class TestAProgramsLead:
    def test_the_row_is_led_by_the_posting_their_scores_prefer_and_every_posting_is_recorded(self):
        postings = program(5, 3)
        store = MemoryStore([TEAM_R, ALICE], postings, {ALICE.email: own({"D0": 4, "D1": 5})})
        out = CaptureTransport()
        _run(store, out)
        got = mails(out)
        assert sections(got[TEAM_R.email])[NEW_TITLE] == ["D0 (DURIP)"] and sections(got[ALICE.email])[NEW_TITLE] == ["D1 (DURIP)"]
        for who in (TEAM_R, ALICE):
            assert sorted(r.opp_id for r in store.rows if r.recipient_id == who.id and r.kind == NEW) == ["D0", "D1"]
        assert "[5/5] Grant D1 (DURIP)" in plain_text(got[ALICE.email]) and "Also posted by: NSF (https://x/D0)" in plain_text(got[ALICE.email])

    def test_it_is_announced_once_per_program_after_scores_arrive_for_another_posting(self):
        postings = program(5, 3, 3)
        store = MemoryStore([ALICE], postings, {ALICE.email: own({"D0": 5})})
        out = CaptureTransport()
        _run(store, out)
        store.personal_data[ALICE.email] = own({"D0": 5, "D1": 5, "D2": 5})
        _run(store, out, MONDAY + timedelta(days=1))
        assert len(out.messages) == 1 and sorted(r.opp_id for r in store.rows) == ["D0", "D1", "D2"]

    def test_the_row_is_a_posting_their_profile_scored_when_an_unscored_one_comes_first(self):
        # D0 is ruled out and leads the group (its 0 ties their 0 on D2 and its number is lower); D1 is unscored
        # and sorts before D2 among the others. At a threshold of 0 the program qualifies through D2, so D2 is the row.
        postings = program(None, None, None, rule=(0,))
        store = MemoryStore([ALICE], postings, {ALICE.email: own({"D2": 0})})
        out = CaptureTransport()
        _run(store, out, config=NotifySettings(overview_day="never", min_score=0))
        assert sections(out.messages[-1])[NEW_TITLE] == ["D2 (DURIP)"]
        assert sorted(r.opp_id for r in store.rows) == ["D0", "D1", "D2"]


def joint_call():
    """A (the team scores it 5, their profile has not yet) and B, which the eligibility rules ruled out, in one program."""
    a = replace(_grant("A", 20, score=5), title="Joint call (JC)", agency_code="X-1")
    b = replace(_grant("B", 20, score=0, by_rule=True), title="Joint call (JC)", agency_code="X-2", reason="rule says no")
    return [a, b]


class TestARuledOutPostingNeverStandsForAnUnscoredOne:
    """B's rule 0 ranks above A's missing score, but a program whose only candidate is unscored for them is not announced."""

    def test_such_a_program_is_held_back_and_not_recorded_even_when_they_marked_it_relevant(self):
        store = MemoryStore([ALICE], [*joint_call(), _grant("OTHER", 25, score=1)], {ALICE.email: own({"OTHER": 5}, {"A": 1})})
        out = CaptureTransport()
        config = NotifySettings(overview_day="never", min_score=4)
        _run(store, out, config=config)
        assert sections(out.messages[-1]) == {NEW_TITLE: ["OTHER"]} and "rule says no" not in plain_text(out.messages[-1])
        assert sorted(r.opp_id for r in store.rows) == ["OTHER"]
        store.personal_data[ALICE.email] = own({"OTHER": 5, "A": (5, "fits her")}, {"A": 1})
        _run(store, out, MONDAY + timedelta(days=1), config=config)
        text = plain_text(out.messages[-1])
        assert sections(out.messages[-1]) == {NEW_TITLE: ["Joint call (JC)"]} and "fits her" in text and "Also posted by: NSF (https://x/B)" in text
        assert sorted(r.opp_id for r in store.rows) == ["A", "B", "OTHER"]

    def test_so_is_it_at_a_threshold_of_zero_with_no_vote_at_all(self):
        store = MemoryStore([ALICE], [*joint_call(), _grant("OTHER", 25, score=1)], {ALICE.email: own({"OTHER": 5})})
        out = CaptureTransport()
        config = NotifySettings(overview_day="never", min_score=0)
        _run(store, out, config=config)
        assert sections(out.messages[-1]) == {NEW_TITLE: ["OTHER"]} and sorted(r.opp_id for r in store.rows) == ["OTHER"]
        store.personal_data[ALICE.email] = own({"OTHER": 5, "A": 0})
        _run(store, out, MONDAY + timedelta(days=1), config=config)
        text = plain_text(out.messages[-1])
        assert sections(out.messages[-1]) == {NEW_TITLE: ["Joint call (JC)"]} and "Also posted by: NSF (https://x/B)" in text and "rule says no" not in text

    def test_the_overview_leaves_it_out_until_a_posting_of_theirs_is_scored(self):
        grants = [*joint_call(), _grant("OTHER", 25, score=1)]
        store = MemoryStore([ALICE], grants, {ALICE.email: own({"OTHER": 5}, {"A": 1})})
        store.rows += [SentRecord(ALICE.id, g.opp_id, NEW, -1, g.close_date, None) for g in grants]  # told already, so only the overview lists them
        out = CaptureTransport()
        config = NotifySettings(overview_day="daily", min_score=4)
        _run(store, out, config=config)
        assert sections(out.messages[-1]) == {"Closes in 8 to 30 days": ["OTHER"]}
        store.personal_data[ALICE.email] = own({"OTHER": 5, "A": 5}, {"A": 1})
        _run(store, out, MONDAY + timedelta(days=1), config=config)
        assert sections(out.messages[-1]) == {"Closes in 8 to 30 days": ["Joint call (JC)", "OTHER"]}
        assert "Also posted by: NSF (https://x/B)" in plain_text(out.messages[-1])

    def test_a_program_of_ruled_out_postings_only_keeps_a_ruled_out_row_at_a_threshold_of_zero(self):
        ruled_out = [replace(g, score=0, by_rule=True, reason="rule says no") for g in joint_call()]
        store = MemoryStore([ALICE], ruled_out, {ALICE.email: own({"Z": 5})})
        out = CaptureTransport()
        _run(store, out, config=NotifySettings(overview_day="never", min_score=0))
        assert sections(out.messages[-1]) == {NEW_TITLE: ["Joint call (JC)"]} and "[0/5] Joint call (JC)" in plain_text(out.messages[-1])


class TestUntilTheFirstScore:
    def test_a_member_with_no_score_yet_gets_the_teams_scores(self):
        store = MemoryStore([ALICE], [replace(_grant("A", 40, score=5), reason="team reason")], {ALICE.email: own()})
        out = CaptureTransport()
        _run(store, out)
        assert sections(out.messages[-1])[NEW_TITLE] == ["A"] and "team reason" in plain_text(out.messages[-1])

    def test_it_ends_with_the_first_score_stored_for_them(self):
        store = MemoryStore([ALICE], [_grant("A", 40, score=5), _grant("B", 41, score=5)], {ALICE.email: own({"B": 5})})
        out = CaptureTransport()
        _run(store, out)
        assert sections(out.messages[-1])[NEW_TITLE] == ["B"]

    def test_the_mode_says_which_it_is(self):
        assert mode_of(TEAM_R, None) == TEAM and mode_of(TEAM_R, own({"A": 5})) == TEAM
        member = replace(ALICE, has_profile=True)
        assert mode_of(member, own({"A": 5})) == OWN and mode_of(member, own()) == PENDING and mode_of(member, None) == UNREAD


class TestWhenTheirDataCannotBeRead:
    def settings(self):
        return NotifySettings(overview_day="monday", reminder_days=[30, 14, 7, 2])

    def world(self):
        grants = [
            _grant("TRACKED", 6, score=5, watched=True),
            _grant("GONE", 50, score=5, watched=True, listed=False),
            _grant("FRESH", 40, score=5),
        ]
        store = MemoryStore([TEAM_R, ALICE], grants, {ALICE.email: own({"FRESH": 5, "TRACKED": 5})})
        store.personal_fails = True
        return store

    def test_a_member_gets_only_reminders_and_withdrawn_notices_and_the_team_gets_everything(self):
        store, out = self.world(), CaptureTransport()
        assert _run(store, out, config=self.settings()) == 0
        got = mails(out)
        assert set(sections(got[TEAM_R.email])) >= {NEW_TITLE, "Deadline reminders", "Tracked grants no longer open"}
        assert sections(got[ALICE.email]) == {"Deadline reminders": ["TRACKED"], "Tracked grants no longer open": ["GONE"]}

    def test_nothing_is_recorded_for_their_new_grants_or_overview_so_the_next_good_run_still_has_them(self):
        store, out = self.world(), CaptureTransport()
        _run(store, out, config=self.settings())
        assert {r.kind for r in store.rows if r.recipient_id == ALICE.id} == {REMINDER, REMOVED}
        assert {r.kind for r in store.rows if r.recipient_id == TEAM_R.id} >= {NEW, OVERVIEW}
        store.personal_fails = False
        _run(store, out, MONDAY + timedelta(days=1), config=self.settings())
        assert sections(mails(out)[ALICE.email])[NEW_TITLE] == ["FRESH"] and {r.kind for r in store.rows if r.recipient_id == ALICE.id} >= {NEW, OVERVIEW}

    def test_a_team_recipients_email_is_what_it_is_when_the_read_works(self):
        broken, fine = self.world(), self.world()
        fine.personal_fails = False
        first, second = CaptureTransport(), CaptureTransport()
        _run(broken, first, config=self.settings()), _run(fine, second, config=self.settings())
        assert plain_text(mails(first)[TEAM_R.email]) == plain_text(mails(second)[TEAM_R.email])

    def test_no_score_is_shown_beside_what_it_does_send(self):
        store, out = self.world(), CaptureTransport()
        _run(store, out, config=self.settings())
        assert "[unscored] Grant TRACKED" in plain_text(mails(out)[ALICE.email]) and "[5/5] Grant TRACKED" in plain_text(mails(out)[TEAM_R.email])

    def test_it_is_logged_and_the_run_still_succeeds(self, caplog):
        store, out = self.world(), CaptureTransport()
        with caplog.at_level(logging.WARNING, logger="grantwatch"):
            assert _run(store, out, config=self.settings()) == 0
        assert "could not read the own scores and votes of 1 member" in caplog.text and "member_scores cannot be read" in caplog.text

    def test_a_member_whose_profile_vanished_between_the_reads_is_treated_the_same(self):
        class Vanished(MemoryStore):
            def personal(self, emails):
                return {}

        store, out = Vanished([ALICE], [_grant("FRESH", 40, score=5)], {ALICE.email: own({"FRESH": 5})}), CaptureTransport()
        _run(store, out)
        assert out.messages == [] and store.rows == []


class TestTheReadIsOnePerRun:
    def test_all_members_are_read_in_one_call_and_a_team_only_run_reads_nothing(self):
        store = MemoryStore([TEAM_R, ALICE, BOB], [_grant("A", 40, score=5)], {ALICE.email: own({"A": 5}), BOB.email: own({"A": 5})})
        _run(store, CaptureTransport())
        assert store.personal_reads == [[ALICE.email, BOB.email]]
        team_only = MemoryStore([TEAM_R], [_grant("A", 40, score=5)])
        _run(team_only, CaptureTransport())
        assert team_only.personal_reads == []


# --- properties -----------------------------------------------------------------------------------------

SLOW = dict(deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


@st.composite
def pools(draw):
    """Programs of one to three postings: some closed, unlisted or tracked, some ruled out, with team scores and votes."""
    grants: List[GrantState] = []
    for i in range(draw(st.integers(1, 7))):
        close = draw(st.one_of(st.none(), st.integers(-4, 70)))
        names = draw(st.booleans())
        for j in range(draw(st.integers(1, 3))):
            rule = draw(st.integers(0, 5)) == 0
            grants.append(GrantState(
                opp_id=f"P{i}-{j}", title=f"Program {i} (PRG{chr(65 + i)})" if names else f"Solo {i}-{j}", agency=f"Service {j}", agency_code=f"AG-{j}",
                url=f"https://example.org/{i}/{j}", close_date=None if close is None else MONDAY + timedelta(days=close),
                score=0 if rule else draw(st.one_of(st.none(), st.integers(0, 5))), by_rule=rule, reason=f"team reason {i}-{j}",
                matches=draw(st.integers(0, 3)), watched=draw(st.integers(0, 9)) == 0, listed=draw(st.integers(0, 9)) != 0,
                net_votes=draw(st.integers(-2, 2)),
            ))
    return grants


@st.composite
def personals(draw, grants, scored=True):
    """A member's own data for these grants: scores on a random subset, rows for grants that no longer exist, votes."""
    ids = [g.opp_id for g in grants]
    gone = [g.opp_id for g in grants if not g.is_open(MONDAY)]
    chosen = draw(st.sets(st.sampled_from(ids + gone + ["OLD-1", "OLD-2"]), max_size=len(ids) + 2)) if scored else set()
    only_gone = draw(st.booleans())  # scores only on grants that are not open: the case that must not fall back to keywords
    if only_gone:
        chosen &= set(gone + ["OLD-1", "OLD-2"])
    scores = {opp: OwnScore(draw(st.integers(0, 5)), f"own reason {opp}") for opp in sorted(chosen)}
    votes = {opp: draw(st.sampled_from([1, -1])) for opp in draw(st.sets(st.sampled_from(ids), max_size=len(ids)))}
    return Personal(scores, votes)


def settings_for(min_score, max_new=30, overview="daily"):
    return NotifySettings(min_score=min_score, max_new=max_new, overview_day=overview, keyword_threshold=1, dashboard_url="https://gw.example.edu")


def stands_for_their_program(row, mine):
    """Whether the posting a row shows is one their profile scored, or a rule's 0 in a program of ruled-out postings only.

    The other postings of the program ride along behind it unscored, as "Also posted by". A ruled-out posting may not
    stand for a program that has a posting which is not ruled out, since that posting is the one they have no score for.
    """
    return (row.opp_id in mine.scores and not row.by_rule) or all(p.by_rule for p in (row, *row.also))


class TestProperties:
    @settings(max_examples=150, **SLOW)
    @given(data=st.data(), pool=pools(), min_score=st.integers(0, 5), a_has_profile=st.booleans())
    def test_a_plan_never_depends_on_another_members_scores_or_votes(self, data, pool, min_score, a_has_profile):
        pa = data.draw(personals(pool))
        b_before, b_after = data.draw(personals(pool)), data.draw(personals(pool))
        b_profile_before, b_profile_after = data.draw(st.booleans()), data.draw(st.booleans())
        a = replace(ALICE, min_score=data.draw(st.one_of(st.none(), st.integers(0, 5))))

        def email_for(b_data):
            people = {a.email: pa} if a_has_profile else {}
            if b_data is not None:
                people[BOB.email] = b_data
            store = MemoryStore([TEAM_R, a, BOB], pool, people)
            return plan_all(store, store.recipients(), MONDAY, settings_for(min_score))[a.id]

        assert email_for(b_before if b_profile_before else None) == email_for(b_after if b_profile_after else None)

    @settings(max_examples=250, **SLOW)
    @given(data=st.data(), pool=pools(), min_score=st.integers(0, 5))
    def test_nothing_unscored_for_a_member_is_ever_announced_to_them_and_what_is_shown_is_theirs(self, data, pool, min_score):
        mine = data.draw(personals(pool))
        member = replace(ALICE, has_profile=True, min_score=data.draw(st.one_of(st.none(), st.integers(0, 5))))
        plan = plan_email(member, pool, [], MONDAY, settings_for(min_score), mine)
        assert plan.profile == (OWN if mine.scores else PENDING)
        if not mine.scores:
            return
        assert plan.scoring_active  # never the keyword fallback, however few of their scores are on open grants
        shown = [n.grant for n in plan.new] + [g for _, members in plan.overview for g in members]
        for g in shown:
            if not g.by_rule and g.opp_id in mine.scores:
                assert g.score == mine.scores[g.opp_id].score and g.reason == mine.scores[g.opp_id].reason
            if g.by_rule:
                assert g.score == 0 and g.reason == f"team reason {g.opp_id[1:]}"  # the rule's, whatever is stored
            assert g.net_votes == mine.votes.get(g.opp_id, 0)
        for g in [n.grant for n in plan.new]:
            assert g.score is not None
        for notice in plan.new:
            assert not notice.grant.watched and stands_for_their_program(notice.grant, mine)
        for _, members in plan.overview:
            for g in members:
                assert g.watched or stands_for_their_program(g, mine)  # a tracked grant is the team's, scored or not

    @settings(max_examples=200, **SLOW)
    @given(data=st.data(), pool=pools(), min_score=st.integers(0, 5))
    def test_when_the_read_fails_team_emails_are_unchanged_and_members_get_no_new_grants_or_overview(self, data, pool, min_score):
        mine = data.draw(personals(pool))
        people = {ALICE.email: mine}
        good, bad = MemoryStore([TEAM_R, ALICE], pool, people), MemoryStore([TEAM_R, ALICE], pool, people)
        bad.personal_fails = True
        config = settings_for(min_score)
        fine, broken = plan_all(good, good.recipients(), MONDAY, config), plan_all(bad, bad.recipients(), MONDAY, config)
        assert fine[TEAM_R.id] == broken[TEAM_R.id]
        email = broken[ALICE.id]
        if email is not None:
            assert {row.kind for row in email.records(ALICE.id, MONDAY)} <= {REMINDER, CHANGED, REMOVED}
            assert not email.overview_shown and all(n.kind != NEW for n in email.shown)
            assert all(n.grant.score is None or n.grant.by_rule for n in email.shown)

    @settings(max_examples=200, **SLOW)
    @given(data=st.data(), pool=pools())
    def test_the_overlay_changes_only_scores_reasons_and_votes_and_keeps_a_rules_zero(self, data, pool):
        mine = data.draw(personals(pool))
        for personal in (mine, Personal(votes=mine.votes), None):
            out = personalise(pool, personal)
            assert [g.opp_id for g in out] == [g.opp_id for g in pool]
            for before, after in zip(pool, out):
                assert replace(after, score=before.score, reason=before.reason, net_votes=before.net_votes) == before
                assert after.net_votes == (0 if personal is None else personal.votes.get(before.opp_id, 0))
                if before.by_rule:
                    assert (after.score, after.reason) == (before.score, before.reason)
                elif personal is None:
                    assert (after.score, after.reason) == (None, "")
                elif not personal.scores:
                    assert (after.score, after.reason) == (before.score, before.reason)
                else:
                    own_score = personal.scores.get(before.opp_id)
                    assert (after.score, after.reason) == ((own_score.score, own_score.reason) if own_score else (None, ""))

    @settings(max_examples=150, **SLOW)
    @given(data=st.data(), count=st.integers(1, 12), min_score=st.integers(0, 5))
    def test_a_member_is_told_exactly_the_grants_their_own_scores_and_votes_qualify_and_each_once(self, data, count, min_score):
        grants = [_grant(f"S{i:02d}", data.draw(st.integers(-3, 90)), score=data.draw(st.one_of(st.none(), st.integers(0, 5))),
                         by_rule=(rule := data.draw(st.integers(0, 6)) == 0), watched=data.draw(st.integers(0, 8)) == 0, net_votes=data.draw(st.integers(-1, 1)))
                  for i in range(count)]
        grants = [replace(g, score=0) if g.by_rule else g for g in grants]
        mine = data.draw(personals(grants))
        member = replace(ALICE, min_score=None)
        store = MemoryStore([member], grants, {member.email: mine})
        out = CaptureTransport()
        for _ in range(count + 3):  # enough runs to drain whatever is qualifying, then it goes quiet
            before = len(out.messages)
            run_notify(store=store, transport=out, today=MONDAY, settings=settings_for(min_score, max_new=4, overview="never"))
            if len(out.messages) == before:
                break
        told = [r.opp_id for r in store.rows if r.kind == NEW]
        assert len(told) == len(set(told))
        if not mine.scores:
            return  # the team's scores: not this property
        expected = set()
        for g in grants:
            score = 0 if g.by_rule else (mine.scores[g.opp_id].score if g.opp_id in mine.scores else None)
            vote = mine.votes.get(g.opp_id, 0)
            if g.is_open(MONDAY) and not g.watched and score is not None and vote >= 0 and (score >= min_score or vote > 0):
                expected.add(g.opp_id)
        assert set(told) == expected
