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
DURIP = ["DURIP-AFOSR-26", "DURIP-ARMY-26", "DURIP-ONR-26"]  # one program, three services
LOW = "E2E-NOCODES"  # the model scores it 3, under the team threshold of 4


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

    # The three DURIP postings are one row, so a person has one vote on them. Press them through different
    # postings and views; it stays one vote, and it ends taken back, so nothing below sees it.
    def durip():
        return by_id(client.get("/api/grants?limit=1000").json())["DURIP-AFOSR-26"]

    def step(reply):
        held = world.query("SELECT opp_id, verdict FROM grant_feedback WHERE opp_id = ANY(%s) ORDER BY opp_id", (DURIP,))
        return SimpleNamespace(status=reply.status_code, body=reply.json(), row=durip(), held=[(r["opp_id"], r["verdict"]) for r in held])

    s.durip = [step(client.post("/api/feedback", json={"opp_id": "DURIP-ARMY-26", "verdict": 1}))]  # not the row's lead
    s.durip.append(step(client.post("/api/feedback", json={"opp_id": s.durip[0].row["my_vote_opp_id"], "verdict": -1})))  # a change of mind
    s.durip_search = by_id(client.get("/api/grants?q=Naval&limit=1000").json())  # the Navy posting alone, its siblings hidden
    s.durip.append(step(client.post("/api/feedback", json={"opp_id": "DURIP-ONR-26", "verdict": 1})))
    s.durip.append(step(client.delete("/api/feedback/DURIP-AFOSR-26")))  # taken back through the lead; the vote sat on ONR

    # The dashboard opens at the team threshold, and a grant the team marked Relevant counts as meeting it (#45).
    # Tried on a grant scoring 3 at that floor, then on a program at a floor that only the mark gets it through.
    # Each vote ends taken back, so nothing below sees it.
    def floor(score, limit=1000):
        return client.get(f"/api/grants?min_score={score}&limit={limit}").json()

    s.floor = SimpleNamespace(before=floor(4), before_total=floor(4, limit=1)["total"])  # a full list of one, so the count query runs
    s.floor.marked_reply = client.post("/api/feedback", json={"opp_id": LOW, "verdict": 1})
    s.floor.marked, s.floor.marked_total = floor(4), floor(4, limit=1)["total"]
    s.floor.cleared_reply = client.delete(f"/api/feedback/{LOW}")
    s.floor.cleared = floor(4)
    s.floor.durip_before = floor(5)
    client.post("/api/feedback", json={"opp_id": "DURIP-ARMY-26", "verdict": 1})  # the 3; the program's best posting scores 4
    s.floor.durip_marked = floor(5)
    # A fourth posting of the program closed early (archived yesterday), so it is not open and not in the email's program.
    world.query(
        "INSERT INTO grants (opp_id, title, agency_code, close_date, archive_date) "
        "SELECT 'DURIP-OLD-26', title, 'DOD-OLD', close_date, CURRENT_DATE - 1 FROM grants WHERE opp_id = 'DURIP-ARMY-26'"
    )
    world.query("INSERT INTO grant_feedback (opp_id, email, verdict) VALUES ('DURIP-OLD-26', 'bo@und.edu', -1)")
    s.floor.durip_archived_sibling = floor(5)
    world.query("DELETE FROM grant_feedback WHERE opp_id = 'DURIP-OLD-26'")
    world.query("DELETE FROM grants WHERE opp_id = 'DURIP-OLD-26'")
    world.query("INSERT INTO grant_feedback (opp_id, email, verdict) VALUES ('DURIP-ONR-26', 'bo@und.edu', -1)")
    s.floor.durip_tied = floor(5)  # a Not for us on another posting of the program, from someone else
    world.query("DELETE FROM grant_feedback WHERE email = 'bo@und.edu'")
    client.delete("/api/feedback/DURIP-ARMY-26")
    s.floor.durip_cleared = floor(5)

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

    def test_a_closed_grant_is_not_counted_as_scored(self, world, story):
        client = TestClient(webapp.app, base_url="http://localhost")
        before = client.get("/api/profile").json()["data"]
        world.query("INSERT INTO grants (opp_id, title, close_date, relevance_score) "
                    "VALUES ('E2E-CLOSED-SCORED', 'Closed last month', CURRENT_DATE - 30, 5)")
        try:
            after = client.get("/api/profile").json()["data"]
        finally:
            world.query("DELETE FROM grants WHERE opp_id = 'E2E-CLOSED-SCORED'")
        assert after["total"] == before["total"] + 1
        assert (after["open"], after["scored"]) == (before["open"], before["scored"])
        assert after["scored"] <= after["open"]


class TestScoreFloor:
    def test_a_grant_scoring_under_the_threshold_is_not_listed_at_it(self, story):
        assert LOW not in by_id(story.floor.before) and LOW not in by_id(story.floor.cleared)
        assert "E2E-NOKW" in by_id(story.floor.before)  # the ones that meet it are

    def test_marked_relevant_it_is_listed_at_the_default_floor_and_counted(self, story):
        assert story.floor.marked_reply.status_code == 200
        row = by_id(story.floor.marked)[LOW]
        assert (row["relevance_score"], row["my_verdict"], row["team_verdict"], row["votes_up"], row["hidden"]) == (3, 1, 1, 1, False)
        assert story.floor.marked["count"] == story.floor.before["count"] + 1
        assert story.floor.marked_total == story.floor.before_total + 1  # the count behind "Showing" reads the same condition
        assert set(by_id(story.floor.marked)) - {LOW} == set(by_id(story.floor.before))  # and nothing else comes in

    def test_taking_the_mark_back_takes_it_off_the_list_again(self, story):
        assert story.floor.cleared_reply.json()["removed"] is True
        assert set(by_id(story.floor.cleared)) == set(by_id(story.floor.before))

    def test_a_program_comes_in_whole_when_the_team_marks_one_of_its_postings(self, story):
        assert not [opp for opp in by_id(story.floor.durip_before) if opp.startswith("DURIP")]  # 4, 4 and 3: none reaches 5
        row = by_id(story.floor.durip_marked)["DURIP-AFOSR-26"]
        assert {a["opp_id"] for a in row["also_posted"]} == {"DURIP-ARMY-26", "DURIP-ONR-26"}  # the marked posting's siblings too
        assert (row["my_verdict"], row["my_vote_opp_id"], row["team_verdict"]) == (1, "DURIP-ARMY-26", 1)

    def test_a_posting_that_is_no_longer_open_does_not_vote_on_the_program(self, story):
        # The email groups the open postings only, so a Not for us on an archived one does not tie the program's Relevant.
        listed = by_id(story.floor.durip_archived_sibling)
        assert "DURIP-AFOSR-26" in listed and "DURIP-OLD-26" not in listed
        assert {a["opp_id"] for a in listed["DURIP-AFOSR-26"]["also_posted"]} == {"DURIP-ARMY-26", "DURIP-ONR-26"}

    def test_a_program_is_judged_on_all_its_postings_votes(self, story):
        assert not [opp for opp in by_id(story.floor.durip_tied) if opp.startswith("DURIP")]  # a Relevant and a Not for us: a tie
        assert not [opp for opp in by_id(story.floor.durip_cleared) if opp.startswith("DURIP")]


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

    def test_a_vote_on_a_posting_that_is_not_the_lead_shows_on_the_grouped_row(self, story):
        first = story.durip[0]
        assert first.status == 200 and first.held == [("DURIP-ARMY-26", 1)]
        row = first.row
        assert (row["my_verdict"], row["my_vote_opp_id"], row["votes_up"], row["votes_down"], row["team_verdict"]) == (1, "DURIP-ARMY-26", 1, 0, 1)

    def test_a_change_of_mind_on_the_grouped_row_replaces_the_vote(self, story):
        change = story.durip[1]
        assert change.status == 200 and change.held == [("DURIP-ARMY-26", -1)]
        row = change.row
        assert (row["my_verdict"], row["votes_up"], row["votes_down"], row["team_verdict"], row["hidden"]) == (-1, 0, 1, -1, True)

    def test_a_vote_cast_through_a_search_that_hides_the_other_postings_still_replaces_theirs(self, story):
        shown = story.durip_search["DURIP-ONR-26"]
        assert shown["also_posted"] == [] and "DURIP-AFOSR-26" not in story.durip_search
        assert (shown["my_verdict"], shown["my_vote_opp_id"]) == (None, "DURIP-ONR-26")  # the vote sits on a posting not shown
        press = story.durip[2]
        assert press.held == [("DURIP-ONR-26", 1)]  # ARMY's Not for us is gone, not added to
        assert (press.row["my_verdict"], press.row["my_vote_opp_id"], press.row["votes_up"], press.row["votes_down"]) == (1, "DURIP-ONR-26", 1, 0)

    def test_a_vote_is_taken_back_through_any_posting_of_the_row(self, story):
        gone = story.durip[3]
        assert gone.body["removed"] is True and gone.held == []
        assert (gone.row["my_verdict"], gone.row["my_vote_opp_id"], gone.row["votes_up"], gone.row["votes_down"]) == (None, "DURIP-AFOSR-26", 0, 0)

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
