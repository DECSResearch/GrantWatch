"""What the web API gives a signed-in member: the team's scores, or their own once they have any (no database).

Every database function the routes call is replaced by a recording fake.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grants.feedback import Votes  # noqa: E402
from grants.members import personal_profile  # noqa: E402
from grants.profile import Profile  # noqa: E402
from llm_utils.relevance import LLMSettings, personal_key  # noqa: E402
from notifications.planner import Recipient  # noqa: E402
from web import app as webapp  # noqa: E402
from web import auth  # noqa: E402

KEY = "k" * 32
TEAM = Profile(summary="We study cyber-physical security. We also teach.", institution="UND", min_score_to_notify=4)
CLEAN = "I build battery models for grid-scale storage, mostly lithium and sodium chemistries."
HAND_TYPED = "I build battery models for grid-scale storage,\nmostly lithium and sodium chemistries.  "
UPDATED = datetime(2026, 10, 30, 8, 15)


def state(profile=False, scores=False, summary=CLEAN):
    return {"has_profile": profile, "summary": summary if profile else None, "updated_at": UPDATED if profile else None, "has_scores": scores}


def row(opp, **kw):
    out = {"opp_id": opp, "title": f"Grant {opp}", "agency_code": opp, "close_date": "2026-11-06", "post_date": "2026-08-01", "opportunity_status": "Posted",
           "relevance_score": 4, "watched": False, "url": f"https://x/{opp}", "also_posted": []}
    out.update(kw)
    return out


@pytest.fixture
def web(monkeypatch):
    me = Recipient(id=1, email="me@und.edu", unsubscribe_token="tok")
    s = SimpleNamespace(me=me, member=state(), rows=[], queries=[], counted=[], relevant=[], relevant_asked=[], counts={}, counts_asked=[], status_asked=[],
                        status={"total": 9, "open": 8, "scored": 8, "watched": 0, "fit_count": 3, "data_as_of": "2026-10-30T06:00:00"}, coverage=None,
                        coverage_asked=[], coverage_error=None, detail_asked=[], model="model-one")

    def relevant(own=None):
        s.relevant_asked.append(own)
        return list(s.relevant)

    def counts(opp_ids, email=None):
        s.counts_asked.append((sorted(opp_ids), email))
        return dict(s.counts)

    def status(min_score=None, scores_for=None):
        s.status_asked.append((min_score, scores_for))
        return dict(s.status)

    def coverage(email, key):
        s.coverage_asked.append((email, key))
        if s.coverage_error:
            raise s.coverage_error
        return dict(s.coverage)

    def detail(opp_id, scores_for=None):
        s.detail_asked.append((opp_id, scores_for))
        return {"opp_id": opp_id, "relevance_score": 2}

    monkeypatch.setenv("GRANTS_SECRET_KEY", KEY)
    monkeypatch.setattr(webapp.store, "active_recipient", lambda email: s.me if email == s.me.email else None)
    monkeypatch.setattr(webapp, "_profile", lambda: TEAM)
    monkeypatch.setattr(webapp.LLMSettings, "from_env", classmethod(lambda cls: LLMSettings(model=s.model)))
    monkeypatch.setattr(webapp, "fetch_member_state", lambda email: dict(s.member))
    monkeypatch.setattr(webapp, "fetch_relevant_ids", relevant)
    monkeypatch.setattr(webapp, "fetch_vote_counts", counts)
    monkeypatch.setattr(webapp, "query_grants", lambda q: (s.queries.append(q), [dict(r) for r in s.rows])[1])
    monkeypatch.setattr(webapp, "count_grants", lambda q: (s.counted.append(q), 99)[1])
    monkeypatch.setattr(webapp, "data_status", status)
    monkeypatch.setattr(webapp, "member_coverage", coverage)
    monkeypatch.setattr(webapp, "get_grant", detail)
    client = TestClient(webapp.app, base_url="http://localhost")
    client.cookies.set(auth.SESSION_COOKIE, auth.sign(auth.SESSION, me.email, 3600))
    return client, s


class TestWhoseScores:
    def test_a_member_on_the_team_profile_is_shown_the_teams_scores_and_the_teams_votes(self, web):
        client, s = web
        s.rows, s.counts = [row("A")], {"A": Votes(2, 1, 1)}
        s.relevant = ["A"]
        body = client.get("/api/grants?min_score=4").json()
        assert body["scores"] == "team" and s.queries[0].scores_for is None
        assert s.relevant_asked == [None]  # the team's Relevant marks pass the floor
        assert (body["results"][0]["votes_up"], body["results"][0]["team_verdict"], body["results"][0]["my_verdict"]) == (2, 1, 1)

    def test_a_member_whose_profile_has_no_score_yet_is_shown_the_teams_scores_but_votes_for_themselves(self, web):
        client, s = web
        s.member = state(profile=True, scores=False)
        s.rows, s.counts = [row("A"), row("B")], {"A": Votes(5, 0, -1), "B": Votes(0, 4, None)}
        s.relevant = ["B"]
        body = client.get("/api/grants?min_score=4").json()
        assert body["scores"] == "team" and s.queries[0].scores_for is None  # until the first score is stored
        assert s.relevant_asked == ["me@und.edu"]  # their own Relevant marks pass their floor, not the team's
        by_id = {r["opp_id"]: r for r in body["results"]}
        assert (by_id["A"]["my_verdict"], by_id["A"]["hidden"], by_id["A"]["votes_up"], by_id["A"]["team_verdict"]) == (-1, True, None, None)
        assert by_id["B"]["hidden"] is False and by_id["B"]["votes_down"] is None  # the team's Not for us is not theirs

    def test_a_member_with_a_profile_and_a_score_is_shown_their_own(self, web):
        client, s = web
        s.member = state(profile=True, scores=True)
        s.rows = [row("A", relevance_score=5)]
        body = client.get("/api/grants?min_score=3").json()
        assert body["scores"] == "mine" and s.queries[0].scores_for == "me@und.edu"
        assert s.counted == [] and s.relevant_asked == ["me@und.edu"]

    @pytest.mark.parametrize("profile,scores,shown", [(False, False, "team"), (False, True, "team"), (True, False, "team"), (True, True, "mine")])
    def test_their_own_scores_need_a_profile_and_at_least_one_score_under_any_key(self, web, profile, scores, shown):
        client, s = web
        s.member = state(profile=profile, scores=scores)
        assert client.get("/api/grants").json()["scores"] == shown
        assert (s.queries[0].scores_for == "me@und.edu") == (shown == "mine")

    def test_the_total_is_counted_by_the_same_scores(self, web):
        client, s = web
        s.member = state(profile=True, scores=True)
        s.rows = [row("A")]
        assert client.get("/api/grants?limit=1").json()["total"] == 99
        assert s.counted[0].scores_for == "me@und.edu"

    def test_there_is_no_server_side_floor_the_page_asks_for_the_one_it_wants(self, web):
        client, s = web
        s.me = Recipient(id=1, email="me@und.edu", min_score=3, unsubscribe_token="tok")
        client.get("/api/grants")
        assert s.queries[0].min_score is None and s.relevant_asked == []  # no floor, so no votes read for it

    def test_a_profile_that_cannot_be_read_is_a_500_that_says_nothing_of_the_cause(self, web, monkeypatch):
        client, _ = web

        def down(email):
            raise RuntimeError("password=hunter2")

        monkeypatch.setattr(webapp, "fetch_member_state", down)
        reply = client.get("/api/grants")
        assert reply.status_code == 500 and "hunter2" not in reply.text

    def test_one_grants_detail_is_scored_the_same_way_as_the_list(self, web):
        client, s = web
        client.get("/api/grants/A-1")
        s.member = state(profile=True, scores=True)
        client.get("/api/grants/A-1")
        s.member = state(profile=True, scores=False)
        client.get("/api/grants/A-1")
        assert s.detail_asked == [("A-1", None), ("A-1", "me@und.edu"), ("A-1", None)]


class TestProfile:
    def ask(self, client):
        reply = client.get("/api/profile")
        assert reply.status_code == 200
        return reply.json()

    def test_a_member_on_the_team_profile(self, web):
        client, s = web
        body = self.ask(client)
        assert body["configured"] is True and body["summary"] == "We study cyber-physical security" and body["min_score"] == 4
        assert body["me"] == {"has_profile": False, "updated_at": None, "min_score": 4, "coverage": {"open": 8, "scored": 0}, "using": "team"}
        assert s.status_asked == [(4, None)] and s.coverage_asked == []  # nothing is scored for a profile they do not have

    def test_their_default_floor_is_their_own_threshold_and_the_team_threshold_is_still_reported(self, web):
        client, s = web
        s.me = Recipient(id=1, email="me@und.edu", min_score=5, unsubscribe_token="tok")
        body = self.ask(client)
        assert body["min_score"] == 4 and body["me"]["min_score"] == 5
        assert s.status_asked == [(5, None)]  # the line over the list counts at the floor they will see

    def test_a_member_with_a_profile_and_scores_is_counted_by_their_scores(self, web):
        client, s = web
        s.member, s.coverage = state(profile=True, scores=True), {"open": 8, "scored": 5}
        body = self.ask(client)
        assert body["me"] == {"has_profile": True, "updated_at": "2026-10-30T08:15:00", "min_score": 4, "coverage": {"open": 8, "scored": 5}, "using": "mine"}
        assert s.status_asked == [(4, "me@und.edu")]

    def test_one_with_a_profile_and_no_score_yet_is_on_the_teams_until_the_run(self, web):
        client, s = web
        s.member, s.coverage = state(profile=True, scores=False), {"open": 8, "scored": 0}
        me = self.ask(client)["me"]
        assert (me["using"], me["has_profile"], me["coverage"]) == ("team", True, {"open": 8, "scored": 0})
        assert s.status_asked == [(4, None)]

    def test_coverage_is_asked_for_with_the_key_of_the_cleaned_text_not_the_text_as_stored(self, web):
        client, s = web
        s.member, s.coverage = state(profile=True, scores=True, summary=HAND_TYPED), {"open": 8, "scored": 8}
        self.ask(client)
        (email, key), = s.coverage_asked
        assert email == "me@und.edu"
        assert key == personal_key(personal_profile(TEAM, CLEAN), "model-one")
        assert key != personal_key(personal_profile(TEAM, HAND_TYPED), "model-one")  # the key of the raw column would match none of their scores

    def test_the_key_follows_the_model_the_scoring_pass_would_use(self, web):
        client, s = web
        s.member, s.coverage = state(profile=True, scores=True), {"open": 8, "scored": 0}
        self.ask(client)
        s.model = "model-two"
        self.ask(client)
        assert s.coverage_asked[0][1] != s.coverage_asked[1][1]
        assert s.coverage_asked[1][1] == personal_key(personal_profile(TEAM, CLEAN), "model-two")

    def test_a_text_that_can_never_be_scored_is_counted_against_no_key(self, web):
        client, s = web
        s.member, s.coverage = state(profile=True, scores=True, summary="too short"), {"open": 8, "scored": 1}
        self.ask(client)
        assert s.coverage_asked == [("me@und.edu", None)]

    def test_coverage_that_cannot_be_read_does_not_fail_the_profile(self, web, caplog):
        client, s = web
        s.member, s.coverage_error = state(profile=True, scores=True), RuntimeError("down")
        with caplog.at_level(logging.WARNING, logger="grantwatch.web"):
            me = self.ask(client)["me"]
        assert me["coverage"] == {"open": 8, "scored": 0} and "Could not count what is scored" in caplog.text

    def test_a_status_that_cannot_be_read_is_null_and_open_is_zero(self, web, monkeypatch):
        client, s = web
        monkeypatch.setattr(webapp, "data_status", lambda min_score=None, scores_for=None: (_ for _ in ()).throw(RuntimeError("down")))
        body = self.ask(client)
        assert body["data"] is None and body["me"]["coverage"] == {"open": 0, "scored": 0}

    @given(profile=st.booleans(), scores=st.booleans(), own_floor=st.one_of(st.none(), st.integers(0, 5)))
    def test_the_default_floor_is_theirs_else_the_teams_and_they_are_on_their_own_scores_only_with_a_profile_and_a_score(self, profile, scores, own_floor):
        me = Recipient(id=1, email="me@und.edu", min_score=own_floor, unsubscribe_token="tok")
        asked = []
        with patch.dict(os.environ, {"GRANTS_SECRET_KEY": KEY}), patch.object(webapp.store, "active_recipient", lambda email: me), patch.object(webapp, "_profile", lambda: TEAM), \
                patch.object(webapp, "fetch_member_state", lambda email: state(profile, scores)), \
                patch.object(webapp, "data_status", lambda min_score=None, scores_for=None: asked.append((min_score, scores_for)) or {"open": 1}), \
                patch.object(webapp, "member_coverage", lambda email, key: {"open": 1, "scored": 1}):
            client = TestClient(webapp.app, base_url="http://localhost")
            client.cookies.set(auth.SESSION_COOKIE, auth.sign(auth.SESSION, me.email, 3600))
            body = client.get("/api/profile").json()
        floor = own_floor if own_floor is not None else TEAM.min_score_to_notify
        assert body["me"]["min_score"] == floor and body["min_score"] == TEAM.min_score_to_notify
        assert body["me"]["using"] == ("mine" if profile and scores else "team")
        assert asked == [(floor, "me@und.edu" if profile and scores else None)]
