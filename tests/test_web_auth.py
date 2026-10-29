"""Dashboard sign-in, tracking permissions, and unsubscribe links (no database)."""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notifications.planner import Recipient  # noqa: E402
from support import CaptureTransport, plain_text  # noqa: E402
from web import app as webapp  # noqa: E402
from web import auth  # noqa: E402

KEY = "k" * 32
MEMBER = Recipient(id=1, email="me@und.edu", unsubscribe_token="tok")


class TestTokens:
    def test_round_trip_and_tampering(self, monkeypatch):
        monkeypatch.setenv("GRANTS_SECRET_KEY", KEY)
        token = auth.sign(auth.LOGIN, "me@und.edu", 60, now=1000)
        assert auth.verify(token, auth.LOGIN, now=1030) == "me@und.edu"
        assert auth.verify(token, auth.LOGIN, now=1061) is None  # expired
        assert auth.verify(token, auth.SESSION, now=1030) is None  # wrong purpose
        body, sig = token.split(".")
        assert auth.verify(body + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B"), auth.LOGIN, now=1030) is None
        monkeypatch.setenv("GRANTS_SECRET_KEY", "x" * 32)
        assert auth.verify(token, auth.LOGIN, now=1030) is None  # another key

    def test_no_key_no_tokens(self, monkeypatch):
        monkeypatch.delenv("GRANTS_SECRET_KEY", raising=False)
        assert auth.secret_key() is None
        assert auth.verify("a.b", auth.LOGIN) is None
        with pytest.raises(RuntimeError):
            auth.sign(auth.LOGIN, "me@und.edu", 60)


@pytest.fixture
def web(monkeypatch):
    members = {MEMBER.email: MEMBER}
    state = {"active": True, "watch": []}
    outbox = CaptureTransport()
    monkeypatch.setenv("GRANTS_SECRET_KEY", KEY)
    monkeypatch.delenv("GRANTS_DASHBOARD_URL", raising=False)
    monkeypatch.setattr(webapp.store, "active_recipient", lambda email: members.get(email) if state["active"] else None)
    monkeypatch.setattr(webapp.store, "claim_login_email", lambda email: members.get(email.lower()))
    monkeypatch.setattr(webapp.store, "recipient_for_token", lambda t: {"email": MEMBER.email, "active": state["active"]} if t == "tok" else None)

    def unsubscribe(token):
        if token != "tok":
            return None
        state["active"] = False
        return MEMBER.email

    monkeypatch.setattr(webapp.store, "unsubscribe", unsubscribe)
    monkeypatch.setattr(webapp, "add_to_watchlist", lambda opp, note=None: state["watch"].append(opp))
    monkeypatch.setattr(webapp, "get_transport", lambda: outbox)
    client = TestClient(webapp.app, base_url="http://localhost")
    return client, state, outbox


def _link(outbox: CaptureTransport) -> str:
    text = plain_text(outbox.messages[-1])
    return next(line for line in text.splitlines() if "/auth?token=" in line)


class TestSignIn:
    def test_tracking_needs_a_session(self, web):
        client, state, _ = web
        assert client.post("/api/watchlist", json={"opp_id": "A"}).status_code == 401
        assert state["watch"] == []

    def test_fails_closed_without_a_key(self, web, monkeypatch):
        client, state, _ = web
        monkeypatch.delenv("GRANTS_SECRET_KEY")
        assert client.post("/api/watchlist", json={"opp_id": "A"}).status_code == 503
        assert client.get("/api/session").json() == {"auth_configured": False, "email": None}

    def test_magic_link_flow(self, web):
        client, state, outbox = web
        reply = client.post("/api/login", json={"email": "me@und.edu"})
        assert reply.status_code == 200 and len(outbox.messages) == 1
        link = _link(outbox)
        assert link.startswith("http://localhost/auth?token=")
        token = parse_qs(urlsplit(link).query)["token"][0]
        done = client.get(f"/auth?token={token}", follow_redirects=False)
        assert done.status_code == 303 and auth.SESSION_COOKIE in done.cookies
        assert client.get("/api/session").json() == {"auth_configured": True, "email": "me@und.edu"}
        assert client.post("/api/watchlist", json={"opp_id": "A"}).status_code == 201
        assert state["watch"] == ["A"]

    def test_same_reply_for_strangers_and_no_email(self, web):
        client, _, outbox = web
        member = client.post("/api/login", json={"email": "me@und.edu"}).json()
        stranger = client.post("/api/login", json={"email": "who@else.org"}).json()
        assert member == stranger
        assert [m["To"] for m in outbox.messages] == ["me@und.edu"]

    def test_removed_member_loses_access(self, web):
        client, state, outbox = web
        client.post("/api/login", json={"email": "me@und.edu"})
        token = parse_qs(urlsplit(_link(outbox)).query)["token"][0]
        client.get(f"/auth?token={token}", follow_redirects=False)
        state["active"] = False
        assert client.post("/api/watchlist", json={"opp_id": "A"}).status_code == 401

    def test_links_never_built_from_a_public_host_header(self, web):
        _, _, outbox = web
        public = TestClient(webapp.app, base_url="https://evil.example")
        assert public.post("/api/login", json={"email": "me@und.edu"}).status_code == 503
        assert outbox.messages == []

    def test_configured_dashboard_url_wins(self, web, monkeypatch):
        _, _, outbox = web
        monkeypatch.setenv("GRANTS_DASHBOARD_URL", "https://gw.und.edu/")
        public = TestClient(webapp.app, base_url="https://evil.example")
        assert public.post("/api/login", json={"email": "me@und.edu"}).status_code == 200
        assert _link(outbox).startswith("https://gw.und.edu/auth?token=")

    def test_expired_link(self, web):
        client, _, _ = web
        page = client.get("/auth?token=nope", follow_redirects=False)
        assert page.status_code == 200 and "expired" in page.text


class TestUnsubscribe:
    def test_get_only_asks(self, web):
        client, state, _ = web
        page = client.get("/unsubscribe?token=tok")
        assert "Stop GrantWatch email?" in page.text and 'method="post"' in page.text
        assert state["active"] is True  # link scanners fetch GETs

    def test_post_unsubscribes(self, web):
        client, state, _ = web
        # RFC 8058 one-click: the mail client POSTs this body to the List-Unsubscribe URL.
        page = client.post("/unsubscribe?token=tok", data={"List-Unsubscribe": "One-Click"})
        assert page.status_code == 200 and "will not get GrantWatch email" in page.text
        assert state["active"] is False
        assert "Already unsubscribed" in client.get("/unsubscribe?token=tok").text

    def test_unknown_token(self, web):
        client, _, _ = web
        assert "not valid" in client.post("/unsubscribe?token=bad").text
