"""`sign-in-link` against the real recipients table and the real sign-in route."""
from __future__ import annotations

import contextlib
import io
import re

from fastapi.testclient import TestClient

import main
from harness import DASHBOARD_URL
from web import app as webapp


def link(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main.main(["sign-in-link", *args])
    return code, out.getvalue()


def test_the_link_for_an_active_recipient_signs_them_in(fresh):
    assert main.main(["recipients", "add", "lead@und.edu"]) == 0
    code, out = link("LEAD@und.edu")
    found = re.search(r"^(\S+)/auth\?token=(\S+)$", out, re.M)
    assert code == 0 and found and found.group(1) == DASHBOARD_URL
    client = TestClient(webapp.app, base_url="http://localhost")
    assert client.get("/api/session").json()["email"] is None
    assert client.get(f"/auth?token={found.group(2)}", follow_redirects=False).status_code == 303
    assert client.get("/api/session").json()["email"] == "lead@und.edu"


def test_nobody_else_gets_one(fresh):
    main.main(["recipients", "add", "lead@und.edu"])
    assert link("stranger@und.edu")[0] == 1
    fresh.query("UPDATE recipients SET active = FALSE WHERE email = 'lead@und.edu'")
    code, out = link("lead@und.edu")
    assert code == 1 and "/auth" not in out  # unsubscribed
    main.main(["recipients", "remove", "lead@und.edu"])
    assert link("lead@und.edu")[0] == 1


def test_a_link_stops_working_for_a_recipient_removed_after_it_was_made(fresh):
    main.main(["recipients", "add", "lead@und.edu"])
    token = re.search(r"token=(\S+)", link("lead@und.edu")[1]).group(1)
    main.main(["recipients", "remove", "lead@und.edu"])
    client = TestClient(webapp.app, base_url="http://localhost")
    reply = client.get(f"/auth?token={token}", follow_redirects=False)
    assert reply.status_code == 200 and "expired" in reply.text and client.get("/api/session").json()["email"] is None
