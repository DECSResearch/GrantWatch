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
