"""Every /api answer is private to the cookie that asked and is never cached (no database)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from hypothesis import given, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web import app as webapp  # noqa: E402

KEY = "k" * 32


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GRANTS_SECRET_KEY", KEY)
    return TestClient(webapp.app, base_url="http://localhost")


def private(reply):
    return reply.headers["cache-control"] == "no-store" and "cookie" in reply.headers["vary"].lower()


class TestPrivateApi:
    def test_an_answer_is_marked(self, client):
        reply = client.get("/api/session")
        assert reply.status_code == 200 and private(reply)

    @pytest.mark.parametrize("method,path,status", [
        ("post", "/api/watchlist", 401),  # signed out
        ("get", "/api/no-such-route", 404),
        ("get", "/api/grants?min_score=9", 422),  # refused before the route runs
    ])
    def test_so_is_a_refusal(self, client, method, path, status):
        reply = getattr(client, method)(path, **({"json": {"opp_id": "A"}} if method == "post" else {}))
        assert reply.status_code == status and private(reply)

    def test_a_preflight_is_marked_too(self, client):
        reply = client.options("/api/feedback", headers={"Origin": "https://elsewhere.example", "Access-Control-Request-Method": "POST"})
        assert private(reply)

    def test_the_pages_are_not(self, client):
        assert "cache-control" not in client.get("/").headers

    def test_it_adds_to_a_vary_a_route_already_set_and_does_not_repeat_itself(self):
        app = FastAPI()
        app.middleware("http")(webapp.private_api)

        @app.get("/api/a")
        def a():
            return JSONResponse({}, headers={"Vary": "Accept-Encoding"})

        @app.get("/api/b")
        def b():
            return JSONResponse({}, headers={"Vary": "cookie, Origin"})

        local = TestClient(app)
        assert local.get("/api/a").headers["vary"] == "Accept-Encoding, Cookie"
        assert local.get("/api/b").headers["vary"] == "cookie, Origin"


class TestWithVary:
    @given(names=st.lists(st.sampled_from(["Origin", "Accept-Encoding", "COOKIE", "Accept"]), unique=True, max_size=4))
    def test_the_result_always_names_the_token_once_and_keeps_the_rest(self, names):
        out = webapp._with_vary(", ".join(names) if names else None, "Cookie")
        listed = [name.strip().lower() for name in out.split(",")]
        assert listed.count("cookie") == 1
        assert [n for n in listed if n != "cookie"] == [n.lower() for n in names if n.lower() != "cookie"]

    def test_a_star_stays_a_star(self):
        assert webapp._with_vary("*", "Cookie") == "*"
