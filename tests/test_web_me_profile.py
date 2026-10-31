"""A member reads, writes and removes their own research profile over /api/me/profile (no database)."""
from __future__ import annotations

import json
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grants.members import MAX_SUMMARY_CHARS, MIN_SUMMARY_CHARS, clean_summary  # noqa: E402
from notifications.planner import Recipient  # noqa: E402
from web import app as webapp  # noqa: E402
from web import auth  # noqa: E402

KEY = "k" * 32
ME = Recipient(id=1, email="me@und.edu", unsubscribe_token="tok")
TEXT = "I build battery models for grid-scale storage, mostly lithium and sodium chemistries."
WHEN = datetime(2026, 10, 30, 8, 15)


@pytest.fixture
def web(monkeypatch):
    s = SimpleNamespace(stored=None, saved=[], deleted=[], boom=None, row_error=None)

    def save(email, text):
        if s.boom:
            raise s.boom
        s.saved.append((email, text))
        s.stored = clean_summary(text)  # what the real function stores: the cleaned text, or ProfileError
        return s.stored

    def fetch(email):
        if s.row_error:
            raise s.row_error
        return {"summary": s.stored, "updated_at": WHEN} if s.stored is not None else None

    def delete(email):
        s.deleted.append(email)
        gone, s.stored = s.stored is not None, None
        return gone

    monkeypatch.setenv("GRANTS_SECRET_KEY", KEY)
    monkeypatch.setattr(webapp.store, "active_recipient", lambda email: ME if email == ME.email else None)
    monkeypatch.setattr(webapp, "save_member_profile", save)
    monkeypatch.setattr(webapp, "fetch_member_profile", fetch)
    monkeypatch.setattr(webapp, "delete_member_profile", delete)
    client = TestClient(webapp.app, base_url="http://localhost")
    return client, s


def sign_in(client):
    client.cookies.set(auth.SESSION_COOKIE, auth.sign(auth.SESSION, ME.email, 3600))
    return client


class TestMembersOnly:
    @pytest.mark.parametrize("method", ["get", "put", "delete"])
    def test_a_visitor_is_refused_and_nothing_is_touched(self, web, method):
        client, s = web
        kwargs = {"json": {"summary": TEXT}} if method == "put" else {}
        assert getattr(client, method)("/api/me/profile", **kwargs).status_code == 401
        assert s.saved == [] and s.deleted == []

    @pytest.mark.parametrize("method", ["get", "put", "delete"])
    def test_a_server_with_no_sign_in_has_no_profiles_to_edit(self, web, monkeypatch, method):
        client, s = web
        monkeypatch.delenv("GRANTS_SECRET_KEY")
        kwargs = {"json": {"summary": TEXT}} if method == "put" else {}
        assert getattr(client, method)("/api/me/profile", **kwargs).status_code == 503
        assert s.saved == [] and s.deleted == []

    def test_a_removed_member_with_an_old_cookie_is_refused(self, web, monkeypatch):
        client, s = web
        sign_in(client)
        monkeypatch.setattr(webapp.store, "active_recipient", lambda email: None)
        assert client.put("/api/me/profile", json={"summary": TEXT}).status_code == 401
        assert s.saved == []


class TestReading:
    def test_a_member_on_the_team_profile_has_none(self, web):
        client, _ = web
        sign_in(client)
        assert client.get("/api/me/profile").json() == {"has_profile": False, "summary": None, "updated_at": None}

    def test_a_member_with_a_profile_reads_it_back(self, web):
        client, s = web
        sign_in(client)
        s.stored = TEXT
        assert client.get("/api/me/profile").json() == {"has_profile": True, "summary": TEXT, "updated_at": "2026-10-30T08:15:00"}

    def test_a_database_error_says_nothing_of_its_cause(self, web):
        client, s = web
        sign_in(client)
        s.row_error = RuntimeError("password=hunter2")
        reply = client.get("/api/me/profile")
        assert reply.status_code == 500 and "hunter2" not in reply.text


class TestWriting:
    def test_it_stores_the_text_for_the_signed_in_member_and_answers_with_it_cleaned(self, web):
        client, s = web
        sign_in(client)
        reply = client.put("/api/me/profile", json={"summary": "  I build battery models\nfor grid-scale storage, mostly lithium and sodium.  "})
        assert reply.status_code == 200
        assert s.saved == [(ME.email, "  I build battery models\nfor grid-scale storage, mostly lithium and sodium.  ")]
        assert reply.json() == {"has_profile": True, "summary": "I build battery models for grid-scale storage, mostly lithium and sodium.", "updated_at": "2026-10-30T08:15:00"}

    @pytest.mark.parametrize("text,words", [
        ("too short", "at least 40 characters"),
        ("x" * 2001, "2,000 characters or fewer"),
        ("REPLACE ME with what you work on, in a sentence or two of your own", "Replace the placeholder"),
        ("", "at least 40 characters"),
        ("​\x00" * 30, "at least 40 characters"),
    ])
    def test_a_text_that_cannot_be_stored_is_a_400_that_says_why_and_stores_nothing(self, web, text, words):
        client, s = web
        sign_in(client)
        reply = client.put("/api/me/profile", json={"summary": text})
        assert reply.status_code == 400 and words in reply.json()["detail"]
        assert s.stored is None

    @pytest.mark.parametrize("body", [{}, {"summary": None}, {"summary": 5}, {"summary": ["a"]}, []])
    def test_a_body_that_is_not_a_summary_is_refused(self, web, body):
        client, s = web
        sign_in(client)
        assert client.put("/api/me/profile", json=body).status_code == 422
        assert s.saved == []

    def test_only_the_summary_is_taken_so_no_threshold_rides_along(self, web):
        client, s = web
        sign_in(client)
        assert client.put("/api/me/profile", json={"summary": TEXT, "min_score": 1, "email": "other@und.edu"}).status_code == 200
        assert s.saved == [(ME.email, TEXT)]  # the member is the signed-in one, whatever the body says

    def test_a_database_error_says_nothing_of_its_cause(self, web):
        client, s = web
        sign_in(client)
        s.boom = RuntimeError("password=hunter2")
        reply = client.put("/api/me/profile", json={"summary": TEXT})
        assert reply.status_code == 500 and "hunter2" not in reply.text

    def test_a_lone_surrogate_a_browser_can_send_as_an_escape_is_dropped_not_stored(self, web):
        client, s = web
        sign_in(client)
        raw = b'{"summary": "grid storage models grid storage models grid storage models \\ud800 and more\\udfff"}'
        reply = client.put("/api/me/profile", content=raw, headers={"Content-Type": "application/json"})
        assert reply.status_code == 200 and reply.json()["summary"] == "grid storage models " * 3 + "and more"

    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(text=st.one_of(
        st.text(alphabet=st.characters(exclude_categories=["Cs"]), max_size=2600),
        st.text(alphabet=st.characters(exclude_categories=["Cs"]), min_size=40, max_size=120),
        st.lists(st.sampled_from(["grid", "storage", "\n", "\t", "​", "\x00", "‮", "  ", "model"]), min_size=10, max_size=40).map("".join),
    ))
    def test_what_is_stored_has_no_control_characters_and_a_length_within_bounds_or_the_answer_is_a_400(self, web, text):
        client, s = web
        sign_in(client)
        s.stored = None
        reply = client.put("/api/me/profile", json={"summary": text})
        if reply.status_code == 400:
            assert s.stored is None
            return
        assert reply.status_code == 200
        stored = reply.json()["summary"]
        assert s.stored == stored and MIN_SUMMARY_CHARS <= len(stored) <= MAX_SUMMARY_CHARS
        assert not [ch for ch in stored if not ch.isspace() and unicodedata.category(ch)[0] == "C"]
        assert "\n" not in stored and "  " not in stored and stored == stored.strip()


class TestOnlyJson:
    """The profile is taken from a body that says it is JSON, and from nothing a cross-site form or fetch can send unasked."""

    BODY = json.dumps({"summary": TEXT}).encode()

    @pytest.mark.parametrize("content_type", ["text/plain", "text/plain;charset=UTF-8", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "application/jsonx", None])
    def test_a_put_that_is_not_declared_json_is_refused_though_the_cookie_is_good_and_the_body_is_json(self, web, content_type):
        client, s = web
        sign_in(client)
        headers = {"Content-Type": content_type} if content_type else {}
        reply = client.put("/api/me/profile", content=self.BODY, headers=headers)
        assert reply.status_code == 415 and "JSON" in reply.json()["detail"]
        assert s.saved == [] and s.stored is None

    @pytest.mark.parametrize("content_type", ["application/json", "application/json; charset=utf-8", "Application/JSON"])
    def test_declared_json_is_taken(self, web, content_type):
        client, s = web
        sign_in(client)
        assert client.put("/api/me/profile", content=self.BODY, headers={"Content-Type": content_type}).status_code == 200
        assert s.saved == [(ME.email, TEXT)]

    def test_a_form_post_cannot_stand_in_for_the_put_or_the_delete(self, web):
        client, s = web
        sign_in(client)
        for method in ("post", "patch"):
            assert getattr(client, method)("/api/me/profile", data={"summary": TEXT}).status_code == 405
        assert client.post("/api/me/profile/delete").status_code == 404 and client.post("/api/me/profile/save", data={"summary": TEXT}).status_code == 404
        assert client.get("/api/me/profile?_method=PUT&summary=" + TEXT).status_code == 200 and s.saved == [] and s.deleted == []

    def test_cors_still_does_not_allow_credentials_from_another_site(self, web):
        client, _ = web
        reply = client.options("/api/me/profile", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "PUT", "Access-Control-Request-Headers": "content-type"})
        assert "access-control-allow-credentials" not in reply.headers


class TestRemoving:
    def test_it_returns_the_member_to_the_team_profile(self, web):
        client, s = web
        sign_in(client)
        s.stored = TEXT
        assert client.delete("/api/me/profile").json() == {"has_profile": False, "summary": None, "updated_at": None, "removed": True}
        assert s.deleted == [ME.email]
        assert client.get("/api/me/profile").json()["has_profile"] is False

    def test_removing_what_is_not_there_says_so(self, web):
        client, s = web
        sign_in(client)
        reply = client.delete("/api/me/profile")
        assert reply.status_code == 200 and reply.json()["removed"] is False

    def test_a_database_error_says_nothing_of_its_cause(self, web, monkeypatch):
        client, _ = web
        sign_in(client)

        def down(email):
            raise RuntimeError("password=hunter2")

        monkeypatch.setattr(webapp, "delete_member_profile", down)
        reply = client.delete("/api/me/profile")
        assert reply.status_code == 500 and "hunter2" not in reply.text
