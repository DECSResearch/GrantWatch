"""A member with a profile of their own votes for themselves only: the SQL behind that, in a real Postgres.

The unit suite checks the statements against an in-memory twin; this checks that Postgres accepts them
and that the team's counts, verdicts, scorer examples and email plan leave the member out, count them
again when their profile is deleted, and never touch the member's own verdict.
"""
from __future__ import annotations

import main
import pytest

from grants.members import delete_member_profile, save_member_profile
from grants.sql_utils import ensure_schema, fetch_member_votes, fetch_relevant_ids, fetch_vote_counts, fetch_votes
from grants.feedback import Votes
from notifications.store import PostgresStore

TEXT = "I study wind-energy forecasting and the resilience of rural power grids."
TEAMMATE, ME, HER = "team@und.edu", "me@und.edu", "her@und.edu"


@pytest.fixture
def voted(fresh):
    """Grant A (votes: team +1, me -1, her -1) and B (me +1 alone). ``me`` and ``her`` have no profile yet."""
    ensure_schema()
    for email in (TEAMMATE, ME, HER):
        main.main(["recipients", "add", email])
    fresh.query("INSERT INTO grants (opp_id, title, agency_code, close_date) VALUES ('A', 'Grant A', 'NSF', NOW() + INTERVAL '30 days'), ('B', 'Grant B', 'DOE', NOW() + INTERVAL '40 days')")
    for opp, email, verdict in (("A", TEAMMATE, 1), ("A", ME, -1), ("A", HER, -1), ("B", ME, 1)):
        fresh.query("INSERT INTO grant_feedback (opp_id, email, verdict, title) VALUES (%s, %s, %s, %s)", (opp, email, verdict, f"Grant {opp}"))
    return fresh


def team_nets():
    return {g.opp_id: g.net_votes for g in PostgresStore().grants()}


def test_before_anyone_has_a_profile_every_vote_is_the_teams(voted):
    assert {(v["opp_id"], v["email"]) for v in fetch_votes()} == {("A", TEAMMATE), ("A", ME), ("A", HER), ("B", ME)}
    assert fetch_vote_counts(["A", "B"], ME) == {"A": Votes(1, 2, -1), "B": Votes(1, 0, 1)}
    assert fetch_relevant_ids() == ["B"]
    assert team_nets() == {"A": -1, "B": 1}


def test_a_members_votes_leave_the_team_but_not_their_own_verdict(voted):
    save_member_profile(ME, TEXT)
    assert {(v["opp_id"], v["email"]) for v in fetch_votes()} == {("A", TEAMMATE), ("A", HER)}
    counts = fetch_vote_counts(["A", "B"], ME)
    assert counts == {"A": Votes(1, 1, -1), "B": Votes(0, 0, 1)}  # their -1 and +1 are not counted, and still theirs
    assert fetch_vote_counts(["A", "B"], HER)["A"] == Votes(1, 1, -1) and fetch_vote_counts(["A", "B"], TEAMMATE)["B"].mine is None
    assert fetch_relevant_ids() == []  # B was liked by me alone
    assert team_nets() == {"A": 0, "B": 0}


def test_the_members_votes_are_theirs_to_learn_from(voted):
    save_member_profile(ME, TEXT)
    assert {(v["opp_id"], v["email"], v["verdict"]) for v in fetch_member_votes([ME])} == {("A", ME, -1), ("B", ME, 1)}
    assert {v["email"] for v in fetch_member_votes([ME, HER])} == {ME, HER}
    assert set(fetch_member_votes([ME])[0]) == set(fetch_votes()[0])  # the same columns, so the scorer reads either
    assert fetch_member_votes([]) == [] and fetch_member_votes(["nobody@und.edu"]) == []


def test_a_member_on_their_own_does_not_hide_a_grant_for_the_team(voted):
    save_member_profile(ME, TEXT)
    save_member_profile(HER, TEXT)
    assert team_nets() == {"A": 1, "B": 0}  # only the teammate's +1 on A is the team's


def test_deleting_the_profile_returns_the_votes_to_the_team(voted):
    save_member_profile(ME, TEXT)
    assert team_nets() == {"A": 0, "B": 0}
    assert delete_member_profile(ME) is True
    assert team_nets() == {"A": -1, "B": 1}
    assert {(v["opp_id"], v["email"]) for v in fetch_votes()} == {("A", TEAMMATE), ("A", ME), ("A", HER), ("B", ME)}


def test_changing_a_profiles_text_does_not_move_their_votes(voted):
    save_member_profile(ME, TEXT)
    save_member_profile(ME, TEXT + " Also battery chemistry.")
    assert team_nets() == {"A": 0, "B": 0} and len(fetch_member_votes([ME])) == 2
