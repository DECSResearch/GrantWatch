"""Flow 4: what a visitor who is not signed in gets from the API, over a real database.

The `story` fixture ingests the fixture extract and reads every public route with many parameters.
It then changes only what GrantWatch holds about the team (scores, reasons, the model's summaries,
keywords, tracking, votes, personal profiles and scores, thresholds) and reads them all again.
Whatever the parameters, a visitor's answers must not differ by a byte.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from grants.display import PUBLIC_DETAIL_FIELDS
from grants.members import save_member_profile
from grants.sql_utils import open_grant_sql
from llm_utils.gpt_summarizer import description_summarizer
from notifications import store
from web import app as webapp

ANA, BEN, CARA = "ana@und.edu", "ben@und.edu", "cara@und.edu"
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


def iso(today, days):
    return (today + timedelta(days=days)).isoformat()


def windowed(today):
    """Due-date windows that cut the list at one end or both, with limits smaller than the number of grants in them.

    The fixture's grants close 12, 20, 35 (three postings), 40, 45, 55, 65, 70, 80 (two) and 100 days out, and one far off.
    """
    window = f"due_from={iso(today, 30)}&due_to={iso(today, 75)}"  # 8 postings: from the DURIP trio to E2E-ENTITY
    return [
        f"/api/grants?{window}", f"/api/grants?{window}&limit=1", f"/api/grants?{window}&limit=3", f"/api/grants?{window}&limit=5",
        f"/api/grants?due_from={iso(today, 30)}&limit=2", f"/api/grants?due_to={iso(today, 50)}&limit=2", f"/api/grants?due_to={iso(today, 50)}&limit=4",
        f"/api/grants?{window}&limit=3&min_score=5&watched_only=true&show_hidden=true&scores=mine&sort=relevance_score",
        f"/api/grants?{window}&q=authentication&limit=1", f"/api/grants?due_from={iso(today, 90)}", f"/api/grants?due_from={iso(today, 90)}&due_to={iso(today, 30)}",
    ]


def twin_url(schema):
    """The database address the web app is using, pointed at ``schema`` instead."""
    parts = urlsplit(os.environ["POSTGRES_URL"])
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "options"] + [("options", f"-csearch_path={schema}")]
    return urlunsplit(parts._replace(query=urlencode(query)))


def snapshot(client, paths):
    return {path: (reply.status_code, reply.content) for path, reply in ((p, client.get(p)) for p in paths)}


@pytest.fixture(scope="module")
def story(world):
    world.reset()
    assert world.ingest() == 0
    world.query("UPDATE grants SET description = %s WHERE opp_id = 'E2E-PLACEHOLDER'", (LONG,))
    world.query("UPDATE grants SET description = %s WHERE opp_id = 'E2E-BROAD'", (SPACED,))
    client = TestClient(webapp.app, base_url="http://localhost")
    member = TestClient(webapp.app, base_url="http://localhost")
    world.sign_in(member, CARA)  # a member on the team profile, to show the changes below are ones a member can see
    ids = [row["opp_id"] for row in world.query("SELECT opp_id FROM grants ORDER BY opp_id")]
    paths = PATHS + windowed(world.today) + [f"/api/grants/{opp_id}" for opp_id in ids]
    s = SimpleNamespace(client=client, member=member, ids=ids, paths=paths, today=world.today)
    s.member_paths = ["/api/grants?limit=3", f"/api/grants?{windowed(world.today)[0].split('?')[1]}&limit=3", "/api/grants/E2E-NOKW", "/api/grants/E2E-BROAD"]
    s.before = snapshot(client, paths)
    s.member_before = snapshot(member, s.member_paths)
    s.watchlist = client.get("/api/watchlist")
    s.expected_order = [
        row["opp_id"] for row in world.query(
            f"SELECT g.opp_id FROM grants g WHERE {open_grant_sql('g')} ORDER BY g.close_date NULLS LAST, g.post_date DESC, g.title, g.opp_id"
        )
    ]
    s.in_window = [
        row["opp_id"] for row in world.query(
            f"SELECT g.opp_id FROM grants g WHERE {open_grant_sql('g')} AND g.close_date BETWEEN %s AND %s ORDER BY g.close_date, g.post_date DESC, g.title, g.opp_id",
            (world.today + timedelta(days=30), world.today + timedelta(days=75)),
        )
    ]
    s.descriptions = {row["opp_id"]: row["description"] for row in world.query("SELECT opp_id, description FROM grants")}
    by_score = (
        f"SELECT g.opp_id FROM grants g WHERE {open_grant_sql('g')} AND g.close_date BETWEEN %s AND %s"
        " ORDER BY COALESCE(g.relevance_score, -1) DESC, g.close_date, g.opp_id"
    )
    window = (world.today + timedelta(days=30), world.today + timedelta(days=75))
    s.by_score = {"before": [row["opp_id"] for row in world.query(by_score, window)]}

    # A second copy of the grants as they are now, for the property test at the end: it is read by pointing the app at it.
    twin, own = f"{world.schema}_twin", os.environ["POSTGRES_URL"]
    world.query(f"CREATE SCHEMA {twin}")
    try:
        world.query(f"CREATE TABLE {twin}.grants (LIKE grants INCLUDING ALL)")
        world.query(f"INSERT INTO {twin}.grants SELECT * FROM grants")
        s.twin_url, s.own_url = twin_url(twin), own

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
        s.member_after = snapshot(member, s.member_paths)
        s.by_score["after"] = [row["opp_id"] for row in world.query(by_score, window)]
        yield s
    finally:
        world.mp.setenv("POSTGRES_URL", own)
        world.query(f"DROP SCHEMA IF EXISTS {twin} CASCADE")


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

    def test_a_window_and_a_limit_take_the_first_in_the_public_order(self, story):
        whole, one, five = windowed(story.today)[0], windowed(story.today)[1], windowed(story.today)[3]
        assert len(story.in_window) == 8  # three DURIP postings, the grant the rules ruled out, and four more
        for path, postings in ((whole, 8), (five, 5), (one, 1)):
            listed = body(story, path)[1]
            shown = story.in_window[:postings]
            folded = {a["opp_id"] for row in listed["results"] for a in row["also_posted"]}
            assert [r["opp_id"] for r in listed["results"]] == [opp for opp in shown if opp not in folded], path
        limited = body(story, five)[1]
        assert limited["total"] == 8 and limited["count"] == 3  # the limit is smaller than what the window holds, and the total says so

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


class TestTheChangeWasReal:
    """Identical answers prove nothing if the change was one nobody could see. A member on the team profile sees it."""

    def test_a_member_sees_the_scores_that_changed(self, story):
        for path in story.member_paths:
            assert story.member_before[path] != story.member_after[path], path
        assert json.loads(story.member_before["/api/grants/E2E-NOKW"][1])["relevance_score"] == 5
        assert json.loads(story.member_after["/api/grants/E2E-NOKW"][1])["relevance_score"] == 0

    def test_a_window_ordered_by_the_teams_score_would_have_come_out_differently_in_each_state(self, story):
        first = slice(0, 5)  # the postings a limit of 5 keeps
        assert story.by_score["before"][first] != story.in_window[first] and story.by_score["after"][first] != story.in_window[first]
        assert story.by_score["before"][first] != story.by_score["after"][first]


PARAMETERS = st.fixed_dictionaries({}, optional={
    "q": st.sampled_from(["cyber", "group", "zebra", "funds", "Navy", "E2E", "authentication", "network"]),
    "limit": st.integers(1, 20), "min_score": st.integers(0, 5), "closing_within": st.integers(1, 120),
    "stage": st.sampled_from(["concept", "full"]), "scores": st.sampled_from(["mine", "team"]), "sort": st.sampled_from(["relevance_score", "close_date"]),
    **{name: st.sampled_from(["true", "false"]) for name in ("watched_only", "show_hidden", "include_closed", "include_forecasted")},
    **{name: st.integers(-10, 120).map(lambda n: iso(date.today(), n)) for name in ("due_from", "due_to")},
})


class TestForAnyParameters:
    @settings(max_examples=80, deadline=None)
    @given(params=PARAMETERS)
    def test_what_the_team_holds_changes_no_byte_of_the_list_whatever_is_asked_for(self, story, world, params):
        """The same query on two databases that differ only in the team's data: the grants as they were loaded, and the same with every score flipped, the
        model's summaries and keywords replaced, grants tracked, votes cast, a member's profile and scores added and thresholds set."""
        path = "/api/grants?" + urlencode(params)

        def read(url):
            world.mp.setenv("POSTGRES_URL", url)
            try:
                reply = story.client.get(path)
                return reply.status_code, reply.content
            finally:
                world.mp.setenv("POSTGRES_URL", story.own_url)

        assert read(story.own_url) == read(story.twin_url), path


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
