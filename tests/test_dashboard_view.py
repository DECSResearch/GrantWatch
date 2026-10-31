"""What the dashboard shows to a visitor and to a member, decided by the page's pure "view rules" (#54).

The page's script keeps the rules that follow from who is looking in one block between ``BEGIN view rules`` and
``END view rules``. It reads neither the page nor its state, so these tests cut the block out and run it under
node, with no browser: ``viewOf`` (the session and profile answers in, the decisions out), the HTML of a row, the
query a list asks for. Each test batches its cases into one node run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, List

import pytest
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web import app as webapp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

SCRIPT = webapp.INDEX_HTML.split("<script>")[1].split("</script>")[0]
RULES = re.search(r"// BEGIN view rules\n(.*?)// END view rules", SCRIPT, re.S).group(1)
# Dates and numbers are written in the viewer's locale; the tests read them in one.
ENV = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8", "TZ": "UTC"}

VISITOR = {"auth_configured": True, "email": None}
MEMBER = {"auth_configured": True, "email": "ana@und.edu"}
NO_SIGN_IN = {"auth_configured": False, "email": None}
DATA = {"open": 1371, "scored": 1371, "watched": 2, "fit_count": 37, "data_as_of": "2026-10-31T06:00:00"}
LIST_ON = {"configured": True, "data": {"open": 1371, "data_as_of": "2026-10-31T06:00:00"}}
TEAM = {"configured": True, "summary": "Cyber-physical security research", "min_score": 4, "horizons": [7, 30, 60, 90], "data": DATA}
# What only the team's side holds. None of it may reach anything a visitor is shown.
SECRETS = ["ZSUMMARY", "ZREASON", "ZKEYWORD", "ZMODEL", "ZNOTE"]


def call(*calls: List[Any]) -> List[Any]:
    """Each ``[name, *args]`` run as ``name(*args)`` in the view rules, all in one node process; the results as JSON."""
    program = (
        RULES
        + f"\nconst calls = {json.dumps(list(calls))};"
        + "\nconsole.log(JSON.stringify(calls.map(([name, ...args]) => { const r = eval(name)(...args); return r instanceof URLSearchParams ? r.toString() : r; })));"
    )
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, env=ENV)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def view(session, profile):
    return call(["viewOf", session, profile])[0]


def row(**fields):
    base = {"idx": 0, "opp_id": "G-1", "title": "Instrumentation Program", "agency": "Air Force Office of Scientific Research", "url": "https://x/1",
            "close_date": "2026-11-06", "days_left": 7, "firm_deadline": True, "forecasted": False, "summary": "Funds lab equipment.", "also_posted": []}
    base.update(fields)
    return base


class TestVisitorsPage:
    def test_the_status_line_is_the_number_of_open_grants_and_the_date(self):
        assert view(VISITOR, LIST_ON)["status"] == "Open Grants.gov opportunities as of Oct 31, 2026: 1,371."

    def test_one_open_grant_is_still_counted_in_the_same_sentence(self):
        got = view(VISITOR, {"configured": True, "data": {"open": 1, "data_as_of": "2026-10-31T06:00:00"}})["status"]
        assert got == "Open Grants.gov opportunities as of Oct 31, 2026: 1."

    def test_before_any_run_there_is_no_date_to_give(self):
        assert view(VISITOR, {"configured": True, "data": {"open": 0, "data_as_of": None}})["status"] == "No pipeline run recorded yet."

    def test_an_unreadable_database_is_said_so(self):
        assert view(VISITOR, {"configured": True, "data": None})["status"] == "Database unavailable."
        assert view(VISITOR, {"configured": False})["status"] == "Database unavailable."  # the profile request itself failed

    def test_the_account_line_and_the_help_for_a_link_that_does_not_arrive(self):
        got = view(VISITOR, LIST_ON)
        assert got["accountText"] == "A public list of open Grants.gov grants. Team members sign in for fit scores, tracking and email."
        assert got["signInHelp"] == "Link not arriving? Ask the GrantWatch admin for one."
        assert got["signIn"] is True

    def test_the_strip_and_the_empty_list_say_what_a_visitor_can_do(self):
        got = view(VISITOR, LIST_ON)
        assert got["stripHint"] == "Each bar is a grant's deadline. Hollow bars are estimated dates."
        assert got["emptyText"] == "Nothing matches. Widen the closing window or clear the search."

    def test_with_no_model_there_is_no_list_and_only_the_sign_in_prompt(self):
        got = view(VISITOR, {"configured": False, "data": {"open": 0, "data_as_of": None}})
        assert got["list"] is False and got["status"] == ""
        assert got["accountText"].startswith("A public list") and got["signInHelp"] and got["signIn"] is True
        assert got["notices"] == [] and got["profileLine"] == ""  # not even the scoring-off note

    def test_where_sign_in_is_off_a_visitor_is_not_told_to_sign_in(self):
        got = view(NO_SIGN_IN, LIST_ON)
        assert got["signIn"] is False and got["accountText"] == "" and got["signInHelp"] == ""
        assert got["list"] is True and got["status"].startswith("Open Grants.gov opportunities as of")

    def test_the_scoring_note_the_profile_line_and_the_score_bar_are_a_members(self):
        # The team profile is unset and nothing is scored: a member is told scoring is off, a visitor is not.
        off = {"configured": False, "min_score": 4, "summary": "", "data": {**DATA, "scored": 0, "fit_count": None}}
        assert view(MEMBER, off)["notices"] and view(VISITOR, off)["notices"] == []
        assert view(MEMBER, TEAM)["profileLine"] == "Scored for: Cyber-physical security research." and view(VISITOR, TEAM)["profileLine"] == ""
        assert view(MEMBER, TEAM)["floor"] == 4 and view(VISITOR, TEAM)["floor"] == 0

    @settings(max_examples=40, deadline=None)  # one node run per example
    @given(
        session=st.fixed_dictionaries({"auth_configured": st.booleans()}, optional={"email": st.sampled_from([None, ""])}),
        configured=st.booleans(),
        scored=st.integers(0, 2000),
    )
    def test_nothing_of_the_team_reaches_the_view_of_anyone_not_signed_in(self, session, configured, scored):
        # Whatever the profile answer holds (a server would not send it, but the page must not depend on that),
        # a viewer with no email gets none of it.
        profile = {
            "configured": configured, "summary": SECRETS[0], "institution": SECRETS[1], "min_score": 3, "horizons": [7, 30],
            "data": {"open": 5, "scored": scored, "watched": 9, "fit_count": 4, "data_as_of": "2026-10-31T06:00:00"},
            "me": {"has_profile": True, "updated_at": "2026-10-30T00:00:00", "min_score": 2, "using": "mine", "coverage": {"open": 5, "scored": 5}, "note": SECRETS[2]},
        }
        got = view(session, profile)
        text = json.dumps(got)
        assert got["member"] is False and got["floor"] == 0 and got["notices"] == [] and got["profileLine"] == ""
        assert not any(secret in text for secret in SECRETS)
        assert "tracked" not in got["status"] and "scored" not in got["status"]
        assert got["list"] is (configured is True)

    def test_a_list_asks_for_no_score_bar_and_no_tracked_only(self):
        values = {"q": " wind ", "minScore": "4", "closingWithin": "30", "stage": "full", "forecasted": False, "watchedOnly": True}
        visitor, member = call(["paramsFor", values, {"member": False}], ["paramsFor", values, {"member": True}])
        assert visitor == "q=wind&closing_within=30&stage=full&include_forecasted=false&limit=500"
        assert member == "q=wind&min_score=4&closing_within=30&stage=full&include_forecasted=false&watched_only=true&limit=500"

    @settings(max_examples=30, deadline=None)
    @given(q=st.text(max_size=12), minimum=st.sampled_from(["", "0", "3", "5"]), within=st.sampled_from(["", "7", "180"]),
           stage=st.sampled_from(["", "concept", "full"]), forecasted=st.booleans(), watched=st.booleans())
    def test_a_visitors_query_only_ever_names_the_four_filters_they_have(self, q, minimum, within, stage, forecasted, watched):
        values = {"q": q, "minScore": minimum, "closingWithin": within, "stage": stage, "forecasted": forecasted, "watchedOnly": watched}
        query = call(["paramsFor", values, {"member": False}])[0]
        names = {pair.split("=")[0] for pair in query.split("&")}
        assert names <= {"q", "closing_within", "stage", "include_forecasted", "limit"}


class TestMembersPage:
    def test_a_member_gets_the_page_as_it_was_with_their_name_in_the_account_line(self):
        got = view(MEMBER, TEAM)
        assert got["member"] is True and got["list"] is True and got["signIn"] is True
        assert got["accountText"] == "Signed in as ana@und.edu." and got["signInHelp"] == ""  # no sign-in prompt for someone signed in
        assert got["status"] == "Data as of Oct 31, 2026. 1371 open grants, 1371 scored, 2 tracked. 37 score 4 or higher."
        assert got["stripHint"] == "Each bar is a grant; taller means a better fit. Hollow bars are estimated dates."
        assert got["emptyText"] == "Nothing matches. Widen the closing window, lower the relevance bar, or clear the search."
        assert got["profileLine"] == "Scored for: Cyber-physical security research." and got["notices"] == []

    def test_a_member_is_told_when_scoring_is_off_and_still_gets_the_list(self):
        off = {"configured": False, "min_score": 4, "summary": "", "data": {**DATA, "scored": 0, "fit_count": None}}
        got = view(MEMBER, off)
        assert got["list"] is True and got["floor"] == 0 and got["profileLine"] == ""
        assert len(got["notices"]) == 1 and got["notices"][0].startswith("Relevance scoring is off.") and "config/research_profile.yml" in got["notices"][0]
        assert got["status"] == "Data as of Oct 31, 2026. 1371 open grants, 0 scored, 2 tracked."  # no fit count without scores

    def test_there_is_no_member_without_an_email_in_the_session(self):
        assert [view(session, TEAM)["member"] for session in (VISITOR, NO_SIGN_IN, {"auth_configured": True, "email": ""}, None)] == [False] * 4


class TestRows:
    HOSTILE = {
        "relevance_score": 5, "relevance_reason": "ZREASON", "relevance_model": "ZMODEL", "matched_keywords": "ZKEYWORD", "watch_note": "ZNOTE",
        "watched": True, "my_verdict": 1, "votes_up": 3, "votes_down": 1, "team_verdict": 1, "hidden": True, "my_vote_opp_id": "G-1",
    }

    def test_a_visitors_row_has_two_columns_and_nothing_to_press(self):
        html = call(["rowHtml", row(), {"member": False}])[0]
        assert 'class="days' in html and 'class="main"' in html and "Funds lab equipment." in html
        for absent in ('class="score"', 'class="actions"', "data-track", "data-vote", 'class="squares"', "of 5", "unscored"):
            assert absent not in html

    def test_a_row_that_holds_the_teams_marks_still_draws_none_of_them_for_a_visitor(self):
        html = call(["rowHtml", row(**self.HOSTILE), {"member": False}])[0]
        assert not any(secret in html for secret in SECRETS) and "Tracked" not in html and "Relevant" not in html and "Not for us" not in html
        assert 'class="why summary"' in html  # the description, as the visitor's row has it

    @settings(max_examples=40, deadline=None)
    @given(extra=st.fixed_dictionaries({}, optional={key: st.sampled_from(["ZREASON", "ZKEYWORD", "ZNOTE", 5, 0, True, None]) for key in HOSTILE}))
    def test_whatever_internal_fields_a_row_carries_a_visitors_html_is_the_same(self, extra):
        plain, loaded = call(["rowHtml", row(), {"member": False}], ["rowHtml", row(**extra), {"member": False}])
        assert plain == loaded

    def test_a_members_row_has_the_reason_the_score_and_the_buttons(self):
        html = call(["rowHtml", row(**self.HOSTILE), {"member": True}])[0]
        assert 'class="why">ZREASON</p>' in html and "5 of 5" in html and 'data-track="G-1"' in html and html.count("data-vote=") == 2

    def test_a_visitors_details_leave_out_the_keywords_and_the_scorer(self):
        g = {"opp_id": "G-1", "opportunity_status": "Posted", "post_date": "2026-08-01", "close_date": "2026-11-06", "matched_keywords": "ZKEYWORD", "relevance_model": "ZMODEL"}
        visitor, member = call(["detailRows", g, {"member": False}], ["detailRows", g, {"member": True}])
        assert [k for k, _ in visitor] == ["Opportunity", "Status", "Posted", "Closes"]
        assert [k for k, _ in member][-2:] == ["Matched keywords", "Scored by"]

    def test_a_visitors_bars_are_all_one_height(self):
        heights = call(*[["barHeight", row(relevance_score=s), {"member": False}] for s in (0, 3, 5, None)])
        assert heights == [12, 12, 12, 12]
        assert call(["barHeight", row(relevance_score=5), {"member": True}], ["barHeight", row(), {"member": True}]) == [62, 12]
