"""Flow 4: a member with a profile of their own is shown their own scores, over a real database.

The `story` fixture ingests the fixture extract, gives four members different profiles and scores
(written straight into the tables, some under a key their profile no longer has) and reads the API
as each of them. Every test reads what a step saw.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from grants.members import personal_profile, save_member_profile
from grants.sql_utils import open_grant_sql
from harness import MODEL
from llm_utils.relevance import personal_key
from web import app as webapp

ANA, BEN, CARA, DAN = "ana@und.edu", "ben@und.edu", "cara@und.edu", "dan@und.edu"
ANA_CLEAN = "I build battery models for grid-scale storage, mostly lithium and sodium chemistries."
ANA_HAND_TYPED = "I build battery models for grid-scale storage,\nmostly lithium and sodium chemistries.  "  # as typed into psql
BEN_TEXT = "I study wind-energy forecasting and the resilience of rural power grids to storms."
NOT_YET = "Not scored for your profile yet"
LOW = "E2E-NOCODES"  # the team's model scores it 3, under the team threshold of 4


def key(text, model=MODEL):
    """The score key of a profile with this summary, made the way the scoring pass makes it."""
    return personal_key(personal_profile(webapp._profile(), text), model)


def score(world, email, opp_id, value, reason, score_key):
    world.query(
        "INSERT INTO member_scores (opp_id, email, score, reason, model, score_key) VALUES (%s, %s, %s, %s, 'm', %s)",
        (opp_id, email, value, reason, score_key),
    )


def by_id(listing):
    return {row["opp_id"]: row for row in listing["results"]}


@pytest.fixture(scope="module")
def story(world):
    world.reset()
    assert world.ingest() == 0
    for email in (ANA, BEN, CARA, DAN):
        world.sign_in(TestClient(webapp.app, base_url="http://localhost"), email)  # makes them recipients
    world.query("INSERT INTO member_profiles (email, summary) VALUES (%s, %s)", (ANA, ANA_HAND_TYPED))
    save_member_profile(BEN, BEN_TEXT)
    save_member_profile(DAN, "I study how zebras cross the savanna, with satellite tags.")
    # Ana: three scores for the profile she has now, one for the text as typed (a key nothing else makes), one under another model.
    score(world, ANA, "E2E-NOKW", 2, "Ana: the team likes it, she does not", key(ANA_CLEAN))
    score(world, ANA, "E2E-TRUSTED", 5, "Ana: trusted systems fit", key(ANA_CLEAN))
    score(world, ANA, "E2E-BROAD", 4, "Ana: broad but fits", key(ANA_CLEAN))
    score(world, ANA, "E2E-BULLETS", 5, "Ana: scored from the text as typed", key(ANA_HAND_TYPED))
    score(world, ANA, "E2E-ENTITY", 3, "Ana: scored by an older model", key(ANA_CLEAN, "older-model"))
    score(world, BEN, "E2E-NOKW", 1, "Ben: no", key(ANA_CLEAN))  # Ana's key, not his: it does not count for him
    s = SimpleNamespace()
    s.open = world.query(f"SELECT COUNT(*) AS n FROM grants g WHERE {open_grant_sql('g')}")[0]["n"]
    s.ruled_out = [row["opp_id"] for row in world.query(f"SELECT g.opp_id FROM grants g WHERE g.relevance_model = 'rule' AND {open_grant_sql('g')}")]
    s.team = {row["opp_id"]: row for row in world.query("SELECT opp_id, relevance_score, relevance_reason FROM grants")}
    clients = {}
    for email in (ANA, BEN, CARA, DAN):
        clients[email] = TestClient(webapp.app, base_url="http://localhost")
        world.sign_in(clients[email], email)
    s.clients = clients

    ana, cara, dan = clients[ANA], clients[CARA], clients[DAN]
    s.ana_list = ana.get("/api/grants?limit=1000").json()
    s.ana_floor = ana.get("/api/grants?min_score=4&limit=1000").json()
    s.ana_profile = ana.get("/api/profile").json()
    s.ben_profile = clients[BEN].get("/api/profile").json()
    s.cara_list = cara.get("/api/grants?limit=1000").json()
    s.cara_profile = cara.get("/api/profile").json()
    s.dan_list = dan.get("/api/grants?limit=1000").json()
    s.dan_profile = dan.get("/api/profile").json()
    s.detail = {"ana": ana.get("/api/grants/E2E-NOKW").json(), "cara": cara.get("/api/grants/E2E-NOKW").json(), "dan": dan.get("/api/grants/E2E-NOKW").json()}

    # Ana marks Relevant a grant she has no score for and the team scores 3, under the floor of 4, and Not for us one she likes;
    # Dan, who has a profile and no score yet, marks one Not for us.
    s.cara_floor = cara.get("/api/grants?min_score=4&limit=1000").json()
    assert ana.post("/api/feedback", json={"opp_id": LOW, "verdict": 1}).status_code == 200
    assert ana.post("/api/feedback", json={"opp_id": "E2E-TRUSTED", "verdict": -1}).status_code == 200
    assert dan.post("/api/feedback", json={"opp_id": "E2E-NOKW", "verdict": -1}).status_code == 200
    s.ana_voted = ana.get("/api/grants?limit=1000").json()
    s.ana_floor_voted = ana.get("/api/grants?min_score=4&limit=1000").json()
    s.cara_voted = cara.get("/api/grants?limit=1000").json()
    s.dan_voted = dan.get("/api/grants?limit=1000").json()
    s.cara_floor_voted = cara.get("/api/grants?min_score=4&limit=1000").json()
    return s


class TestTheirOwnScores:
    def test_a_member_with_a_profile_and_scores_is_shown_their_own(self, story):
        assert story.ana_list["scores"] == "mine" and story.ana_profile["me"]["using"] == "mine"
        row = by_id(story.ana_list)["E2E-NOKW"]
        assert (row["relevance_score"], row["relevance_reason"], row["relevance_model"]) == (2, "Ana: the team likes it, she does not", "m")
        assert story.team["E2E-NOKW"]["relevance_score"] == 5  # the team's score is another

    def test_an_older_score_is_shown_until_it_is_replaced_whatever_key_it_has(self, story):
        rows = by_id(story.ana_list)
        assert (rows["E2E-BULLETS"]["relevance_score"], rows["E2E-ENTITY"]["relevance_score"]) == (5, 3)

    def test_a_grant_not_scored_for_them_says_so_and_has_no_score(self, story):
        row = by_id(story.ana_list)["E2E-FORECAST"]
        assert row["relevance_score"] is None and row["relevance_reason"] == NOT_YET

    def test_a_grant_the_pre_check_ruled_out_is_zero_with_the_rules_reason(self, story):
        assert story.ruled_out == ["E2E-STATES"]
        row = by_id(story.ana_list)["E2E-STATES"]
        assert row["relevance_score"] == 0 and row["relevance_model"] == "rule"
        assert row["relevance_reason"] == story.team["E2E-STATES"]["relevance_reason"] and "Grants.gov eligibility" in row["relevance_reason"]

    def test_the_floor_lets_through_only_what_they_scored_at_it_and_not_the_teams_picks(self, story):
        assert set(by_id(story.ana_floor)) == {"E2E-TRUSTED", "E2E-BROAD", "E2E-BULLETS"}  # not NOKW (the team's 5, her 2), nor an unscored grant

    def test_the_team_member_next_to_them_is_shown_the_teams_scores(self, story):
        assert story.cara_list["scores"] == "team"
        rows = by_id(story.cara_list)
        assert rows["E2E-NOKW"]["relevance_score"] == 5 and rows["E2E-FORECAST"]["relevance_score"] == story.team["E2E-FORECAST"]["relevance_score"]
        assert story.cara_profile["me"]["using"] == "team" and story.cara_profile["me"]["has_profile"] is False

    def test_the_detail_of_a_grant_has_the_score_of_the_list(self, story):
        assert (story.detail["ana"]["relevance_score"], story.detail["ana"]["relevance_reason"]) == (2, "Ana: the team likes it, she does not")
        assert story.detail["cara"]["relevance_score"] == 5 and story.detail["cara"]["description"]

    def test_a_profile_with_no_score_yet_is_shown_the_teams_scores(self, story):
        assert story.dan_list["scores"] == "team" and story.dan_profile["me"] | {"updated_at": None} == {
            "has_profile": True, "updated_at": None, "min_score": 4, "coverage": {"open": story.open, "scored": len(story.ruled_out)}, "using": "team",
        }
        assert by_id(story.dan_list)["E2E-NOKW"]["relevance_score"] == 5 and story.detail["dan"]["relevance_score"] == 5


class TestCoverage:
    def test_it_counts_the_scores_under_the_profile_as_the_scoring_pass_keys_it_plus_the_ruled_out(self, story):
        # Ana's profile was typed by hand. Three scores carry the key of the cleaned text and count; the one made
        # from the text as typed, and the one by an older model, are shown but are still to do. E2E-STATES is ruled out.
        assert story.ana_profile["me"]["coverage"] == {"open": story.open, "scored": 3 + 1}

    def test_a_score_made_for_another_members_key_does_not_count_for_this_one(self, story):
        assert story.ben_profile["me"]["coverage"] == {"open": story.open, "scored": 1}  # only the ruled-out grant

    def test_it_is_reported_with_the_profile_and_the_default_floor(self, story):
        me = story.ana_profile["me"]
        assert me["has_profile"] is True and me["min_score"] == 4 and me["updated_at"]
        assert story.ana_profile["min_score"] == 4 and story.ana_profile["data"]["fit_count"] == 3  # the status line counts by her scores at her floor

    def test_a_model_swap_leaves_their_scores_on_view_and_makes_them_all_to_do(self, story, monkeypatch):
        monkeypatch.setenv("GRANTS_LLM_MODEL", "a-newer-model")
        ana = story.clients[ANA]
        assert ana.get("/api/profile").json()["me"]["coverage"] == {"open": story.open, "scored": 1}
        listed = ana.get("/api/grants?limit=1000").json()
        assert listed["scores"] == "mine" and by_id(listed)["E2E-TRUSTED"]["relevance_score"] == 5

    def test_a_member_on_the_team_profile_has_nothing_scored_for_a_profile(self, story):
        assert story.cara_profile["me"]["coverage"] == {"open": story.open, "scored": 0}


class TestVotes:
    def test_their_own_relevant_mark_lets_a_grant_through_their_floor(self, story):
        assert LOW not in by_id(story.ana_floor) and LOW in by_id(story.ana_floor_voted)
        assert set(by_id(story.ana_floor_voted)) == set(by_id(story.ana_floor)) | {LOW}

    def test_their_votes_are_theirs_alone_and_no_team_count_is_sent(self, story):
        rows = by_id(story.ana_voted)
        for opp in (LOW, "E2E-TRUSTED"):
            assert (rows[opp]["votes_up"], rows[opp]["votes_down"], rows[opp]["team_verdict"]) == (None, None, None)
        assert (rows[LOW]["my_verdict"], rows[LOW]["hidden"]) == (1, False)
        assert (rows["E2E-TRUSTED"]["my_verdict"], rows["E2E-TRUSTED"]["hidden"]) == (-1, True)  # their Not for us hides it for them

    def test_a_members_vote_does_not_move_the_teams_counts_verdict_or_hiding(self, story):
        rows = by_id(story.cara_voted)
        assert (rows[LOW]["votes_up"], rows[LOW]["votes_down"], rows[LOW]["team_verdict"]) == (0, 0, 0)
        assert (rows["E2E-NOKW"]["votes_down"], rows["E2E-NOKW"]["team_verdict"], rows["E2E-NOKW"]["hidden"]) == (0, 0, False)  # Dan's Not for us
        assert rows["E2E-TRUSTED"]["hidden"] is False and rows["E2E-TRUSTED"]["my_verdict"] is None
        assert LOW not in by_id(story.cara_floor_voted) and set(by_id(story.cara_floor_voted)) == set(by_id(story.cara_floor))  # nor does it pass the team's floor

    def test_the_vote_of_a_member_with_a_profile_and_no_score_yet_hides_the_grant_for_them_only(self, story):
        dan = by_id(story.dan_voted)["E2E-NOKW"]
        assert (dan["my_verdict"], dan["hidden"], dan["votes_down"], dan["team_verdict"]) == (-1, True, None, None)
        assert story.dan_voted["scores"] == "team"
        assert by_id(story.ana_voted)["E2E-NOKW"]["hidden"] is False and by_id(story.cara_voted)["E2E-NOKW"]["hidden"] is False
