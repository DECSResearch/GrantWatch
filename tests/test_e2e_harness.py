"""The end-to-end suite's fixture extract, profile and fake model server (tests/e2e/harness.py).

These need no database, so they run with the rest of the suite; the flows themselves are in
tests/e2e/ and run only when GRANTS_TEST_POSTGRES_URL is set.
"""
from __future__ import annotations

import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import requests
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))

from grants.members import personal_profile  # noqa: E402
from grants.profile import Profile, load_profile  # noqa: E402
from grants.sql_utils import _DSN_ENV_VARS  # noqa: E402
from grants_data.parse_extract import process_extract_xml  # noqa: E402
from harness import CLEARED_ENV, MEMBER_TAG, MODEL, NOT_SENT, PROFILE, SCORES, FakeLLM, closed_port, render_extract, suite_environment, synopsis  # noqa: E402
from llm_utils.relevance import LLMSettings, score_records  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# Where the program reads its settings. The document checker and the MCP server are other programs, and not under test here.
CODE = ("main.py", "grants", "grants_data", "llm_utils", "notifications", "src", "logs", "sql_utils")
# A name the code reads, wherever it is read: through os.getenv, a helper that takes the name, or a tuple of names.
_SETTING = re.compile(r"""["']((?:GRANTS|GMAIL|OPENAI|POSTGRES)_[A-Z0-9_]+|PG[A-Z]+|DATABASE_URL[A-Z_]*)["']""")
_DIRECT = re.compile(r"""(?:getenv|environ\.get|environ)\(?\[?\s*["']([A-Z][A-Z0-9_]*)["']""")
_EXAMPLE = re.compile(r"^#? ?((?:GRANTS|GMAIL|POSTGRES)_[A-Z0-9_]+)=", re.M)
REFERENCE_DAY = date(2026, 10, 30)
# Days from today that the code treats differently: closed or open, the week, the urgent window, the overview buckets.
BOUNDARIES = (0, 7, 30, 60, 90)
# A fixed date, not one from a day mark: the placeholder close date.
PLACEHOLDER_YEAR = 2076


def parse(text: str):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "extract.xml"
        path.write_text(text, encoding="utf-8")
        return process_extract_xml(path)


def day(value) -> date:
    return datetime.strptime(value, "%m/%d/%Y").date()


def offsets(today: date, extra: str = ""):
    """Opportunity number -> (days from today to its posting, days to its close date), for the fixture rendered on ``today``."""
    return {
        record["OPPORTUNITY_NUMBER"]: ((day(record["POSTED_DATE"]) - today).days, (day(record["CLOSE_DATE"]) - today).days)
        for record in parse(render_extract(today, extra))
    }


@pytest.fixture(scope="module")
def llm():
    server = FakeLLM()
    try:
        yield server
    finally:
        server.stop()


def settings_for(server: FakeLLM, batch_size: int = 3, workers: int = 1) -> LLMSettings:
    return LLMSettings(base_url=server.base_url, model=MODEL, api_key="not-a-real-key", batch_size=batch_size, workers=workers, timeout=5.0)


def records_for(numbers):
    return [{"OPPORTUNITY_NUMBER": n, "OPPORTUNITY_TITLE": f"Title of {n}", "FUNDING_DESCRIPTION": f"What {n} funds."} for n in numbers]


class TestFixtureExtract:
    @settings(max_examples=100, deadline=None)
    @given(today=st.dates(min_value=date(2000, 1, 1), max_value=date(2065, 12, 31)))
    def test_it_reads_the_same_on_any_day(self, today):
        """Leap days, month ends and year ends included: every offset is the one it has on the reference day."""
        found, reference = offsets(today), offsets(REFERENCE_DAY)
        assert found.keys() == reference.keys()
        for number, (posted, close) in found.items():
            if number == "E2E-PLACEHOLDER":
                assert posted == reference[number][0] and today + timedelta(days=close) == date(PLACEHOLDER_YEAR, 12, 31)
            else:
                assert (posted, close) == reference[number], number

    def test_no_close_date_sits_near_a_boundary_the_code_treats_differently(self):
        """So a run that straddles midnight, or a database in another time zone, reads the same."""
        for number, (_, close) in offsets(REFERENCE_DAY).items():
            if number != "E2E-PLACEHOLDER":
                assert min(abs(close - boundary) for boundary in BOUNDARIES) >= 2, f"{number} closes {close} days out"

    def test_extra_grants_are_rendered_with_the_same_day_marks(self):
        grant = synopsis("X-1", "Extra", oid=1, close=50, posted=-5)
        assert offsets(REFERENCE_DAY, grant)["X-1"] == (-5, 50)

    def test_every_grant_the_model_is_asked_about_has_a_scripted_score(self):
        records = parse(render_extract(REFERENCE_DAY))
        profile = load_profile(PROFILE)
        asked = {r["OPPORTUNITY_NUMBER"] for r in records} - set(NOT_SENT)
        assert asked == set(SCORES)
        assert {r["OPPORTUNITY_NUMBER"] for r in records if profile.excludes_applicants(r.get("ELIGIBLE_APPLICANT_CODES"))} == {"E2E-STATES"}

    def test_the_profile_is_configured_for_a_public_university(self):
        profile = load_profile(PROFILE)
        assert profile.is_configured and profile.applicant_codes == ["06"] and profile.min_score_to_notify == 4

    def test_no_identifier_in_it_looks_like_a_placeholder(self):
        text = render_extract(REFERENCE_DAY)
        for record in parse(text):
            for value in (record["OPPORTUNITY_ID"], record["OPPORTUNITY_NUMBER"], record["OPPORTUNITY_URL"]):
                assert "2076" not in value and "999999999" not in value


class TestFakeLLM:
    @settings(max_examples=30, deadline=None)
    @given(
        numbers=st.lists(st.sampled_from(sorted(SCORES)), min_size=1, max_size=len(SCORES), unique=True),
        batch_size=st.integers(1, 4),
        workers=st.integers(1, 2),
    )
    def test_the_scorer_gets_the_scripted_score_for_every_number_whatever_the_batches(self, llm, numbers, batch_size, workers):
        llm.reset()
        outcome = score_records(records_for(numbers), Profile(summary="Security research."), settings=settings_for(llm, batch_size, workers))
        assert outcome.abort_reason is None and outcome.scored == len(numbers) and outcome.failed == 0
        assert {r["OPPORTUNITY_NUMBER"]: r["RELEVANCE_SCORE"] for r in outcome.records} == {n: SCORES[n] for n in numbers}
        assert sorted(llm.sent()) == sorted(numbers)  # each asked about exactly once
        assert max(len(request["numbers"]) for request in llm.requests) <= batch_size

    def test_a_number_that_is_not_scripted_gets_the_default(self, llm):
        llm.reset()
        outcome = score_records(records_for(["UNSCRIPTED"]), Profile(summary="Security research."), settings=settings_for(llm))
        assert outcome.records[0]["RELEVANCE_SCORE"] == 1 and outcome.records[0]["RELEVANCE_MODEL"] == MODEL

    def test_it_keeps_each_request_the_scorer_sent(self, llm):
        llm.reset()
        score_records(records_for(["E2E-BROAD"]), Profile(summary="Security research."), settings=settings_for(llm))
        (request,) = llm.requests
        assert request["numbers"] == ["E2E-BROAD"] and "Security research." in request["system"]
        assert request["body"]["model"] == MODEL and request["body"]["temperature"] == 0

    def test_a_truncated_answer_stops_scoring_with_the_advice_to_raise_the_token_limit(self, llm):
        llm.reset()
        llm.mode = "truncated"
        outcome = score_records(records_for(sorted(SCORES)[:4]), Profile(summary="Security research."), settings=settings_for(llm))
        assert outcome.scored == 0 and outcome.failed == 4
        assert outcome.abort_reason.startswith("the model used all 2048 completion tokens before answering")
        assert len(llm.requests) == 1  # the second batch was not sent
        llm.reset()
        assert llm.mode == "ok"

    def test_a_members_description_picks_the_score_table_and_is_kept_with_the_request(self, llm):
        llm.reset()
        llm.profile_scores = {"battery": {"E2E-BROAD": 5}}
        base = Profile(summary="The team does security research.")
        batteries, wind = (personal_profile(base, f"I study {topic} in depth, as a researcher in the field.") for topic in ("battery chemistry", "wind forecasting"))
        out = {
            label: score_records(records_for(["E2E-BROAD"]), profile, settings=settings_for(llm), personal=personal).records[0]["RELEVANCE_SCORE"]
            for label, profile, personal in (("team", base, False), ("battery", batteries, True), ("wind", wind, True))
        }
        assert out == {"team": SCORES["E2E-BROAD"], "battery": 5, "wind": SCORES["E2E-BROAD"]}
        assert [request["profile"] for request in llm.requests] == [None, "I study battery chemistry in depth, as a researcher in the field.", "I study wind forecasting in depth, as a researcher in the field."]
        llm.reset()
        assert llm.profile_scores == {}

    def test_what_the_model_writes_for_a_member_is_tagged_and_what_it_writes_for_the_team_is_not(self, llm):
        """So a test can tell whose summary and reason a viewer was shown."""
        llm.reset()
        base = Profile(summary="The team does security research.")
        mine = personal_profile(base, "I study battery chemistry in depth, as a researcher in the field.")
        team = score_records(records_for(["E2E-BROAD"]), base, settings=settings_for(llm)).records[0]
        member = score_records(records_for(["E2E-BROAD"]), mine, settings=settings_for(llm), personal=True).records[0]
        assert (team["SUMMARY"], team["RELEVANCE_REASON"]) == ("E2E-BROAD funds the group's research.", "E2E-BROAD fits the group's profile as scripted.")
        assert (member["SUMMARY"], member["RELEVANCE_REASON"]) == (team["SUMMARY"] + MEMBER_TAG, team["RELEVANCE_REASON"] + MEMBER_TAG)
        assert MEMBER_TAG.strip() and MEMBER_TAG not in team["SUMMARY"] + team["RELEVANCE_REASON"]

    @settings(max_examples=30, deadline=None)
    @given(
        plan=st.lists(
            st.tuples(
                st.sampled_from([None, "battery", "security", "wind"]),
                st.lists(st.sampled_from(sorted(SCORES)), min_size=1, max_size=4, unique=True),
            ),
            min_size=1,
            max_size=6,
        )
    )
    def test_requests_are_told_apart_by_the_profile_they_were_made_for(self, llm, plan):
        """Whatever order the team and members are asked in: each profile's requests are its own, and together they are all of them.

        "security" is in the team's summary too, so a team request must not take a member's table because of it.
        """
        llm.reset()
        llm.profile_scores = {"battery": {n: 5 for n in SCORES}, "security": {n: 5 for n in SCORES}}
        base = Profile(summary="The team does security research.")

        def description(topic):
            return None if topic is None else f"I study {topic} in depth, as a researcher in the field."

        for topic, numbers in plan:
            profile = base if topic is None else personal_profile(base, description(topic))
            records = score_records(records_for(numbers), profile, settings=settings_for(llm), personal=topic is not None).records
            table = 5 if topic in ("battery", "security") else None
            assert {r["OPPORTUNITY_NUMBER"]: r["RELEVANCE_SCORE"] for r in records} == {n: SCORES[n] if table is None else table for n in numbers}
            tag = "" if topic is None else MEMBER_TAG  # a member's answers are tagged, even one whose text matches no table
            assert {r["OPPORTUNITY_NUMBER"]: (r["SUMMARY"], r["RELEVANCE_REASON"]) for r in records} == {
                n: (f"{n} funds the group's research.{tag}", f"{n} fits the group's profile as scripted.{tag}") for n in numbers
            }
        asked = {description(topic) for topic, _ in plan}
        for text in asked:
            expected = [n for topic, numbers in plan if description(topic) == text for n in numbers]
            assert sorted(llm.sent(profile=text)) == sorted(expected)
        assert sorted(n for text in asked for n in llm.sent(profile=text)) == sorted(llm.sent())

    def test_a_number_asked_for_the_team_and_two_members_is_found_by_profile(self, llm):
        llm.reset()
        base = Profile(summary="The team does security research.")
        ana, ben = "I study battery chemistry in depth, as a researcher in the field.", "I study wind forecasting in depth, as a researcher in the field."
        for text in (None, ana, ben):
            profile = base if text is None else personal_profile(base, text)
            score_records(records_for(["E2E-BROAD"]), profile, settings=settings_for(llm), personal=text is not None)
        with pytest.raises(AssertionError, match="was in 3 requests"):
            llm.request_for("E2E-BROAD")
        assert llm.request_for("E2E-BROAD", profile=None)["profile"] is None
        assert llm.request_for("E2E-BROAD", profile=ana)["profile"] == ana and llm.request_for("E2E-BROAD", profile=ben)["profile"] == ben
        assert llm.sent() == ["E2E-BROAD"] * 3 and llm.sent(profile=None) == ["E2E-BROAD"] and llm.sent(profile="a description nobody has") == []
        with pytest.raises(AssertionError, match="was in 0 requests"):
            llm.request_for("E2E-BROAD", profile="a description nobody has")
        llm.reset()

    def test_it_lists_its_model(self, llm):
        body = requests.get(llm.base_url + "/models", timeout=5).json()
        assert [m["id"] for m in body["data"]] == [MODEL]

    def test_a_stopped_server_and_a_closed_port_refuse_connections(self):
        server = FakeLLM()
        url = server.base_url
        server.stop()
        for target in (url, f"http://127.0.0.1:{closed_port()}/v1"):
            with pytest.raises(requests.ConnectionError):
                requests.get(target + "/models", timeout=5)


def settings_read_by_the_code():
    names = set()
    for entry in CODE:
        path = ROOT / entry
        for source in [path] if path.is_file() else sorted(path.rglob("*.py")):
            text = source.read_text(encoding="utf-8")
            names.update(_SETTING.findall(text))
            names.update(_DIRECT.findall(text))
    return names


def known_settings():
    pinned = suite_environment(postgres_url="postgresql://x/y", outbox=Path("o"), llm_url="http://127.0.0.1:1/v1", admin_users="a:b")
    return set(_DSN_ENV_VARS) | set(CLEARED_ENV) | set(pinned)


class TestTheEnvironment:
    """The suite starts from an environment it sets itself: a setting the code reads must be cleared or pinned, or a developer's shell leaks in."""

    def test_every_setting_the_code_reads_is_cleared_or_pinned(self):
        assert sorted(settings_read_by_the_code() - known_settings()) == []

    def test_every_setting_in_the_example_env_file_is_cleared_or_pinned(self):
        """A developer's .env is a copy of it, and main.py and the web app load it."""
        listed = set(_EXAMPLE.findall((ROOT / ".env.example").read_text(encoding="utf-8")))
        assert "GRANTS_PERSONAL_STOP_MINUTES" in listed
        assert sorted(listed - known_settings()) == []

    def test_the_search_finds_the_settings_it_is_there_to_find(self):
        read = settings_read_by_the_code()
        assert {"GRANTS_PERSONAL_STOP_MINUTES", "GRANTS_SECRET_KEY", "GRANTS_LLM_BASE_URL", "POSTGRES_URL", "PGSSLMODE", "GMAIL_TOKEN_JSON"} <= read

    def test_the_settings_the_suite_pins_name_a_schema_a_folder_the_fake_server_and_an_admin(self):
        pinned = suite_environment(postgres_url="postgresql://x/y", outbox=Path("o"), llm_url="http://127.0.0.1:1/v1", admin_users="a:b")
        assert pinned["POSTGRES_URL"] == "postgresql://x/y" and pinned["GRANTS_LLM_BASE_URL"] == "http://127.0.0.1:1/v1"
        assert pinned["GRANTS_EMAIL_OUTBOX"] == "o" and pinned["GRANTS_ADMIN_USERS"] == "a:b" and pinned["GRANTS_LLM_MODEL"] == MODEL
