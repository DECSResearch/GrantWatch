"""What the admin page says about personal profiles, decided by the page's pure "rules" (#55).

The page's script keeps the wording it builds from the server's numbers between ``BEGIN rules`` and ``END rules``.
That block reads neither the page nor its state, so these tests cut it out and run it under node, with no
browser. Each test batches its cases into one node run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, List

import pytest
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web import app as webapp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

SCRIPT = webapp.ADMIN_HTML.split("<script>")[1].split("</script>")[0]
RULES = re.search(r"// BEGIN rules\n(.*?)// END rules", SCRIPT, re.S).group(1)
# Dates and numbers are written in the viewer's locale; the tests read them in one.
ENV = {**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8", "TZ": "UTC"}
THIS_YEAR = date.today().year


def call(*calls: List[Any]) -> List[Any]:
    """Each ``[name, *args]`` run as ``name(*args)`` in the rules, all in one node process; the results as JSON."""
    program = (
        RULES
        + f"\nconst calls = {json.dumps(list(calls))};"
        + "\nconsole.log(JSON.stringify(calls.map(([name, ...args]) => eval(name)(...args))));"
    )
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, env=ENV)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def person(**fields):
    base = {"email": "ana@und.edu", "name": None, "profile": "team", "profile_updated_at": None, "coverage": {"open": 1371, "scored": 1371}}
    base.update(fields)
    return base


class TestProfileCell:
    def test_a_person_on_the_teams_profile_is_the_team(self):
        assert call(["profileLabel", person()]) == ["Team"]

    def test_a_person_with_their_own_names_the_day_they_saved_it(self):
        own = person(profile="own", profile_updated_at=f"{THIS_YEAR}-03-05T10:15:00+00:00")
        assert call(["profileLabel", own]) == ["Own, updated Mar 5"]

    def test_a_day_in_another_year_carries_the_year(self):
        own = person(profile="own", profile_updated_at="2020-03-05T10:15:00+00:00")
        assert call(["profileLabel", own]) == ["Own, updated Mar 5, 2020"]

    def test_a_missing_day_is_left_out_not_read_as_1970(self):
        assert call(["profileLabel", person(profile="own")], ["profileLabel", person(profile="own", profile_updated_at="")]) == ["Own", "Own"]

    def test_the_cell_is_built_from_the_kind_and_the_day_alone(self):
        # Whatever else a person's row holds, the text of a profile is not in the label.
        secret = "ZPROFILE-TEXT"
        own = person(profile="own", profile_updated_at=f"{THIS_YEAR}-03-05T10:15:00+00:00", summary=secret, text=secret, profile_text=secret)
        assert secret not in call(["profileLabel", own])[0]


class TestCoverageCell:
    def test_scored_of_open(self):
        assert call(["coverageLabel", {"open": 1371, "scored": 412}], ["coverageLabel", {"open": 1371, "scored": 1371}]) == ["412 of 1,371", "1,371 of 1,371"]

    def test_nothing_open_is_zero_of_zero(self):
        assert call(["coverageLabel", {"open": 0, "scored": 0}]) == ["0 of 0"]

    @pytest.mark.parametrize("coverage", [None, {}, {"open": 5}, {"scored": 5}, {"open": None, "scored": 1}, {"open": "5", "scored": 5}, {"open": 5, "scored": True}, []])
    def test_a_count_that_could_not_be_made_is_a_dash(self, coverage):
        assert call(["coverageLabel", coverage]) == ["–"]

    @settings(max_examples=30, deadline=None)  # one node run per example
    @given(open_grants=st.integers(0, 100_000), data=st.data())
    def test_the_cell_always_reads_scored_of_open(self, open_grants, data):
        scored = data.draw(st.integers(0, open_grants))
        assert call(["coverageLabel", {"open": open_grants, "scored": scored}]) == [f"{scored:,} of {open_grants:,}"]


def run(**personal):
    """A personal step's run record: its totals in ``stats.personal``."""
    return {"step": "personal", "state": "ok", "stats": {"personal": personal}}


def line(*runs):
    return call(*(["personalLine", r] for r in runs))


class TestPersonalProfilesLine:
    TOTALS = {"people": 3, "scored": 120, "failed": 2, "pending": 40, "stopped": None}

    def test_the_totals_of_the_step(self):
        assert line(run(**self.TOTALS)) == ["Personal profiles: 3 people, 120 scored, 2 failed, 40 pending."]

    def test_one_person_and_thousands(self):
        assert line(run(people=1, scored=1_200, failed=0, pending=0, stopped=None)) == ["Personal profiles: 1 person, 1,200 scored, 0 failed, 0 pending."]

    def test_it_says_when_the_time_stop_was_reached(self):
        assert line(run(**{**self.TOTALS, "stopped": "time limit"})) == ["Personal profiles: 3 people, 120 scored, 2 failed, 40 pending. Stopped at the time limit."]

    def test_any_other_stop_is_given_as_the_step_recorded_it(self):
        outage = run(**{**self.TOTALS, "stopped": "the model is unavailable: cannot reach http://10.0.0.5:18100: Connection error."})
        early = run(**{**self.TOTALS, "stopped": "1 of 3 members stopped early: scoring failed: boom"})
        assert line(outage, early) == [
            "Personal profiles: 3 people, 120 scored, 2 failed, 40 pending. Stopped: the model is unavailable: cannot reach http://10.0.0.5:18100: Connection error.",
            "Personal profiles: 3 people, 120 scored, 2 failed, 40 pending. Stopped: 1 of 3 members stopped early: scoring failed: boom.",
        ]

    def test_a_skipped_step_says_why_and_shows_no_zeros(self):
        skipped = run(people=0, scored=0, failed=0, pending=0, stopped="skipped: the team's scoring stopped: cannot reach http://x: down")
        assert line(skipped) == ["Personal profiles: skipped, the team's scoring stopped: cannot reach http://x: down."]

    def test_nobody_with_a_profile_is_said_so(self):
        assert line(run(people=0, scored=0, failed=0, pending=0, stopped=None)) == ["Personal profiles: nobody has written one yet."]

    def test_a_run_with_no_totals_has_no_line(self):
        bare = [None, {}, {"step": "personal", "stats": None}, {"step": "personal", "stats": {}}, {"step": "personal", "stats": {"personal": None}},
                {"step": "personal", "stats": {"personal": [1, 2]}}, {"step": "personal", "stats": {"personal": "3 people"}}, {"step": "personal", "stats": [1]}]
        assert line(*bare) == [""] * len(bare)

    def test_a_number_that_is_missing_is_left_out_rather_than_made_up(self):
        assert line(run(people=2, scored=5), run(), run(people="2", scored=None, failed=True, pending="x")) == [
            "Personal profiles: 2 people, 5 scored.", "Personal profiles: no numbers recorded.", "Personal profiles: no numbers recorded.",
        ]

    def test_the_step_records_exactly_the_word_the_line_looks_for(self):
        """Run the real pass past its deadline and read what it recorded, so a renamed stop cannot silently stop showing."""
        from grants.personal import Member, score_people
        from grants.profile import Profile
        from llm_utils.relevance import LLMSettings

        member = Member("ana@und.edu", "I build battery models for grid-scale storage, mostly lithium chemistries.")
        record = {"OPPORTUNITY_NUMBER": "G-1", "CLOSE_DATE": "2026-12-01"}
        totals = score_people(
            [member], [record], Profile(summary="Cyber-physical security research"), LLMSettings.from_env(), deadline=0.0,
            clock=lambda: 1.0, scored_numbers=lambda email, key: set(), votes_of=lambda emails: [],
        )
        assert totals.stopped == "time limit" and totals.pending == 1
        assert line({"step": "personal", "stats": {"personal": totals.as_dict()}}) == ["Personal profiles: 1 person, 0 scored, 0 failed, 1 pending. Stopped at the time limit."]

    @settings(max_examples=30, deadline=None)
    @given(
        totals=st.fixed_dictionaries({k: st.integers(0, 100_000) for k in ("people", "scored", "failed", "pending")}),
        stopped=st.one_of(st.none(), st.just("time limit"), st.text(min_size=1, max_size=40).filter(lambda t: t.strip(".") and t != "time limit" and not t.startswith("skipped: "))),
    )
    def test_every_number_is_in_the_line_and_the_time_stop_is_named_only_when_it_was_reached(self, totals, stopped):
        text = line(run(**totals, stopped=stopped))[0]
        if totals["people"] == 0 and stopped is None:
            assert text == "Personal profiles: nobody has written one yet."
            return
        assert text.startswith("Personal profiles: ")
        for key in ("scored", "failed", "pending"):
            assert f"{totals[key]:,} {key}" in text
        assert ("Stopped at the time limit." in text) == (stopped == "time limit")
        assert ("Stopped: " in text) == (stopped not in (None, "time limit"))
