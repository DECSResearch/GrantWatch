"""Member profiles and their scores in a real Postgres: the foreign keys do the cleaning up.

Removing a member, or their profile, needs no code of its own (resolution 16), and only a
database can show that. Each test starts from an empty database.
"""
from __future__ import annotations

import main
import psycopg2
import pytest

from grants.members import ProfileError, delete_member_profile, fetch_member_profile, save_member_profile
from grants.sql_utils import ensure_schema

ME, YOU = "me@und.edu", "you@und.edu"
TEXT = "I study wind-energy forecasting and the resilience of rural power grids."


def score(world, opp_id, email, value=4, key="k:p1"):
    world.query(
        "INSERT INTO member_scores (opp_id, email, score, reason, model, score_key) VALUES (%s, %s, %s, 'fits', 'm', %s)",
        (opp_id, email, value, key),
    )


@pytest.fixture
def team(fresh):
    """Two recipients, each with a profile and two scores."""
    ensure_schema()
    for email in (ME, YOU):
        assert main.main(["recipients", "add", email]) == 0
        save_member_profile(email, TEXT)
        score(fresh, "A-1", email)
        score(fresh, "A-2", email)
    return fresh


def counts(world):
    return (
        [row["email"] for row in world.query("SELECT email FROM member_profiles ORDER BY email")],
        [(row["email"], row["opp_id"]) for row in world.query("SELECT email, opp_id FROM member_scores ORDER BY email, opp_id")],
    )


def test_the_tables_are_created_again_without_harm(team):
    before = counts(team)
    ensure_schema()
    ensure_schema()
    assert counts(team) == before


def test_removing_a_member_removes_their_profile_and_scores_and_nobody_elses(team):
    assert main.main(["recipients", "remove", ME]) == 0
    assert counts(team) == ([YOU], [(YOU, "A-1"), (YOU, "A-2")])


def test_deleting_a_profile_removes_its_scores_and_keeps_the_member(team):
    assert delete_member_profile(ME) is True
    assert counts(team) == ([YOU], [(YOU, "A-1"), (YOU, "A-2")])
    assert [row["email"] for row in team.query("SELECT email FROM recipients ORDER BY email")] == [ME, YOU]
    assert delete_member_profile(ME) is False  # there is nothing left to delete


def test_unsubscribing_keeps_the_profile_and_its_scores(team):
    team.query("UPDATE recipients SET active = FALSE WHERE email = %s", (ME,))
    assert counts(team)[0] == [ME, YOU] and len(counts(team)[1]) == 4  # resubscribing restores them


def test_a_score_needs_a_profile_and_a_profile_needs_a_recipient(team):
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        score(team, "A-1", "nobody@und.edu")
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        team.query("INSERT INTO member_profiles (email, summary) VALUES ('nobody@und.edu', %s)", (TEXT,))
    with pytest.raises(psycopg2.errors.IntegrityError):
        save_member_profile("nobody@und.edu", TEXT)


def test_a_member_has_one_score_per_grant(team):
    with pytest.raises(psycopg2.errors.UniqueViolation):
        score(team, "A-1", ME)


class TestSavingAProfile:
    def test_a_changed_text_is_stored_and_the_old_scores_go_with_it(self, team):
        stored = save_member_profile(ME, "  I study   battery\n chemistry for grid storage, mostly lithium and sodium.  ")
        assert stored == "I study battery chemistry for grid storage, mostly lithium and sodium."
        assert fetch_member_profile(ME)["summary"] == stored
        assert counts(team) == ([ME, YOU], [(YOU, "A-1"), (YOU, "A-2")])  # theirs only

    def test_the_same_text_again_changes_nothing(self, team):
        before = fetch_member_profile(ME)
        assert save_member_profile(ME, "  " + TEXT + "\n") == TEXT
        assert fetch_member_profile(ME) == before  # updated_at too
        assert len(counts(team)[1]) == 4

    def test_a_text_that_is_refused_changes_nothing(self, team):
        with pytest.raises(ProfileError):
            save_member_profile(ME, "too short")
        assert fetch_member_profile(ME)["summary"] == TEXT and len(counts(team)[1]) == 4

    def test_the_first_profile_is_created(self, team):
        team.query("DELETE FROM member_profiles WHERE email = %s", (ME,))
        assert fetch_member_profile(ME) is None
        assert save_member_profile(ME, TEXT) == TEXT
        assert fetch_member_profile(ME)["summary"] == TEXT
