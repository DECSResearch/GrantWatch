"""`python main.py sign-in-link EMAIL`: a one-hour sign-in link an admin can paste into chat when mail does not arrive."""
from __future__ import annotations

import contextlib
import io
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402
from notifications import store  # noqa: E402
from notifications.planner import Recipient  # noqa: E402
from web import app as webapp  # noqa: E402
from web import auth  # noqa: E402

KEY = "k" * 32
ME = Recipient(id=1, email="me@und.edu", unsubscribe_token="tok")
TOKEN = re.compile(r"^(\S+)/auth\?token=(\S+)$", re.M)


@pytest.fixture
def members(monkeypatch):
    """One active recipient, found by normalised address as the database would; a key and a dashboard address."""
    monkeypatch.setenv("GRANTS_SECRET_KEY", KEY)
    monkeypatch.setenv("GRANTS_DASHBOARD_URL", "https://grants.example.edu/")

    def active(address):
        try:
            return ME if store.normalise_email(address) == ME.email else None
        except ValueError:
            return None

    monkeypatch.setattr(store, "active_recipient", active)
    monkeypatch.setattr(webapp.store, "active_recipient", active)
    return ME


def run(capsys, address="me@und.edu"):
    code = main.main(["sign-in-link", address])
    return code, capsys.readouterr().out


def link_of(out):
    found = TOKEN.search(out)
    assert found, out
    return found.group(1), found.group(2)


class TestLink:
    def test_it_prints_a_link_to_the_dashboard_that_signs_that_recipient_in_for_an_hour(self, members, capsys):
        now = time.time()
        code, out = run(capsys, "  Me@UND.edu ")  # the stored address is the one signed, however it was typed
        base, token = link_of(out)
        assert code == 0 and base == "https://grants.example.edu"
        assert auth.verify(token, auth.LOGIN, now=now) == "me@und.edu"
        assert auth.verify(token, auth.LOGIN, now=now + 3590) == "me@und.edu"  # an hour, not the emailed link's 15 minutes
        assert auth.verify(token, auth.LOGIN, now=now + 3615) is None
        assert "good for one hour" in out and "me@und.edu" in out.splitlines()[0]

    def test_the_link_is_a_sign_in_link_and_never_a_session(self, members, capsys):
        _, token = link_of(run(capsys)[1])
        assert auth.verify(token, auth.SESSION) is None

    def test_the_dashboards_own_route_accepts_it(self, members, capsys):
        _, token = link_of(run(capsys)[1])
        client = TestClient(webapp.app, base_url="http://localhost")
        reply = client.get(f"/auth?token={token}", follow_redirects=False)
        assert reply.status_code == 303 and auth.SESSION_COOKIE in reply.headers["set-cookie"]
        assert client.get("/api/session").json()["email"] == "me@und.edu"

    @given(st.from_regex(r"[a-z0-9][a-z0-9._%+-]{0,29}@[a-z0-9-]{1,20}\.(edu|org)", fullmatch=True))  # not a leading "-": argparse reads it as a flag
    def test_whoever_the_recipient_is_the_link_is_one_run_of_plain_characters_that_pastes_into_chat_unchanged(self, address):
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("GRANTS_SECRET_KEY", KEY)
            mp.setenv("GRANTS_DASHBOARD_URL", "https://grants.example.edu")
            mp.setattr(store, "active_recipient", lambda a: Recipient(id=2, email=address))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert main.main(["sign-in-link", address]) == 0
            _, token = link_of(out.getvalue())
            assert re.fullmatch(r"[A-Za-z0-9_.-]+", token) and auth.verify(token, auth.LOGIN) == address

    def test_without_a_dashboard_address_it_points_at_this_machine_and_says_so(self, members, monkeypatch, capsys):
        monkeypatch.delenv("GRANTS_DASHBOARD_URL")
        code, out = run(capsys)
        assert code == 0 and link_of(out)[0] == "http://localhost:8000"
        assert "GRANTS_DASHBOARD_URL is not set" in out

    def test_with_one_set_it_does_not_say_that(self, members, capsys):
        assert "GRANTS_DASHBOARD_URL" not in run(capsys)[1]


class TestRefusals:
    def test_no_secret_key_no_link_and_no_database_read(self, members, monkeypatch, capsys):
        monkeypatch.delenv("GRANTS_SECRET_KEY")
        monkeypatch.setattr(store, "active_recipient", lambda a: pytest.fail("the database was read"))
        code, out = run(capsys)
        assert code == 1 and "GRANTS_SECRET_KEY is not set" in out and "/auth" not in out

    def test_a_key_that_is_too_short_is_no_key(self, members, monkeypatch, capsys):
        monkeypatch.setenv("GRANTS_SECRET_KEY", "short")
        assert run(capsys)[0] == 1

    @pytest.mark.parametrize("address", ["stranger@und.edu", "not an address", ""])
    def test_someone_who_is_not_an_active_recipient_gets_none(self, members, capsys, address):
        code, out = run(capsys, address)
        assert code == 1 and "is not an active recipient" in out and "/auth" not in out and "token" not in out

    def test_an_unsubscribed_recipient_is_not_active(self, members, monkeypatch, capsys):
        monkeypatch.setattr(store, "active_recipient", lambda a: None)  # what the query's "AND active" gives
        assert run(capsys)[0] == 1

    def test_a_database_that_cannot_be_read_says_so(self, members, monkeypatch, capsys):
        def down(address):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(store, "active_recipient", down)
        code, out = run(capsys)
        assert code == 1 and "Could not check the recipient list: connection refused" in out and "/auth" not in out
