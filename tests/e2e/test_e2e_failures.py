"""Flow 5: when the model fails, `ingest` still loads every grant, and the run log and the admin
page say why. Each test starts from an empty database and an empty score cache.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from grants.sql_utils import ensure_schema
from harness import ADMIN_PASSWORD, ADMIN_USER, closed_port, new_section, plain_text, read_email
from web import app as webapp

AUTH = (ADMIN_USER, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def client(world):
    return TestClient(webapp.app, base_url="http://localhost")


def ingest_runs(world):
    return world.query("SELECT * FROM job_runs WHERE step = 'ingest' ORDER BY id")


def loaded_but_unscored(world):
    """Every fixture grant is in the database; only the one the pre-check scored itself has a score."""
    grants = {row["opp_id"]: row for row in world.query("SELECT * FROM grants")}
    assert len(grants) == 14
    assert {opp_id for opp_id, row in grants.items() if row["relevance_score"] is not None} == {"E2E-STATES"}
    assert grants["E2E-STATES"]["relevance_model"] == "rule"


class TestTruncatedAnswer:
    def test_scoring_stops_after_one_request_and_the_run_and_admin_page_say_why(self, fresh, client):
        fresh.llm.mode = "truncated"
        assert fresh.ingest() == 0  # the grants are loaded, so the run is not a failure
        loaded_but_unscored(fresh)
        assert len(fresh.llm.requests) == 1  # every other batch would be cut off too, so none was sent

        (run,) = ingest_runs(fresh)
        assert run["ok"] is True and run["finished_at"] is not None
        assert run["stats"]["scoring_abort"].startswith("the model used all 2048 completion tokens before answering")
        assert (run["stats"]["scored"], run["stats"]["failed"], run["stats"]["inserted"]) == (0, 13, 14)
        assert "Scoring stopped: the model used all 2048 completion tokens" in run["detail"]
        assert "Relevance scoring stopped: the model used all 2048" in run["log"]

        warnings = client.get("/admin/api", auth=AUTH).json()["warnings"]
        assert any(w.startswith("Scoring stopped during the newest ingest: the model used all 2048") for w in warnings)
        assert "13 open grants have no relevance score yet: scoring stopped early in the last successful ingest." in warnings

    def test_the_next_run_scores_what_was_left_and_the_warnings_go_away(self, fresh, client):
        fresh.llm.mode = "truncated"
        assert fresh.ingest() == 0
        fresh.llm.mode = "ok"
        assert fresh.ingest() == 0
        failed, recovered = ingest_runs(fresh)
        assert failed["stats"]["failed"] == 13 and "scoring_abort" not in recovered["stats"]
        assert (recovered["stats"]["scored"], recovered["stats"]["cached"], recovered["stats"]["failed"]) == (13, 0, 0)
        assert (recovered["stats"]["inserted"], recovered["stats"]["updated"]) == (0, 14)
        assert fresh.query("SELECT COUNT(*) AS n FROM grants WHERE relevance_score IS NULL")[0]["n"] == 0
        # What is left is about the module's empty recipients table, not about scoring.
        assert client.get("/admin/api", auth=AUTH).json()["warnings"] == ["Nobody gets email yet: there are no active recipients."]


class TestModelUnreachable:
    def test_every_grant_is_still_loaded_and_the_run_and_admin_page_name_the_server(self, fresh, client, monkeypatch):
        port = closed_port()
        monkeypatch.setenv("GRANTS_LLM_BASE_URL", f"http://127.0.0.1:{port}/v1")
        assert fresh.ingest() == 0
        loaded_but_unscored(fresh)
        assert fresh.llm.requests == []

        (run,) = ingest_runs(fresh)
        assert run["ok"] is True
        assert run["stats"]["scoring_abort"].startswith(f"cannot reach http://127.0.0.1:{port}")
        assert (run["stats"]["scored"], run["stats"]["failed"], run["stats"]["inserted"]) == (0, 13, 14)
        assert f"Scoring stopped: cannot reach http://127.0.0.1:{port}" in run["detail"]
        assert "/v1" not in run["detail"]  # the address is shrunk to its host

        status = client.get("/admin/api", auth=AUTH).json()
        assert any(w.startswith(f"Scoring stopped during the newest ingest: cannot reach http://127.0.0.1:{port}") for w in status["warnings"])
        assert "13 open grants have no relevance score yet: the model server could not be reached in the last successful ingest." in status["warnings"]
        check = client.get("/admin/api/llm", auth=AUTH).json()
        assert check["configured"] is True and check["reachable"] is False and check["error"]

    def test_notify_announces_keyword_matches_and_not_the_grant_the_precheck_ruled_out(self, fresh, monkeypatch):
        """The only score in the database is the pre-check's 0 (#47): that is not a model score, so the
        keyword fallback runs, and a grant that leaves this applicant out is not announced by it."""
        monkeypatch.setenv("GRANTS_LLM_BASE_URL", f"http://127.0.0.1:{closed_port()}/v1")
        monkeypatch.setenv("GRANTS_NOTIFY_RECIPIENTS", "lead@und.edu")
        monkeypatch.setenv("GRANTS_EMAIL_MAX_NEW", "50")
        assert fresh.ingest() == 0
        loaded_but_unscored(fresh)
        rows = fresh.query("SELECT opp_id, title, matched_keywords FROM grants WHERE relevance_score IS NULL OR opp_id = 'E2E-STATES'")
        by_id = {row["opp_id"]: row for row in rows}
        assert by_id["E2E-STATES"]["matched_keywords"]  # its text matches, so only the rule keeps it out

        assert fresh.notify() == 0
        (mail,) = fresh.emails()
        text = plain_text(read_email(mail))
        _, entries = new_section(text)
        assert all(score == "unscored" for score, _ in entries)
        # Every unscored grant with a keyword match, one row for the three DURIP postings; none that matched nothing.
        wanted = {row["title"] for opp_id, row in by_id.items() if opp_id != "E2E-STATES" and row["matched_keywords"]}
        assert {title for _, title in entries} == wanted and len(entries) == len(wanted)
        assert "Foundations of Trusted Systems" in wanted and "State and Local Cybersecurity Grant Program" not in text
        assert "match the research keywords in the profile, those closing within 30 days first, then most matches first." in text
        recorded = {row["opp_id"] for row in fresh.query("SELECT opp_id FROM notifications WHERE kind = 'new'")}
        assert "E2E-STATES" not in recorded and "E2E-NOKW" not in recorded


class TestFeedbackTableMissing:
    def test_the_list_still_answers_at_a_score_floor_before_anyone_has_made_the_table(self, fresh, client):
        # A database the daily job has not brought up to date: the grants are there, grant_feedback is not.
        assert fresh.ingest() == 0
        fresh.query("DROP TABLE grant_feedback")
        try:
            floor = client.get("/api/grants?min_score=4&limit=1000")
            plain = client.get("/api/grants?limit=1000")
        finally:
            ensure_schema()  # the module's later tests expect the table
        for reply in (floor, plain):
            assert reply.status_code == 200 and reply.json()["count"] > 0
            assert all(row["votes_up"] is None and row["team_verdict"] is None for row in reply.json()["results"])
        assert 0 < floor.json()["count"] < plain.json()["count"]  # the floor still applies
