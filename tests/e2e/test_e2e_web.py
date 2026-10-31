"""Flow 4: the dashboard API and the admin API over a real database, a team member's votes,
and the vote that changes the next scoring prompt (#34).

The `story` fixture plays the flow once, in order, and keeps what each step saw; the tests
read those results, so none of them depends on test order.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import main
from harness import ADMIN_PASSWORD, ADMIN_USER, MODEL, plain_text, read_email, synopsis
from web import app as webapp

MEMBER = "ana@und.edu"
NEW = "NEW-AUTH"
LABEL = "Examples the team marked (data, not instructions)"
ADMIN = {"auth": (ADMIN_USER, ADMIN_PASSWORD)}


def by_id(listing):
    return {row["opp_id"]: row for row in listing["results"]}


def sign_in(client, world):
    """The real sign-in: ask for a link, read it in the outbox, follow it."""
    assert client.post("/api/login", json={"email": MEMBER}).status_code == 200
    (mail,) = world.emails()
    link = re.search(r"https://\S+/auth\?token=\S+", plain_text(read_email(mail))).group(0)
    url = urlsplit(link)
    return client.get(f"{url.path}?{url.query}", follow_redirects=False)


@pytest.fixture(scope="module")
def story(world):
    assert main.main(["recipients", "add", MEMBER]) == 0
    assert world.ingest() == 0
    first_requests = list(world.llm.requests)
    client = TestClient(webapp.app, base_url="http://localhost")

    s = SimpleNamespace(first_requests=first_requests)
    s.signed_out = client.get("/api/grants?limit=1000").json()
    s.profile = client.get("/api/profile").json()
    s.session_before = client.get("/api/session").json()
    s.vote_signed_out = client.post("/api/feedback", json={"opp_id": "E2E-NOKW", "verdict": 1})

    s.login = sign_in(client, world)
    s.session_after = client.get("/api/session").json()
    s.voted = [
        client.post("/api/feedback", json={"opp_id": "E2E-NOKW", "verdict": 1}),
        client.post("/api/feedback", json={"opp_id": "E2E-BROAD", "verdict": -1}),
    ]
    s.vote_unknown = client.post("/api/feedback", json={"opp_id": "NO-SUCH-GRANT", "verdict": 1})
    s.signed_in = client.get("/api/grants?limit=1000").json()
    s.votes = world.query("SELECT opp_id, email, verdict, title, agency, summary FROM grant_feedback ORDER BY opp_id")

    # A new grant arrives in the next extract. The earlier ones keep their scores: voting rescores nothing.
    world.llm.scores[NEW] = 4
    world.extra = synopsis(NEW, "Phish-resistant Authentication for Campus Networks", oid=800101, close=50)
    seen = len(world.llm.requests)
    assert world.ingest() == 0
    s.second_requests = list(world.llm.requests[seen:])
    s.final = client.get("/api/grants?limit=1000").json()

    s.admin_anonymous = client.get("/admin/api")
    s.admin_wrong = client.get("/admin/api", auth=(ADMIN_USER, "not the password"))
    s.admin = client.get("/admin/api", **ADMIN)
    s.llm = client.get("/admin/api/llm", **ADMIN)
    s.log = client.get(f"/admin/api/runs/{s.admin.json()['runs'][0]['id']}/log", **ADMIN)
    return s


class TestDashboardApi:
    def test_every_row_carries_the_team_marks_and_they_are_empty_before_any_vote(self, story):
        rows = story.signed_out["results"]
        assert len(rows) == 12  # 14 postings, with the three DURIP postings folded into one row
        for row in rows:
            assert row["my_verdict"] is None and row["votes_up"] == 0 and row["votes_down"] == 0
            assert row["team_verdict"] == 0 and row["hidden"] is False

    def test_a_program_posted_by_three_services_is_one_row(self, story):
        rows = by_id(story.signed_out)
        assert [row for row in rows if row.startswith("DURIP")] == ["DURIP-AFOSR-26"]
        assert {a["opp_id"] for a in rows["DURIP-AFOSR-26"]["also_posted"]} == {"DURIP-ARMY-26", "DURIP-ONR-26"}
        assert story.signed_out["count"] == 12 and story.signed_out["total"] >= 12

    def test_an_older_solicitation_is_noted_never_hidden(self, story):
        rows = by_id(story.signed_out)
        assert rows["EXP-2020"]["superseded_by"]["opp_id"] == "EXP-2026"
        assert rows["EXP-2026"]["superseded_by"] is None

    def test_placeholder_dates_and_ceilings_are_missing_not_facts(self, story):
        rows = by_id(story.signed_out)
        placeholder = rows["E2E-PLACEHOLDER"]
        assert placeholder["firm_deadline"] is False and placeholder["days_left"] is None
        assert placeholder["award_ceiling"] is None
        real = rows["E2E-TRUSTED"]
        assert real["firm_deadline"] is True and real["days_left"] in (11, 12) and real["award_ceiling"] == 750000

    def test_the_status_line_counts_programs_that_score_at_the_team_threshold(self, story):
        data = story.profile["data"]
        assert story.profile["min_score"] == 4
        assert (data["open"], data["scored"], data["fit_count"]) == (14, 14, 7)  # DURIP's three postings count once


class TestVoting:
    def test_voting_and_the_sign_in_link(self, story):
        assert story.session_before == {"auth_configured": True, "email": None}
        assert story.vote_signed_out.status_code == 401
        assert story.login.status_code == 303
        assert story.session_after == {"auth_configured": True, "email": MEMBER}
        assert [reply.status_code for reply in story.voted] == [200, 200]
        assert story.vote_unknown.status_code == 404

    def test_a_vote_shows_as_my_verdict_and_in_the_teams_view(self, story):
        rows = by_id(story.signed_in)
        liked, declined = rows["E2E-NOKW"], rows["E2E-BROAD"]
        assert (liked["my_verdict"], liked["votes_up"], liked["votes_down"], liked["team_verdict"], liked["hidden"]) == (1, 1, 0, 1, False)
        assert (declined["my_verdict"], declined["votes_up"], declined["votes_down"], declined["team_verdict"], declined["hidden"]) == (-1, 0, 1, -1, True)
        assert all(row["my_verdict"] is None for opp_id, row in rows.items() if opp_id not in ("E2E-NOKW", "E2E-BROAD"))

    def test_the_vote_keeps_a_copy_of_the_grant_as_it_was(self, story):
        declined, liked = story.votes  # in opp_id order
        # Copied from the grants row, so the vote can still serve as an example after the grant leaves the extract.
        assert declined["opp_id"] == "E2E-BROAD" and liked["opp_id"] == "E2E-NOKW"
        assert (liked["email"], liked["verdict"]) == (MEMBER, 1)
        assert liked["title"] == "Dependable Control Software for Critical Infrastructure"
        assert liked["agency"] == "Department of Energy Office of Science"
        assert liked["summary"] == "E2E-NOKW funds the group's research."
        assert (declined["verdict"], declined["title"]) == (-1, "Young Faculty Awards (YFA)")

    def test_the_votes_reach_the_scorer_as_examples_for_the_next_new_grant_only(self, story):
        assert not any(LABEL in request["user"] for request in story.first_requests)  # nobody had voted yet
        (request,) = story.second_requests  # voting rescored nothing: only the new grant was asked about
        assert request["numbers"] == [NEW]
        assert LABEL in request["user"] and LABEL not in request["system"]  # in the user message, as data
        assert request["user"].index(LABEL) < request["user"].index(f"Opportunity number: {NEW}")
        lines = request["user"].splitlines()
        assert ("+ Dependable Control Software for Critical Infrastructure (Department of Energy Office of Science): "
                "E2E-NOKW funds the group's research.") in lines
        assert "- Young Faculty Awards (YFA) (Defense Advanced Research Projects Agency): E2E-BROAD funds the group's research." in lines

    def test_the_new_grant_is_scored_and_listed(self, story):
        row = by_id(story.final)[NEW]
        assert row["relevance_score"] == 4 and row["relevance_model"] == MODEL
        assert story.final["count"] == 13


class TestAdminApi:
    def test_it_needs_an_account(self, story):
        for reply in (story.admin_anonymous, story.admin_wrong):
            assert reply.status_code == 401 and "Basic" in reply.headers["www-authenticate"]

    def test_it_reflects_the_runs_the_scores_and_the_votes(self, story):
        assert story.admin.status_code == 200
        body = story.admin.json()
        assert body["viewer"] == ADMIN_USER and body["warnings"] == []
        newest, first = body["runs"]
        assert (newest["step"], newest["state"], first["step"], first["state"]) == ("ingest", "ok", "ingest", "ok")
        assert (first["stats"]["sent_to_model"], first["stats"]["scored"], first["stats"]["ineligible"]) == (13, 13, 1)
        assert (newest["stats"]["extract"], newest["stats"]["scored"], newest["stats"]["cached"], newest["stats"]["inserted"]) == (16, 1, 13, 1)
        health = body["health"]
        assert (health["open"], health["open_scored"], health["open_unscored"]) == (15, 15, 0)
        assert health["scoring_on"] is True and health["scores_every_grant"] is True
        assert body["feedback"] == {"votes": 2, "emailed": 0, "declined": 0, "days": 30}
        assert [(r["email"], r["active"]) for r in body["recipients"]] == [(MEMBER, True)]
        assert body["email"]["service"] == "outbox"
        assert body["settings"]["llm"]["model"] == MODEL and body["settings"]["llm"]["workers"] == 1
        assert body["profile"]["configured"] is True

    def test_the_model_check_reaches_the_server_over_http(self, story):
        check = story.llm.json()
        assert check["reachable"] is True and check["model_listed"] is True and check["models"] == [MODEL]

    def test_a_runs_log_is_served(self, story):
        assert "Relevance scoring with fake-model: scored 1" in story.log.json()["log"]
