"""Flow 4: the admin page's People table and Personal profiles line over a real database (#55).

The `story` fixture plays the steps once, in order, and keeps what each saw; the tests read those results.
Alice and Bob write profiles of their own, Carol uses the team's, Dan wrote one and then unsubscribed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from grants.members import personal_profile, save_member_profile
from grants.sql_utils import ensure_schema, open_grant_sql
from harness import ADMIN_PASSWORD, ADMIN_USER, MODEL, SECRET_KEY
from llm_utils.relevance import personal_key
from web import app as webapp

ALICE, BOB, CAROL, DAN = "alice@und.edu", "bob@und.edu", "carol@und.edu", "dan@und.edu"
ALICE_TEXT = "I build battery models for grid-scale storage, mostly lithium and sodium chemistries."
BOB_TEXT = "I study wind-energy forecasting and the resilience of rural power grids to storms."
DAN_TEXT = "I work on hypersonic flight and thermal protection for reentry vehicles at the lab."
ALICE_EDIT = ALICE_TEXT + " I also work on solid-state electrolytes."
ADMIN = {"auth": (ADMIN_USER, ADMIN_PASSWORD)}


def key(text):
    return personal_key(personal_profile(webapp._profile(), text), MODEL)


def by_email(body):
    return {r["email"]: r for r in body["recipients"]}


def open_count(world, extra=""):
    return world.query(f"SELECT COUNT(*) AS n FROM grants g WHERE ({open_grant_sql('g')}) {extra}")[0]["n"]


@pytest.fixture(scope="module")
def story(world):
    world.reset()
    ensure_schema()
    world.llm.profile_scores = {"battery": {"E2E-BROAD": 5}, "wind-energy": {"E2E-BROAD": 1}}
    for email in (ALICE, BOB, CAROL, DAN):
        assert main.main(["recipients", "add", email]) == 0
    for email, text in ((ALICE, ALICE_TEXT), (BOB, BOB_TEXT), (DAN, DAN_TEXT)):
        save_member_profile(email, text)
    world.query("UPDATE recipients SET active = FALSE, unsubscribed_at = NOW() WHERE email = %s", (DAN,))
    assert world.ingest() == 0

    admin = TestClient(webapp.app, base_url="http://localhost")
    alice = TestClient(webapp.app, base_url="http://localhost")
    world.sign_in(alice, ALICE)
    s = SimpleNamespace(alice=alice, admin=admin)

    s.complete = admin.get("/admin/api", **ADMIN).json()
    s.alice_sees = alice.get("/api/profile").json()["me"]
    s.updated = {r["email"]: r["updated_at"] for r in world.query("SELECT email, updated_at FROM member_profiles")}
    s.open = open_count(world)
    s.team_scored = open_count(world, "AND g.relevance_score IS NOT NULL")
    s.ruled_out = open_count(world, "AND g.relevance_model = 'rule'")

    # Alice rewrites her profile: her scores go until the next run. Then one grant is scored under the new key.
    save_member_profile(ALICE, ALICE_EDIT)
    s.rewritten = admin.get("/admin/api", **ADMIN).json()
    s.alice_sees_rewritten = alice.get("/api/profile").json()["me"]
    pending = world.query("SELECT opp_id FROM grants g WHERE (" + open_grant_sql("g") + ") AND g.relevance_model IS DISTINCT FROM 'rule' ORDER BY opp_id LIMIT 1")[0]["opp_id"]
    world.query("INSERT INTO member_scores (opp_id, email, score, reason, model, score_key) VALUES (%s, %s, 4, 'r', 'm', %s)", (pending, ALICE, key(ALICE_EDIT)))
    s.one_scored = admin.get("/admin/api", **ADMIN).json()
    s.alice_sees_one = alice.get("/api/profile").json()["me"]

    # The same data with and without a sign-in key.
    world.mp.delenv("GRANTS_SECRET_KEY")
    s.no_key = admin.get("/admin/api", **ADMIN).json()
    world.mp.setenv("GRANTS_SECRET_KEY", SECRET_KEY)
    return s


class TestThePeopleTable:
    def test_the_people_with_a_profile_of_their_own_say_so_with_the_day_it_was_saved(self, story):
        people = by_email(story.complete)
        for email in (ALICE, BOB, DAN):
            assert people[email]["profile"] == "own"
            assert people[email]["profile_updated_at"].startswith(story.updated[email].date().isoformat())
        assert (people[CAROL]["profile"], people[CAROL]["profile_updated_at"]) == ("team", None)

    def test_a_person_whose_grants_are_all_scored_for_them_is_complete(self, story):
        people = by_email(story.complete)
        assert story.open > story.ruled_out > 0
        for email in (ALICE, BOB):
            assert people[email]["coverage"] == {"open": story.open, "scored": story.open}

    def test_it_is_the_count_their_own_dashboard_shows(self, story):
        assert by_email(story.complete)[ALICE]["coverage"] == story.alice_sees["coverage"]
        assert by_email(story.rewritten)[ALICE]["coverage"] == story.alice_sees_rewritten["coverage"]
        assert by_email(story.one_scored)[ALICE]["coverage"] == story.alice_sees_one["coverage"]

    def test_a_rewritten_profile_loses_its_scores_until_the_next_run_but_keeps_what_the_pre_check_ruled_out(self, story):
        assert by_email(story.rewritten)[ALICE]["coverage"] == {"open": story.open, "scored": story.ruled_out}
        assert by_email(story.rewritten)[BOB]["coverage"] == {"open": story.open, "scored": story.open}  # nobody else is touched

    def test_one_score_under_the_new_key_is_one_more(self, story):
        assert by_email(story.one_scored)[ALICE]["coverage"] == {"open": story.open, "scored": story.ruled_out + 1}

    def test_a_person_on_the_teams_profile_has_the_teams_coverage(self, story):
        assert by_email(story.complete)[CAROL]["coverage"] == {"open": story.open, "scored": story.team_scored}
        assert story.complete["health"]["open"] == story.open and story.complete["health"]["open_scored"] == story.team_scored

    def test_someone_who_unsubscribed_keeps_their_profile_in_the_table(self, story):
        dan = by_email(story.complete)[DAN]
        assert dan["active"] is False and dan["profile"] == "own"

    def test_nothing_anyone_wrote_reaches_the_page(self, story):
        for body in (story.complete, story.rewritten, story.one_scored):
            text = str(body)
            for words in (ALICE_TEXT, BOB_TEXT, DAN_TEXT, ALICE_EDIT, "battery", "wind-energy", "hypersonic", "electrolytes"):
                assert words not in text


class TestThePersonalProfilesLine:
    def test_the_totals_of_the_personal_step_are_in_the_runs_the_page_reads(self, story):
        runs = [r for r in story.complete["runs"] if r["step"] == "personal"]
        assert len(runs) == 1 and runs[0]["state"] == "ok"
        assert runs[0]["stats"] == {"personal": {"people": 2, "scored": 26, "failed": 0, "pending": 0, "stopped": None}}  # Dan unsubscribed: not scored


class TestSignIn:
    def test_the_data_says_whether_there_is_a_key_and_never_the_key(self, story):
        assert story.complete["sign_in"] is True and story.no_key["sign_in"] is False
        assert SECRET_KEY not in str(story.complete) and SECRET_KEY not in str(story.no_key)


def test_a_database_without_the_member_tables_gets_them_when_the_page_loads(world):
    """The schema check looks for them, so the People table never reads a table that is not there."""
    world.reset()
    world.query("DROP TABLE member_scores, member_profiles")
    assert world.query("SELECT to_regclass('member_profiles') IS NULL AS gone")[0]["gone"] is True
    reply = TestClient(webapp.app, base_url="http://localhost").get("/admin/api", **ADMIN)
    assert reply.status_code == 200
    assert world.query("SELECT to_regclass('member_profiles') IS NULL AS gone, to_regclass('member_scores') IS NULL AS also")[0] == {"gone": False, "also": False}
    assert reply.json()["recipients"] == []
