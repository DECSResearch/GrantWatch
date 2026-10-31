"""The end-to-end suite's fixture extract, profile and fake model server (tests/e2e/harness.py).

These need no database, so they run with the rest of the suite; the flows themselves are in
tests/e2e/ and run only when GRANTS_TEST_POSTGRES_URL is set.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import requests
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))

from grants.profile import Profile, load_profile  # noqa: E402
from grants_data.parse_extract import process_extract_xml  # noqa: E402
from harness import MODEL, NOT_SENT, PROFILE, SCORES, FakeLLM, closed_port, render_extract, synopsis  # noqa: E402
from llm_utils.relevance import LLMSettings, score_records  # noqa: E402

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
