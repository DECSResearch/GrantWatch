"""What the web API gives a visitor who is not signed in (no database).

Every function the routes call for a member is replaced by one that fails, so a visitor's request
that reached the team's scores, votes or watchlist would fail the test.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings, strategies as st
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grants.display import PUBLIC_DETAIL_FIELDS  # noqa: E402
from notifications.planner import Recipient  # noqa: E402
from web import app as webapp  # noqa: E402
from web import auth  # noqa: E402

KEY = "k" * 32
ME = Recipient(id=1, email="me@und.edu", unsubscribe_token="tok")
PROFILE_TEXT = "We build cyber-physical security for the grid and its sensors."
MEMBER_FUNCTIONS = ("query_grants", "count_grants", "fetch_vote_counts", "fetch_relevant_ids", "get_grant", "data_status", "list_watchlist")
ALLOWED = set(PUBLIC_DETAIL_FIELDS) | {"also_posted", "superseded_by"}
NESTED = {"opp_id", "agency", "url"}


def public(opp, **kw):
    """A row as ``query_public_grants`` makes it."""
    row = {"opp_id": opp, "title": f"Grant {opp}", "agency": "NSF", "agency_code": "NSF", "url": f"https://x/{opp}", "close_date": "2026-11-20T00:00:00",
           "post_date": "2026-09-01", "opportunity_status": "Posted", "summary": "Funds things.", "stage": "full", "days_left": 21, "firm_deadline": True}
    row.update(kw)
    return row


@pytest.fixture
def web(monkeypatch):
    state = SimpleNamespace(rows=[], queries=[], counted=[], on=True, status={"open": 3, "data_as_of": "2026-10-30T06:00:00"}, status_error=None,
                            detail={}, detail_error=None, watchlist=[], asked=[])

    def query(q):
        state.queries.append(q)
        return [dict(r) for r in state.rows]

    def count(q):
        state.counted.append(q)
        return 99

    def detail(opp_id):
        state.asked.append(opp_id)
        if state.detail_error:
            raise state.detail_error
        return state.detail.get(opp_id)

    def status():
        if state.status_error:
            raise state.status_error
        return dict(state.status)

    def never(name):
        def call(*args, **kwargs):
            raise AssertionError(f"a visitor's request reached {name}")

        return call

    for name in MEMBER_FUNCTIONS:
        monkeypatch.setattr(webapp, name, never(name))
    monkeypatch.setenv("GRANTS_SECRET_KEY", KEY)
    monkeypatch.setattr(webapp.store, "active_recipient", lambda email: ME if email == ME.email else None)
    monkeypatch.setattr(webapp, "_public_list_on", lambda: state.on)
    monkeypatch.setattr(webapp, "query_public_grants", query)
    monkeypatch.setattr(webapp, "count_public_grants", count)
    monkeypatch.setattr(webapp, "get_public_grant", detail)
    monkeypatch.setattr(webapp, "public_status", status)
    return TestClient(webapp.app, base_url="http://localhost"), state


def sign_in(client):
    client.cookies.set(auth.SESSION_COOKIE, auth.sign(auth.SESSION, ME.email, 3600))
    return client


class TestVisitorList:
    def test_a_visitor_gets_the_public_rows_grouped(self, web):
        client, state = web
        state.rows = [public("B-1", title="Instrumentation Program (DURIP)", agency_code="DOD-ONR"), public("A-1", title="Instrumentation Program (DURIP)", agency_code="DOD-AMC"),
                      public("C-1", title="Something else")]
        reply = client.get("/api/grants")
        body = reply.json()
        assert reply.status_code == 200 and set(body) == {"results", "count", "total"}
        assert body["count"] == 2 and body["total"] == 3
        assert [r["opp_id"] for r in body["results"]] == ["A-1", "C-1"]  # the lowest number leads, whatever the team scores
        assert body["results"][0]["also_posted"] == [{"opp_id": "B-1", "agency": "NSF", "url": "https://x/B-1"}]
        assert body["results"][0]["superseded_by"] is None

    def test_it_is_the_same_with_no_secret_key_on_the_server(self, web, monkeypatch):
        client, state = web
        monkeypatch.delenv("GRANTS_SECRET_KEY")
        state.rows = [public("A-1")]
        assert client.get("/api/grants").json()["count"] == 1

    def test_a_removed_member_with_an_old_cookie_is_a_visitor(self, web, monkeypatch):
        client, state = web
        sign_in(client)
        monkeypatch.setattr(webapp.store, "active_recipient", lambda email: None)
        state.rows = [public("A-1")]
        assert client.get("/api/grants").json()["results"][0]["opp_id"] == "A-1"

    def test_a_score_floor_the_watchlist_and_closed_grants_are_not_a_visitors_to_ask_for(self, web):
        client, state = web
        state.rows = [public("A-1")]
        plain = client.get("/api/grants?limit=10").json()
        asked = client.get("/api/grants?limit=10&min_score=5&watched_only=true&include_closed=true").json()
        assert asked == plain
        query = state.queries[-1]
        assert (query.min_score, query.watched_only, query.include_closed, query.team_relevant) == (None, False, False, ())

    def test_the_filters_a_visitor_has_reach_the_query(self, web):
        client, state = web
        client.get("/api/grants?q=wind&closing_within=30&include_forecasted=false&stage=full&due_from=2026-10-01&due_to=2026-12-01&limit=7")
        query = state.queries[0]
        assert (query.q, query.closing_within, query.include_forecasted, query.stage, query.limit) == ("wind", 30, False, "full", 7)
        assert (query.due_from, query.due_to, query.today) == (date(2026, 10, 1), date(2026, 12, 1), date.today())

    def test_a_bad_filter_is_still_refused(self, web):
        client, _ = web
        assert client.get("/api/grants?stage=bogus").status_code == 400
        assert client.get("/api/grants?due_from=2026-12-01&due_to=2026-10-01").status_code == 400

    def test_the_total_is_counted_only_when_the_list_is_full(self, web):
        client, state = web
        state.rows = [public("A-1"), public("B-1")]
        assert client.get("/api/grants?limit=5").json()["total"] == 2 and state.counted == []
        assert client.get("/api/grants?limit=2").json()["total"] == 99 and len(state.counted) == 1

    def test_a_database_error_says_nothing_of_its_cause(self, web, monkeypatch):
        client, _ = web

        def down(q):
            raise RuntimeError("password=hunter2")

        monkeypatch.setattr(webapp, "query_public_grants", down)
        reply = client.get("/api/grants")
        assert reply.status_code == 500 and "hunter2" not in reply.text

    def test_with_no_list_there_is_nothing_to_show_and_the_database_is_not_asked(self, web):
        client, state = web
        state.on, state.rows = False, [public("A-1")]
        assert client.get("/api/grants").json() == {"results": [], "count": 0, "total": 0}
        assert state.queries == []

    def test_a_member_is_not_a_visitor(self, web, monkeypatch):
        client, _ = web
        sign_in(client)
        monkeypatch.setattr(webapp, "query_grants", lambda q: [])
        monkeypatch.setattr(webapp, "fetch_vote_counts", lambda ids, email=None: {})
        assert client.get("/api/grants").json() == {"results": [], "count": 0, "total": 0}  # reached the member's query, not the public one

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        params=st.dictionaries(
            st.sampled_from(["q", "min_score", "watched_only", "include_closed", "show_hidden", "sort", "order_by", "scores", "closing_within", "stage", "limit", "include_forecasted"]),
            st.sampled_from(["", "0", "5", "true", "false", "score", "relevance_score", "-relevance_score", "mine", "team", "30", "full", "concept", "wind", "10"]),
            max_size=8,
        ),
    )
    def test_nothing_internal_comes_back_for_any_parameters_even_from_a_query_that_leaks(self, web, params):
        client, state = web
        leaky = {
            "relevance_score": 5, "relevance_reason": PROFILE_TEXT, "relevance_model": "m", "matched_keywords": "cyber; grid", "watched": True,
            "watch_note": PROFILE_TEXT, "my_verdict": 1, "votes_up": 4, "votes_down": 0, "team_verdict": 1, "hidden": False, "first_seen_at": "2026-09-02",
            "profile": {"summary": PROFILE_TEXT},
        }
        state.rows = [public("A-1", title="Instrumentation Program (DURIP)", agency_code="DOD-AMC", **leaky), public("B-1", title="Instrumentation Program (DURIP)", agency_code="DOD-ONR", **leaky),
                      public("C-1", **leaky)]
        reply = client.get("/api/grants", params=params)
        if reply.status_code != 200:  # a parameter the route refuses (a bad stage) says so and shows nothing
            assert PROFILE_TEXT not in reply.text
            return
        text = json.dumps(reply.json())
        assert PROFILE_TEXT not in text and "cyber" not in text
        for row in reply.json()["results"]:
            assert set(row) <= ALLOWED
            assert all(set(entry) <= NESTED for entry in row["also_posted"])


class TestVisitorProfile:
    def test_it_says_whether_there_is_a_list_how_many_are_open_and_when_it_was_loaded(self, web):
        client, _ = web
        assert client.get("/api/profile").json() == {"configured": True, "data": {"open": 3, "data_as_of": "2026-10-30T06:00:00"}}

    def test_with_no_list_it_says_so_and_does_not_read_the_database(self, web):
        client, state = web
        state.on, state.status_error = False, AssertionError("read the database")
        assert client.get("/api/profile").json() == {"configured": False, "data": {"open": 0, "data_as_of": None}}

    def test_a_status_that_cannot_be_read_is_null_and_logged(self, web, caplog):
        client, state = web
        state.status_error = RuntimeError("down")
        with caplog.at_level(logging.WARNING, logger="grantwatch.web"):
            assert client.get("/api/profile").json() == {"configured": True, "data": None}
        assert "Could not read data status" in caplog.text

    def test_a_member_still_gets_the_whole_profile(self, web, monkeypatch):
        client, _ = web
        sign_in(client)
        monkeypatch.setattr(webapp, "data_status", lambda min_score=None: {"open": 1})
        body = client.get("/api/profile").json()
        assert {"summary", "institution", "min_score", "horizons", "data"} <= set(body)


class TestVisitorDetail:
    def test_it_is_the_public_row_with_the_description(self, web):
        client, state = web
        state.detail = {"A-1": public("A-1", description="The whole call.")}
        reply = client.get("/api/grants/A-1")
        assert reply.status_code == 200 and reply.json()["description"] == "The whole call." and state.asked == ["A-1"]

    def test_a_number_with_a_slash_is_taken_whole(self, web):
        client, state = web
        state.detail = {"HHS/2026/1": public("HHS/2026/1")}
        assert client.get("/api/grants/HHS/2026/1").status_code == 200

    def test_an_unknown_grant_is_a_404(self, web):
        client, _ = web
        assert client.get("/api/grants/NOPE").status_code == 404

    def test_with_no_list_even_a_known_grant_is_a_404_and_is_not_looked_up(self, web):
        client, state = web
        state.on, state.detail = False, {"A-1": public("A-1")}
        assert client.get("/api/grants/A-1").status_code == 404 and state.asked == []

    def test_a_database_error_says_nothing_of_its_cause(self, web):
        client, state = web
        state.detail_error = RuntimeError("password=hunter2")
        reply = client.get("/api/grants/A-1")
        assert reply.status_code == 500 and "hunter2" not in reply.text


class TestWatchlist:
    def test_reading_it_needs_a_sign_in(self, web):
        client, _ = web
        assert client.get("/api/watchlist").status_code == 401

    def test_and_a_server_without_sign_in_has_no_watchlist_to_show(self, web, monkeypatch):
        client, _ = web
        monkeypatch.delenv("GRANTS_SECRET_KEY")
        assert client.get("/api/watchlist").status_code == 503

    def test_a_member_reads_it(self, web, monkeypatch):
        client, _ = web
        sign_in(client)
        monkeypatch.setattr(webapp, "list_watchlist", lambda: [{"opp_id": "A-1", "created_at": date(2026, 10, 1), "close_date": None}])
        reply = client.get("/api/watchlist")
        assert reply.status_code == 200 and reply.json()["results"][0]["opp_id"] == "A-1"
