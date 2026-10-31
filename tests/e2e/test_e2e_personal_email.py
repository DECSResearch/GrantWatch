"""Flow 4: `notify` emails a member from their own scores and votes.

Real Postgres and the scripted model server. The fixtures run each step once and keep what it did, so a
test reads the result of a step and does not depend on the tests around it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main
from grants.members import personal_profile, save_member_profile
from grants.sql_utils import ensure_schema
from harness import MODEL
from llm_utils.relevance import personal_key
from notifications.planner import OwnScore, Personal
from notifications.store import PostgresStore
from web import app as webapp

ALICE, BOB, CAROL, DAN, ERIN = "alice@und.edu", "bob@und.edu", "carol@und.edu", "dan@und.edu", "erin@und.edu"
ALICE_TEXT = "I build battery models for grid-scale storage, mostly lithium and sodium chemistries."
ALICE_HAND_TYPED = "I build battery models for grid-scale storage,\nmostly lithium and sodium chemistries.  "  # ALICE_TEXT as typed into psql
BOB_TEXT = "I study wind-energy forecasting and the resilience of rural power grids to storms."


def key(text, model=MODEL):
    """The score key of a profile with this summary, made the way the scoring pass makes it."""
    return personal_key(personal_profile(webapp._profile(), text), model)


def add_score(world, email, opp_id, score, reason, score_key):
    world.query(
        "INSERT INTO member_scores (opp_id, email, score, reason, model, score_key) VALUES (%s, %s, %s, %s, %s, %s)",
        (opp_id, email, score, reason, MODEL, score_key),
    )


def vote(world, email, opp_id, verdict):
    world.query("INSERT INTO grant_feedback (opp_id, email, verdict) VALUES (%s, %s, %s)", (opp_id, email, verdict))


@pytest.fixture(scope="module")
def read(world):
    """Alice (typed by hand), Bob, Dan (a text too short to ever be scored) and Erin have profiles; Carol uses the team's.

    Rows are written straight into the tables, some for a key the member no longer has. The store is read, then read
    again with no model configured, then after Alice's profile text is changed behind the app's back.
    """
    world.reset()
    ensure_schema()
    for email in (ALICE, BOB, CAROL, DAN, ERIN):
        assert main.main(["recipients", "add", email]) == 0
    world.query("INSERT INTO member_profiles (email, summary) VALUES (%s, %s)", (ALICE, ALICE_HAND_TYPED))
    save_member_profile(BOB, BOB_TEXT)
    world.query("INSERT INTO member_profiles (email, summary) VALUES (%s, %s)", (DAN, "too short"))
    save_member_profile(ERIN, BOB_TEXT)
    add_score(world, ALICE, "E2E-BROAD", 5, "Alice fits", key(ALICE_TEXT))
    add_score(world, ALICE, "E2E-NOKW", 4, "scored by another model", key(ALICE_TEXT, "older-model"))
    add_score(world, ALICE, "E2E-TRUSTED", 3, "key made from the text as typed", key(ALICE_HAND_TYPED))
    add_score(world, BOB, "E2E-BROAD", 1, "Bob does not", key(BOB_TEXT))
    add_score(world, DAN, "E2E-BROAD", 5, "scored under a key nothing can match", key("some other text that was long enough"))
    vote(world, ALICE, "E2E-BROAD", -1)
    vote(world, BOB, "E2E-TRUSTED", 1)
    vote(world, CAROL, "E2E-NOKW", -1)
    vote(world, DAN, "E2E-NOKW", 1)
    world.query("UPDATE recipients SET active = FALSE WHERE email = %s", (ERIN,))
    store = PostgresStore()
    first = store.personal([ALICE, BOB, CAROL, DAN])
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("GRANTS_LLM_MODEL")
        no_model = store.personal([ALICE])
    world.query("UPDATE member_profiles SET summary = %s WHERE email = %s", (ALICE_TEXT + " I also work on solid-state electrolytes.", ALICE))
    edited = store.personal([ALICE])
    return SimpleNamespace(
        store=store, first=first, no_model=no_model, edited=edited, nobody=store.personal([]),
        recipients={r.email: r for r in store.recipients()},
    )


class TestTheStoreRead:
    def test_a_recipient_with_a_profile_of_their_own_is_marked_and_the_others_are_not(self, read):
        assert {email: r.has_profile for email, r in read.recipients.items()} == {ALICE: True, BOB: True, CAROL: False, DAN: True}

    def test_an_unsubscribed_member_is_not_a_recipient(self, read):
        assert ERIN not in read.recipients

    def test_only_the_scores_for_the_profile_the_member_has_now_count(self, read):
        assert read.first[ALICE].scores == {"E2E-BROAD": OwnScore(5, "Alice fits")}
        assert read.first[BOB].scores == {"E2E-BROAD": OwnScore(1, "Bob does not")}

    def test_the_key_is_made_from_the_cleaned_text_even_when_the_table_holds_it_as_typed(self, read):
        assert key(ALICE_HAND_TYPED) != key(ALICE_TEXT)
        assert "E2E-BROAD" in read.first[ALICE].scores  # a row under the cleaned text's key counts
        assert "E2E-TRUSTED" not in read.first[ALICE].scores  # one under the typed text's key is not this member's key

    def test_a_member_is_read_with_their_own_votes_only(self, read):
        assert read.first[ALICE].votes == {"E2E-BROAD": -1}
        assert read.first[BOB].votes == {"E2E-TRUSTED": 1}

    def test_an_address_with_no_profile_is_left_out(self, read):
        assert CAROL not in read.first and set(read.first) == {ALICE, BOB, DAN}
        assert read.nobody == {}

    def test_a_text_that_cannot_be_cleaned_has_votes_and_no_scores(self, read):
        assert read.first[DAN] == Personal(scores={}, votes={"E2E-NOKW": 1})

    def test_with_no_model_configured_no_score_matches_but_the_votes_are_kept(self, read):
        assert read.no_model[ALICE] == Personal(scores={}, votes={"E2E-BROAD": -1})

    def test_a_profile_changed_behind_the_apps_back_leaves_its_old_scores_unmatched(self, read):
        assert read.edited[ALICE] == Personal(scores={}, votes={"E2E-BROAD": -1})
