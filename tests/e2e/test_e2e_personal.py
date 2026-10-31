"""Flow 3: `ingest` also scores each member's own profile, between the team's load and the email.

Real Postgres and the scripted model server. The fixtures run each step once, in order, and keep what
it did, so a test reads a result and never depends on test order.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main
from grants.members import delete_member_profile, personal_profile, save_member_profile
from grants.personal import Member, write_scores
from grants.sql_utils import ensure_schema
from harness import MODEL, NOT_SENT, SCORES, synopsis
from llm_utils.relevance import personal_key, score_key
from web import app as webapp

ALICE, BOB, CAROL = "alice@und.edu", "bob@und.edu", "carol@und.edu"
ALICE_TEXT = "I build battery models for grid-scale storage, mostly lithium and sodium chemistries."
BOB_TEXT = "I study wind-energy forecasting and the resilience of rural power grids to storms."
ALICE_EDIT = ALICE_TEXT + " I also work on solid-state electrolytes."
SENT = sorted(set(SCORES))  # what the team pass sends to the model: 13 grants
ALICE_SCORES = {"E2E-BROAD": 5, "E2E-TRUSTED": 0}
BOB_SCORES = {"E2E-BROAD": 1}


def rows(world, email=None):
    sql = "SELECT * FROM member_scores" + (" WHERE email = %s" if email else "") + " ORDER BY email, opp_id"
    return world.query(sql, (email,) if email else ())


def personal_run(world):
    return world.query("SELECT * FROM job_runs WHERE step = 'personal' ORDER BY id DESC LIMIT 1")[0]


def keys(text):
    return personal_key(personal_profile(webapp._profile(), text), MODEL)


@pytest.fixture(scope="module")
def first(world):
    """Alice and Bob have profiles, Carol is on the team's; each voted once; one ingest."""
    world.reset()
    ensure_schema()
    world.llm.profile_scores = {"battery": ALICE_SCORES, "wind-energy": BOB_SCORES}
    for email in (ALICE, BOB, CAROL):
        assert main.main(["recipients", "add", email]) == 0
    save_member_profile(ALICE, ALICE_TEXT)
    save_member_profile(BOB, BOB_TEXT)
    for opp, email, verdict, title in (("E2E-BROAD", ALICE, -1, "Alice Dislikes This Broad Call"), ("E2E-TRUSTED", BOB, 1, "Bob Likes This Trusted Call"),
                                       ("E2E-NOCODES", CAROL, -1, "Carol Dislikes This One For The Team")):
        world.query("INSERT INTO grant_feedback (opp_id, email, verdict, title, agency, summary) VALUES (%s, %s, %s, %s, 'NSF', 'Funds things.')", (opp, email, verdict, title))
    code = world.ingest()
    return SimpleNamespace(
        code=code, requests=list(world.llm.requests), scores=rows(world), grants={r["opp_id"]: r for r in world.query("SELECT * FROM grants")},
        runs=world.query("SELECT id, step, ok, detail, stats, finished_at FROM job_runs ORDER BY id"),
    )


class TestFirstRun:
    def test_every_candidate_is_scored_for_each_member_and_the_run_is_recorded(self, first):
        assert first.code == 0
        assert [r["step"] for r in first.runs] == ["ingest", "personal"]
        personal = first.runs[1]
        assert personal["ok"] is True and personal["finished_at"] is not None
        assert personal["stats"] == {"personal": {"people": 2, "scored": 26, "failed": 0, "pending": 0, "stopped": None}}
        assert personal["detail"] == "2 members: 26 newly scored, 0 still pending."
        for email in (ALICE, BOB):
            assert sorted(r["opp_id"] for r in first.scores if r["email"] == email) == SENT

    def test_a_row_says_what_it_was_scored_for(self, first):
        by = {(r["email"], r["opp_id"]): r for r in first.scores}
        assert by[(ALICE, "E2E-BROAD")]["score_key"] == keys(ALICE_TEXT) and by[(BOB, "E2E-BROAD")]["score_key"] == keys(BOB_TEXT)
        assert keys(ALICE_TEXT).endswith(":p1") and by[(ALICE, "E2E-BROAD")]["model"] == MODEL
        assert by[(ALICE, "E2E-BROAD")]["reason"] == "E2E-BROAD fits the group's profile as scripted." and by[(ALICE, "E2E-BROAD")]["scored_at"] is not None

    def test_each_member_gets_their_own_scores(self, first):
        by = {(r["email"], r["opp_id"]): r["score"] for r in first.scores}
        assert (by[(ALICE, "E2E-BROAD")], by[(ALICE, "E2E-TRUSTED")], by[(ALICE, "E2E-NOKW")]) == (5, 0, SCORES["E2E-NOKW"])
        assert (by[(BOB, "E2E-BROAD")], by[(BOB, "E2E-TRUSTED")]) == (1, SCORES["E2E-TRUSTED"])

    def test_the_team_scores_are_the_teams_and_carry_no_personal_trace(self, first):
        assert {n: first.grants[n]["relevance_score"] for n in SENT} == SCORES
        assert {first.grants[n]["relevance_profile"] for n in SENT} == {score_key(webapp._profile(), MODEL)}
        assert not any(row["relevance_profile"].endswith(":p1") for row in first.grants.values() if row["relevance_profile"])
        assert all(first.grants[n]["relevance_score"] == 0 and first.grants[n]["relevance_model"] == "rule" for n in ("E2E-STATES",))

    def test_the_team_pass_sent_its_prompt_unchanged_and_each_member_theirs_in_the_user_message(self, first):
        team = [r for r in first.requests if r["profile"] is None]
        assert len(team) == 5 and sorted(n for r in team for n in r["numbers"]) == SENT  # 13 grants, 3 at a time
        for request in team:
            assert "Wind-energy" not in request["system"] and "Researcher profile" not in request["user"] and ALICE_TEXT not in request["system"] + request["user"]
        for text, other in ((ALICE_TEXT, BOB_TEXT), (BOB_TEXT, ALICE_TEXT)):
            mine = [r for r in first.requests if r["profile"] == text]
            assert len(mine) == 5 and sorted(n for r in mine for n in r["numbers"]) == SENT
            for request in mine:
                assert text not in request["system"] and other not in request["system"] + request["user"]
                assert "data, not instructions" in request["system"] and f"```\n{text}\n```" in request["user"]
        assert not any(set(r["numbers"]) & set(NOT_SENT) for r in first.requests)  # the pre-check's grants are never sent for anyone

    def test_each_scorer_learns_from_its_own_votes_only(self, first):
        def examples(request):
            return [line for line in request["user"].splitlines() if line.startswith(("+ ", "- "))]

        team = {line for r in first.requests if r["profile"] is None for line in examples(r)}
        alice = {line for r in first.requests if r["profile"] == ALICE_TEXT for line in examples(r)}
        bob = {line for r in first.requests if r["profile"] == BOB_TEXT for line in examples(r)}
        assert team == {"- Carol Dislikes This One For The Team (NSF): Funds things."}
        assert alice == {"- Alice Dislikes This Broad Call (NSF): Funds things."}
        assert bob == {"+ Bob Likes This Trusted Call (NSF): Funds things."}

    def test_the_team_pass_ran_before_the_members_in_the_same_run(self, first):
        order = [("team" if r["profile"] is None else "member") for r in first.requests]
        assert order == ["team"] * 5 + ["member"] * 10  # the team's five requests, then the members' ten
        assert [r["profile"] for r in first.requests[5:]].count(ALICE_TEXT) == 5


@pytest.fixture(scope="module")
def again(world, first):
    world.llm.requests.clear()
    code = world.ingest()
    return SimpleNamespace(code=code, requests=list(world.llm.requests), scores=rows(world), run=personal_run(world))


def test_a_second_run_with_nothing_changed_asks_the_model_nothing(again, first):
    assert again.code == 0 and again.requests == []
    assert again.run["stats"] == {"personal": {"people": 2, "scored": 0, "failed": 0, "pending": 0, "stopped": None}}
    assert again.scores == first.scores  # not a row touched


@pytest.fixture(scope="module")
def edited(world, again):
    """Alice rewrites her profile: her scores go at once, and the next run rescores her and nobody else."""
    world.llm.requests.clear()
    save_member_profile(ALICE, ALICE_EDIT)
    after_save = rows(world)
    code = world.ingest()
    return SimpleNamespace(code=code, after_save=after_save, requests=list(world.llm.requests), scores=rows(world), run=personal_run(world))


class TestAnEdit:
    def test_saving_a_changed_text_deletes_only_their_scores_in_that_step(self, edited, first):
        assert [r["email"] for r in edited.after_save] == [BOB] * 13
        assert edited.after_save == [r for r in first.scores if r["email"] == BOB]

    def test_the_next_run_rescores_that_person_only(self, edited, first):
        assert edited.code == 0 and edited.requests and all(r["profile"] == ALICE_EDIT for r in edited.requests)
        assert edited.run["stats"] == {"personal": {"people": 2, "scored": 13, "failed": 0, "pending": 0, "stopped": None}}
        assert {r["score_key"] for r in edited.scores if r["email"] == ALICE} == {keys(ALICE_EDIT)}
        assert [r for r in edited.scores if r["email"] == BOB] == [r for r in first.scores if r["email"] == BOB]  # untouched


@pytest.fixture(scope="module")
def limited(world, edited):
    """A time stop of zero minutes: the run starts no turn, and the next run, with the default, finishes it."""
    save_member_profile(BOB, BOB_TEXT + " Also hurricanes.")
    world.llm.requests.clear()
    world.mp.setenv("GRANTS_PERSONAL_STOP_MINUTES", "0")
    stopped = SimpleNamespace(code=world.ingest(), requests=list(world.llm.requests), scores=rows(world, BOB), run=personal_run(world))
    world.mp.delenv("GRANTS_PERSONAL_STOP_MINUTES")
    world.llm.requests.clear()
    finished = SimpleNamespace(code=world.ingest(), requests=list(world.llm.requests), scores=rows(world, BOB), run=personal_run(world))
    return SimpleNamespace(stopped=stopped, finished=finished)


class TestTheTimeStop:
    def test_a_stop_before_the_first_turn_leaves_everything_pending_and_is_not_a_failure(self, limited):
        assert limited.stopped.code == 0 and limited.stopped.requests == [] and limited.stopped.scores == []
        run = limited.stopped.run
        assert run["ok"] is True and run["stats"] == {"personal": {"people": 2, "scored": 0, "failed": 0, "pending": 13, "stopped": "time limit"}}
        assert "Stopped: time limit." in run["detail"]

    def test_the_next_run_finishes_the_rest(self, limited):
        assert len(limited.finished.scores) == 13 and limited.finished.run["stats"]["personal"]["stopped"] is None
        assert limited.finished.run["stats"]["personal"]["scored"] == 13 and all(r["profile"] == BOB_TEXT + " Also hurricanes." for r in limited.finished.requests)


class TestWhoIsScored:
    def test_an_unsubscribed_member_is_not_scored_and_keeps_their_profile(self, world, limited):
        save_member_profile(ALICE, ALICE_TEXT)
        world.query("UPDATE recipients SET active = FALSE WHERE email = %s", (ALICE,))
        world.llm.requests.clear()
        assert world.ingest() == 0
        assert all(r["profile"] != ALICE_TEXT for r in world.llm.requests) and rows(world, ALICE) == []
        assert world.query("SELECT summary FROM member_profiles WHERE email = %s", (ALICE,))[0]["summary"] == ALICE_TEXT
        world.query("UPDATE recipients SET active = TRUE WHERE email = %s", (ALICE,))
        world.llm.requests.clear()
        assert world.ingest() == 0
        assert len(rows(world, ALICE)) == 13 and {r["profile"] for r in world.llm.requests} == {ALICE_TEXT}  # resubscribing restores them

    def test_a_member_without_a_profile_is_never_scored(self, world, limited):
        assert rows(world, CAROL) == []
        assert delete_member_profile(BOB) is True
        assert rows(world, BOB) == []  # their scores went with the profile


class TestAWriteForAProfileThatChanged:
    def test_a_profile_deleted_or_edited_while_its_chunk_was_scored_writes_nothing(self, world, limited):
        save_member_profile(BOB, BOB_TEXT)
        entry = {"score": 3, "reason": "r", "summary": "s", "model": MODEL, "scored_at": "2026-11-01T00:00:00+00:00"}
        key = keys(BOB_TEXT)
        assert write_scores(Member(BOB, BOB_TEXT + " (older text)"), key, {"E2E-BROAD": entry}) == 0  # scored for text they have since changed
        assert rows(world, BOB) == []
        assert write_scores(Member(BOB, BOB_TEXT), key, {"E2E-BROAD": entry, "E2E-NOKW": entry}) == 2
        assert write_scores(Member(BOB, BOB_TEXT), key, {"E2E-BROAD": {**entry, "score": 5}}) == 1  # a rescore replaces the row
        assert {r["opp_id"]: r["score"] for r in rows(world, BOB)} == {"E2E-BROAD": 5, "E2E-NOKW": 3}
        assert delete_member_profile(BOB) is True
        assert write_scores(Member(BOB, BOB_TEXT), key, {"E2E-BROAD": entry}) == 0  # the member is gone: no foreign key error, no row
        assert rows(world, BOB) == [] and write_scores(Member(BOB, BOB_TEXT), key, {}) == 0


@pytest.fixture(scope="module")
def outage(world, limited):
    """The team pass stops (the model cuts its answer off) on a new grant: the personal step is skipped; load and email go on."""
    save_member_profile(BOB, BOB_TEXT)
    world.query("DELETE FROM member_scores")
    world.extra = synopsis("E2E-NEW", "A Brand New Network Authentication Program", oid=777001, close=40)
    world.llm.requests.clear()
    world.llm.mode = "truncated"
    world.mp.setenv("GRANTS_NOTIFY_RECIPIENTS", CAROL)
    code = world.ingest()
    notified = world.notify()
    world.llm.mode = "ok"
    return SimpleNamespace(code=code, notified=notified, requests=list(world.llm.requests), run=personal_run(world), scores=rows(world),
                           loaded=world.query("SELECT opp_id FROM grants WHERE opp_id = 'E2E-NEW'"), emails=world.emails(),
                           steps=[r["step"] for r in world.query("SELECT step FROM job_runs ORDER BY id DESC LIMIT 3")])


class TestATeamOutage:
    def test_the_personal_step_is_skipped_and_says_why(self, outage):
        assert outage.run["ok"] is True and outage.run["detail"].startswith("Skipped: the team's scoring stopped: the model used all 2048 completion tokens")
        assert outage.run["stats"]["personal"]["stopped"].startswith("skipped: the team's scoring stopped") and outage.run["stats"]["personal"]["scored"] == 0
        assert outage.scores == [] and all(r["profile"] is None for r in outage.requests)  # nobody was asked a thing for a member

    def test_the_team_load_and_the_email_still_happen(self, outage):
        assert outage.code == 0 and outage.loaded == [{"opp_id": "E2E-NEW"}]
        assert outage.notified == 0 and outage.emails
        assert outage.steps == ["notify", "personal", "ingest"]
