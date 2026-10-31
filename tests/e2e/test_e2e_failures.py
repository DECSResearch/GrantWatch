"""Flow 5: when the model fails, `ingest` still loads every grant, and the run log and the admin
page say why. Each test starts from an empty database and an empty score cache.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harness import ADMIN_PASSWORD, ADMIN_USER, closed_port
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
