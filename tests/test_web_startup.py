"""The web app applies the database schema once when it starts, and starts without a database (no database)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web import app as webapp  # noqa: E402


class TestApplySchema:
    def test_it_applies_the_schema(self, monkeypatch):
        applied = []
        monkeypatch.setattr(webapp, "ensure_schema", lambda: applied.append(True))
        webapp.apply_schema()
        assert applied == [True]

    def test_a_database_that_is_down_is_logged_and_does_not_stop_the_app(self, monkeypatch, caplog):
        def down():
            raise RuntimeError("connection refused")

        monkeypatch.setattr(webapp, "ensure_schema", down)
        with caplog.at_level(logging.WARNING, logger="grantwatch.web"):
            webapp.apply_schema()  # does not raise
        assert "connection refused" in caplog.text and "schema" in caplog.text

    def test_the_server_does_it_once_at_startup_and_not_again_per_request(self, monkeypatch):
        applied = []
        monkeypatch.setattr(webapp, "ensure_schema", lambda: applied.append(True))
        monkeypatch.setenv("GRANTS_SECRET_KEY", "k" * 32)
        with TestClient(webapp.app, base_url="http://localhost") as client:
            assert applied == [True]
            client.get("/api/session")
            client.get("/api/session")
        assert applied == [True]

    def test_it_comes_up_when_the_schema_cannot_be_applied(self, monkeypatch):
        def down():
            raise RuntimeError("connection refused")

        monkeypatch.setattr(webapp, "ensure_schema", down)
        monkeypatch.setenv("GRANTS_SECRET_KEY", "k" * 32)
        with TestClient(webapp.app, base_url="http://localhost") as client:
            assert client.get("/api/session").status_code == 200
