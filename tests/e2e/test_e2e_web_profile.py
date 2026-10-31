"""Flow 4: a member writes, changes and removes their own profile on the dashboard routes, over a real database.

The `story` fixture plays the steps once, in order, and keeps what each saw; the tests read those results.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from grants.members import personal_profile
from harness import MODEL
from llm_utils.relevance import personal_key
from web import app as webapp

ANA, BEN = "ana@und.edu", "ben@und.edu"
FIRST = "I build battery models for grid-scale storage, mostly lithium and sodium chemistries."
TYPED = "  I build battery models for grid-scale storage,\n\tmostly lithium and sodium chemistries.  "  # FIRST, as a textarea sends it
SECOND = "I study wind-energy forecasting and the resilience of rural power grids to storms."


def key(text):
    return personal_key(personal_profile(webapp._profile(), text), MODEL)


def rows(world, sql, *params):
    return world.query(sql, params)


@pytest.fixture(scope="module")
def story(world):
    world.reset()
    assert world.ingest() == 0
    ana, ben = (TestClient(webapp.app, base_url="http://localhost") for _ in range(2))
    world.sign_in(ana, ANA)
    world.sign_in(ben, BEN)
    visitor = TestClient(webapp.app, base_url="http://localhost")
    s = SimpleNamespace(ana=ana)

    s.visitor = [visitor.get("/api/me/profile"), visitor.put("/api/me/profile", json={"summary": FIRST}), visitor.delete("/api/me/profile")]
    s.before = ana.get("/api/me/profile").json()
    s.before_me = ana.get("/api/profile").json()["me"]

    s.refused = {
        "short": ana.put("/api/me/profile", json={"summary": "too short"}),
        "long": ana.put("/api/me/profile", json={"summary": "word " * 500}),
        "placeholder": ana.put("/api/me/profile", json={"summary": "REPLACE ME with what you work on in a sentence or two"}),
        "plain": ana.put("/api/me/profile", content=f'{{"summary": "{FIRST}"}}', headers={"Content-Type": "text/plain"}),
        "form": ana.put("/api/me/profile", data={"summary": FIRST}),
    }
    s.after_refused = rows(world, "SELECT * FROM member_profiles")

    s.first = ana.put("/api/me/profile", json={"summary": TYPED})
    s.stored = rows(world, "SELECT summary, updated_at FROM member_profiles WHERE email = %s", ANA)
    s.read = ana.get("/api/me/profile").json()
    s.me_unscored = ana.get("/api/profile").json()["me"]

    # The daily run scores it: here, rows written straight into the table under the key of the cleaned text.
    for opp, value in (("E2E-TRUSTED", 5), ("E2E-NOKW", 2)):
        world.query("INSERT INTO member_scores (opp_id, email, score, reason, model, score_key) VALUES (%s, %s, %s, 'r', 'm', %s)", (opp, ANA, value, key(FIRST)))
    s.me_scored = ana.get("/api/profile").json()["me"]
    s.list_scored = ana.get("/api/grants?limit=1000").json()["scores"]

    # Ana's vote while she has a profile is hers alone; Ben, on the team profile, votes too.
    assert ana.post("/api/feedback", json={"opp_id": "E2E-BROAD", "verdict": -1}).status_code == 200
    assert ben.post("/api/feedback", json={"opp_id": "E2E-BROAD", "verdict": 1}).status_code == 200
    s.ben_sees_with_profile = {r["opp_id"]: r for r in ben.get("/api/grants?limit=1000").json()["results"]}["E2E-BROAD"]

    s.same = ana.put("/api/me/profile", json={"summary": FIRST})  # the same text again changes nothing
    s.scores_after_same = rows(world, "SELECT opp_id FROM member_scores WHERE email = %s ORDER BY opp_id", ANA)

    s.edit = ana.put("/api/me/profile", json={"summary": SECOND})
    s.scores_after_edit = rows(world, "SELECT opp_id FROM member_scores WHERE email = %s", ANA)
    s.me_edited = ana.get("/api/profile").json()["me"]
    s.list_edited = ana.get("/api/grants?limit=1000").json()["scores"]

    world.query("INSERT INTO member_scores (opp_id, email, score, reason, model, score_key) VALUES ('E2E-TRUSTED', %s, 4, 'r', 'm', %s)", (ANA, key(SECOND)))
    s.delete = ana.delete("/api/me/profile")
    s.after_delete = rows(world, "SELECT (SELECT COUNT(*) FROM member_profiles WHERE email = %s) AS profiles, (SELECT COUNT(*) FROM member_scores WHERE email = %s) AS scores", ANA, ANA)[0]
    s.me_deleted = ana.get("/api/profile").json()["me"]
    s.list_deleted = ana.get("/api/grants?limit=1000").json()["scores"]
    s.delete_again = ana.delete("/api/me/profile")
    s.ben_sees_after = {r["opp_id"]: r for r in ben.get("/api/grants?limit=1000").json()["results"]}["E2E-BROAD"]

    # A removed member's cookie stops working.
    world.sign_in(ana, ANA)
    assert main.main(["recipients", "remove", ANA]) == 0
    s.removed = ana.put("/api/me/profile", json={"summary": FIRST})
    return s


class TestSignedOut:
    def test_a_visitor_can_read_write_or_remove_nothing(self, story):
        assert [reply.status_code for reply in story.visitor] == [401, 401, 401]


class TestWriting:
    def test_a_member_on_the_team_profile_has_none_and_is_counted_so(self, story):
        assert story.before == {"has_profile": False, "summary": None, "updated_at": None}
        assert story.before_me["has_profile"] is False and story.before_me["using"] == "team"

    def test_a_text_that_cannot_be_stored_is_refused_with_the_reason_and_stores_nothing(self, story):
        assert [story.refused[name].status_code for name in ("short", "long", "placeholder")] == [400, 400, 400]
        assert "at least 40 characters" in story.refused["short"].json()["detail"]
        assert "2,000 characters or fewer" in story.refused["long"].json()["detail"]
        assert "Replace the placeholder" in story.refused["placeholder"].json()["detail"]
        assert story.after_refused == []

    def test_a_body_that_is_not_declared_json_is_refused_whatever_it_holds(self, story):
        assert story.refused["plain"].status_code == 415 and story.refused["form"].status_code in (415, 422)
        assert story.after_refused == []

    def test_a_text_is_stored_cleaned_and_read_back(self, story):
        assert story.first.status_code == 200
        assert [row["summary"] for row in story.stored] == [FIRST]
        assert story.first.json()["summary"] == FIRST and story.read == {**story.first.json()}
        assert story.read["has_profile"] is True and story.read["updated_at"]

    def test_with_a_profile_and_no_score_yet_they_are_on_the_teams_scores(self, story):
        assert story.me_unscored["has_profile"] is True and story.me_unscored["using"] == "team"
        assert story.me_unscored["coverage"]["scored"] == 1  # only the grant the pre-check ruled out

    def test_once_a_score_is_stored_they_are_on_their_own(self, story):
        assert story.me_scored["using"] == "mine" and story.list_scored == "mine"
        assert story.me_scored["coverage"]["scored"] == 3

    def test_their_vote_is_theirs_alone_until_the_profile_is_removed(self, story):
        row = story.ben_sees_with_profile
        assert (row["votes_up"], row["votes_down"], row["team_verdict"], row["my_verdict"]) == (1, 0, 1, 1)  # Ana's Not for us is not the team's

    def test_the_same_text_again_keeps_their_scores(self, story):
        assert story.same.status_code == 200 and [r["opp_id"] for r in story.scores_after_same] == ["E2E-NOKW", "E2E-TRUSTED"]

    def test_a_changed_text_deletes_their_scores_and_they_are_back_on_the_teams_until_the_next_run(self, story):
        assert story.edit.status_code == 200 and story.edit.json()["summary"] == SECOND
        assert story.scores_after_edit == []
        assert story.me_edited["using"] == "team" and story.list_edited == "team" and story.me_edited["coverage"]["scored"] == 1


class TestRemoving:
    def test_it_returns_them_to_the_team_profile_and_removes_the_scores(self, story):
        assert story.delete.status_code == 200 and story.delete.json() == {"has_profile": False, "summary": None, "updated_at": None, "removed": True}
        assert story.after_delete == {"profiles": 0, "scores": 0}
        assert story.me_deleted["has_profile"] is False and story.me_deleted["using"] == "team" and story.list_deleted == "team"

    def test_removing_it_again_says_there_was_nothing(self, story):
        assert story.delete_again.status_code == 200 and story.delete_again.json()["removed"] is False

    def test_their_votes_count_for_the_team_again(self, story):
        row = story.ben_sees_after
        assert (row["votes_up"], row["votes_down"], row["team_verdict"], row["hidden"]) == (1, 1, 0, False)  # one each way: no verdict

    def test_a_removed_member_cannot_use_an_old_cookie(self, story):
        assert story.removed.status_code == 401
