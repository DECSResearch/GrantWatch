"""Flow 4: what a visitor who is not signed in gets from the API, over a real database.

The `story` fixture ingests the fixture extract and reads every public route with many parameters.
It then changes only what GrantWatch holds about the team (scores, reasons, the model's summaries,
keywords, tracking, votes, personal profiles and scores, thresholds) and reads them all again.
Whatever the parameters, a visitor's answers must not differ by a byte.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from grants.display import PUBLIC_DETAIL_FIELDS
from grants.members import save_member_profile
from grants.sql_utils import open_grant_sql
from llm_utils.gpt_summarizer import description_summarizer
from notifications import store
from web import app as webapp

ANA, BEN = "ana@und.edu", "ben@und.edu"
DURIP = ["DURIP-AFOSR-26", "DURIP-ARMY-26", "DURIP-ONR-26"]
ALLOWED = set(PUBLIC_DETAIL_FIELDS) | {"also_posted", "superseded_by"}
LONG = ("A long call for proposals. " * 40).strip()  # more than 320 characters
SPACED = "y" * 320 + " and the call goes on well past what a one-sentence summary shows."  # the 321st character is a space
# What only the team's side of the database says. None of it may appear in anything a visitor is sent.
ZEBRA = "zebra"
PATHS = [
    "/api/grants", "/api/grants?limit=1", "/api/grants?limit=3", "/api/grants?limit=1000",
    "/api/grants?min_score=5", "/api/grants?min_score=4&watched_only=true", "/api/grants?watched_only=true", "/api/grants?include_closed=true",
    "/api/grants?scores=mine", "/api/grants?scores=team&sort=relevance_score", "/api/grants?show_hidden=true",
    "/api/grants?q=cyber", "/api/grants?q=group", "/api/grants?q=zebra", "/api/grants?q=funds", "/api/grants?q=Navy", "/api/grants?q=E2E-NOKW",
    "/api/grants?q=cyber-physical", "/api/grants?q=funds%20the%20group", "/api/grants?q=Air%20Force%20Research", "/api/grants?q=trusted%20systems",
    "/api/grants?closing_within=30", "/api/grants?closing_within=1", "/api/grants?stage=concept", "/api/grants?stage=full&include_forecasted=false",
    "/api/grants?due_from=2000-01-01&due_to=2100-01-01", "/api/grants?q=cyber&limit=2&min_score=5",
    "/api/profile", "/api/grants/NO-SUCH-GRANT",
]


def snapshot(client, paths):
    return {path: (reply.status_code, reply.content) for path, reply in ((p, client.get(p)) for p in paths)}


@pytest.fixture(scope="module")
def story(world):
    world.reset()
    assert world.ingest() == 0
    world.query("UPDATE grants SET description = %s WHERE opp_id = 'E2E-PLACEHOLDER'", (LONG,))
    world.query("UPDATE grants SET description = %s WHERE opp_id = 'E2E-BROAD'", (SPACED,))
    client = TestClient(webapp.app, base_url="http://localhost")
    ids = [row["opp_id"] for row in world.query("SELECT opp_id FROM grants ORDER BY opp_id")]
    paths = PATHS + [f"/api/grants/{opp_id}" for opp_id in ids]
    s = SimpleNamespace(client=client, ids=ids, paths=paths)
    s.before = snapshot(client, paths)
    s.watchlist = client.get("/api/watchlist")
    s.expected_order = [
        row["opp_id"] for row in world.query(
            f"SELECT g.opp_id FROM grants g WHERE {open_grant_sql('g')} ORDER BY g.close_date NULLS LAST, g.post_date DESC, g.title, g.opp_id"
        )
    ]
    s.descriptions = {row["opp_id"]: row["description"] for row in world.query("SELECT opp_id, description FROM grants")}

    # Change only what the team holds. The scores run the other way round, the lead of a program by score
    # would be another posting, and the tracked one would be another still.
    world.query(
        "UPDATE grants SET relevance_score = 5 - COALESCE(relevance_score, 0), relevance_reason = 'zebra reason', relevance_model = 'zebra-model',"
        " relevance_profile = 'zebra', summary = 'zebra zebra cyber-physical', matched_keywords = 'zebra; cyber-physical', first_seen_at = first_seen_at - INTERVAL '40 days'"
    )
    world.query("INSERT INTO grant_watchlist (opp_id, note) VALUES ('DURIP-ONR-26', 'zebra note'), ('E2E-NOKW', 'zebra note'), ('E2E-BROAD', 'zebra note')")
    for email in (ANA, BEN):
        store.add_recipient(email)
    world.query("UPDATE recipients SET min_score = 5")
    save_member_profile(ANA, "I study zebra migration with cyber-physical sensors across the savanna.")
    world.query(
        "INSERT INTO member_scores (opp_id, email, score, reason, model, score_key) SELECT opp_id, %s, 5, 'zebra fit', 'zebra-model', 'zebra' FROM grants", (ANA,)
    )
    world.query(
        "INSERT INTO grant_feedback (opp_id, email, verdict, title, agency, summary) VALUES "
        "('DURIP-ARMY-26', %s, 1, 'zebra', 'zebra', 'zebra'), ('E2E-BROAD', %s, -1, 'zebra', 'zebra', 'zebra'), ('E2E-NOKW', %s, -1, 'zebra', 'zebra', 'zebra')",
        (ANA, BEN, ANA),
    )
    s.after = snapshot(client, paths)
    return s


def body(story, path, which="before"):
    status, content = getattr(story, which)[path]
    return status, json.loads(content)


class TestWhatTheTeamHoldsDoesNotShow:
    def test_changing_all_of_it_changes_no_answer_by_a_byte(self, story):
        differing = [path for path in story.paths if story.before[path] != story.after[path]]
        assert differing == []

    def test_none_of_it_is_in_any_answer(self, story):
        for which in ("before", "after"):
            for path, (_, content) in getattr(story, which).items():
                text = content.decode("utf-8").lower()
                assert ZEBRA not in text, path
                assert "relevance" not in text and "matched_keywords" not in text and "watch" not in text, path

    def test_the_team_profile_is_not_in_any_answer(self, story):
        summary = webapp._profile().summary.strip()
        assert summary
        for path, (_, content) in story.after.items():
            assert summary.split(".")[0] not in content.decode("utf-8"), path

    def test_the_lead_of_a_program_is_the_lowest_number_whatever_the_scores_or_tracking(self, story):
        for which in ("before", "after"):
            rows = {r["opp_id"]: r for r in body(story, "/api/grants?limit=1000", which)[1]["results"]}
            assert [opp for opp in rows if opp.startswith("DURIP")] == ["DURIP-AFOSR-26"]
            assert [a["opp_id"] for a in rows["DURIP-AFOSR-26"]["also_posted"]] == ["DURIP-ARMY-26", "DURIP-ONR-26"]


class TestTheList:
    def test_it_lists_the_open_grants_in_the_public_order_and_folds_a_program(self, story):
        status, listed = body(story, "/api/grants?limit=1000")
        assert status == 200 and set(listed) == {"results", "count", "total"}
        shown = [row["opp_id"] for row in listed["results"]]
        folded = {a["opp_id"] for row in listed["results"] for a in row["also_posted"]}
        assert folded == {"DURIP-ARMY-26", "DURIP-ONR-26"}
        assert shown == [opp for opp in story.expected_order if opp not in folded]  # by close date, never by score
        assert "E2E-CLOSED" not in shown and listed["count"] == len(shown) and listed["total"] == len(story.expected_order)

    def test_only_grants_gov_facts_are_in_a_row_at_any_depth(self, story):
        for row in body(story, "/api/grants?limit=1000")[1]["results"]:
            assert set(row) <= ALLOWED
            assert all(set(entry) == {"opp_id", "agency", "url"} for entry in row["also_posted"])
            assert row["superseded_by"] is None or set(row["superseded_by"]) == {"opp_id", "url"}

    def test_a_row_has_the_facts_a_visitor_is_promised(self, story):
        rows = {r["opp_id"]: r for r in body(story, "/api/grants?limit=1000")[1]["results"]}
        row = rows["E2E-TRUSTED"]
        assert row["title"] == "Foundations of Trusted Systems" and row["agency"] == "Air Force Research Laboratory" and row["agency_code"] == "DOD-AFRL"
        assert row["firm_deadline"] is True and row["days_left"] in (11, 12) and row["award_ceiling"] == 750000
        assert rows["E2E-PLACEHOLDER"]["firm_deadline"] is False and rows["E2E-PLACEHOLDER"]["award_ceiling"] is None
        assert rows["EXP-2020"]["superseded_by"]["opp_id"] == "EXP-2026"
        assert rows["E2E-FORECAST"]["forecasted"] is True

    def test_the_summary_is_the_grants_gov_description_cut_short_not_the_models(self, story):
        rows = {r["opp_id"]: r for r in body(story, "/api/grants?limit=1000")[1]["results"]}
        assert rows["E2E-NOKW"]["summary"] == story.descriptions["E2E-NOKW"] == "Funds reliable control software for the operators of critical infrastructure and their university partners."
        assert rows["E2E-PLACEHOLDER"]["summary"] == LONG[:320].rstrip() + "..."

    def test_the_summary_is_cut_as_the_pipeline_cuts_it_when_a_space_falls_where_the_cut_ends(self, story):
        rows = {r["opp_id"]: r for r in body(story, "/api/grants?limit=1000")[1]["results"]}
        assert story.descriptions["E2E-BROAD"] == SPACED
        assert rows["E2E-BROAD"]["summary"] == "y" * 320 + "..." == description_summarizer([{"FUNDING_DESCRIPTION": SPACED}])[0]["SUMMARY"]

    def test_the_limit_takes_the_first_in_the_public_order_and_a_full_list_counts_all(self, story):
        _, listed = body(story, "/api/grants?limit=3")
        assert [r["opp_id"] for r in listed["results"]] == story.expected_order[:3]
        assert listed["total"] == len(story.expected_order)

    def test_a_score_floor_the_watchlist_and_closed_grants_are_not_a_visitors_to_ask_for(self, story):
        plain = body(story, "/api/grants")
        for path in ("/api/grants?min_score=5", "/api/grants?watched_only=true", "/api/grants?include_closed=true", "/api/grants?scores=mine"):
            assert body(story, path) == plain, path

    def test_a_search_reads_the_title_agency_number_and_description(self, story):
        def found(query):
            return [r["opp_id"] for r in body(story, f"/api/grants?q={query}")[1]["results"]]

        assert found("Navy") == ["DURIP-ONR-26"]  # the description of the Navy's posting alone
        assert found("E2E-NOKW") == ["E2E-NOKW"]
        assert "E2E-TRUSTED" in found("Air%20Force%20Research")
        assert "E2E-TRUSTED" in found("trusted%20systems")

    def test_it_does_not_read_the_models_summary_or_the_keywords(self, story):
        # "group" is in every model summary ("funds the group's research"); "cyber-physical" and "zebra" are in none of the descriptions.
        for term in ("group", "zebra", "cyber-physical", "funds%20the%20group"):
            assert body(story, f"/api/grants?q={term}")[1]["results"] == [], term
            assert body(story, f"/api/grants?q={term}", "after")[1]["results"] == [], term


class TestTheRest:
    def test_one_grant_is_its_public_fields_and_the_whole_description(self, story):
        status, grant = body(story, "/api/grants/E2E-PLACEHOLDER")
        assert status == 200 and set(grant) == set(PUBLIC_DETAIL_FIELDS)
        assert grant["description"] == LONG and grant["summary"] == LONG[:320].rstrip() + "..."
        assert body(story, "/api/grants/E2E-BROAD")[1]["summary"] == "y" * 320 + "..."

    def test_an_unknown_grant_is_a_404(self, story):
        assert story.before["/api/grants/NO-SUCH-GRANT"][0] == 404

    def test_the_profile_is_the_count_of_open_grants_and_when_they_were_loaded(self, story):
        status, profile = body(story, "/api/profile")
        assert status == 200 and set(profile) == {"configured", "data"} and set(profile["data"]) == {"open", "data_as_of"}
        assert profile["configured"] is True and profile["data"]["open"] == len(story.expected_order) and profile["data"]["data_as_of"]

    def test_the_watchlist_needs_a_sign_in(self, story):
        assert story.watchlist.status_code == 401

    def test_every_answer_is_private_to_the_cookie(self, story):
        reply = story.client.get("/api/grants")
        assert reply.headers["cache-control"] == "no-store" and "cookie" in reply.headers["vary"].lower()


class TestWithoutAKeyOrAModel:
    def test_a_server_with_no_secret_key_shows_only_the_public_list(self, story, monkeypatch):
        monkeypatch.delenv("GRANTS_SECRET_KEY")
        assert snapshot(story.client, story.paths) == story.after
        assert story.client.get("/api/watchlist").status_code == 503
        assert story.client.get("/api/session").json() == {"auth_configured": False, "email": None}

    def test_a_server_with_no_secret_key_ignores_an_old_cookie(self, story, world, monkeypatch):
        client = TestClient(webapp.app, base_url="http://localhost")
        world.sign_in(client, ANA)
        member = client.get("/api/grants?limit=1000").json()
        assert "relevance_score" in member["results"][0]  # the key is set: Ana is a member
        monkeypatch.delenv("GRANTS_SECRET_KEY")
        assert client.get("/api/grants?limit=1000").content == story.after["/api/grants?limit=1000"][1]

    def test_with_no_model_there_is_no_list_at_all(self, story, monkeypatch):
        # Without a model the database holds the keyword matches, a pick made for the team.
        monkeypatch.delenv("GRANTS_LLM_MODEL")
        assert story.client.get("/api/grants").json() == {"results": [], "count": 0, "total": 0}
        assert story.client.get("/api/grants/E2E-NOKW").status_code == 404
        assert story.client.get("/api/profile").json() == {"configured": False, "data": {"open": 0, "data_as_of": None}}
