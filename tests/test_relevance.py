"""Tests for the OpenAI-compatible relevance scorer (no network)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import httpx2
import pytest
from hypothesis import given, strategies as st
from openai import BadRequestError

from grants.profile import Profile
from grants_data import pipeline
from grants_data.normalize import strip_html
from llm_utils import relevance
from llm_utils.relevance import (
    ELIGIBILITY_CHARS,
    SCORER_VERSION,
    LLMSettings,
    PromptTooLong,
    RelevanceScorer,
    _is_context_error,
    _record_block,
    parse_scores,
    score_key,
    score_records,
)
from llm_utils.relevance_cache import load_cached, save_cached

PROFILE = Profile(summary="Wind-energy forecasting and grid resilience.", institution="UND")
SETTINGS = LLMSettings(model="test-model", batch_size=2, workers=1, max_per_run=10)


def _nums(kwargs):
    return _numbers_in(kwargs)


class FakeCompletions:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.responder(kwargs)
        finish = "stop"
        if isinstance(content, tuple):
            content, finish = content
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)])


class FakeClient:
    def __init__(self, responder):
        self.completions = FakeCompletions(responder)
        self.chat = SimpleNamespace(completions=self.completions)


def _numbers_in(kwargs):
    user = kwargs["messages"][-1]["content"]
    return [line.split(": ", 1)[1] for line in user.splitlines() if line.startswith("Opportunity number:")]


def _echo_scores(score=4, wrap=""):
    def responder(kwargs):
        payload = {"scores": [{"opportunity_number": n, "score": score, "summary": f"Funds {n}", "reason": "fits"} for n in _numbers_in(kwargs)]}
        text = json.dumps(payload)
        return wrap.format(text) if wrap else text
    return responder


def _records(*numbers, **extra):
    return [{"OPPORTUNITY_NUMBER": n, "OPPORTUNITY_TITLE": f"Title {n}", "FUNDING_DESCRIPTION": "<p>desc</p>", **extra} for n in numbers]


class TestParseScores:
    def test_plain_json(self):
        out = parse_scores('{"scores":[{"opportunity_number":"A-1","score":5,"summary":"s","reason":"r"}]}', ["A-1"])
        assert out == {"A-1": {"score": 5, "summary": "s", "reason": "r"}}

    def test_strips_reasoning_and_fences(self):
        text = "<think>hmm\nlots</think>\nSure:\n```json\n{\"scores\":[{\"opportunity_number\":\"a-1\",\"score\":\"3.0\",\"summary\":\" a  b \",\"reason\":\"r\"}]}\n```"
        out = parse_scores(text, ["A-1"])
        assert out["A-1"]["score"] == 3
        assert out["A-1"]["summary"] == "a b"

    def test_clamps_and_ignores_unknown(self):
        text = '{"scores":[{"opportunity_number":"A-1","score":9},{"opportunity_number":"ZZZ","score":1},{"opportunity_number":"B-2","score":"bad"}]}'
        out = parse_scores(text, ["A-1", "B-2"])
        assert out == {"A-1": {"score": 5, "summary": "", "reason": ""}}

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            parse_scores("I cannot help with that.", ["A-1"])


class TestScoreRecords:
    def test_skipped_when_profile_unconfigured(self):
        client = FakeClient(_echo_scores())
        out = score_records(_records("A"), Profile(summary="REPLACE ME"), settings=SETTINGS, client=client)
        assert out.enabled is False
        assert client.completions.calls == []
        assert "RELEVANCE_SCORE" not in out.records[0]

    def test_skipped_without_model(self):
        client = FakeClient(_echo_scores())
        out = score_records(_records("A"), PROFILE, settings=LLMSettings(model=""), client=client)
        assert out.enabled is False
        assert client.completions.calls == []

    def test_scores_in_batches_and_sets_fields(self):
        client = FakeClient(_echo_scores(score=4))
        records = _records("A", "B", "C")
        out = score_records(records, PROFILE, settings=SETTINGS, client=client)
        assert out.scored == 3 and out.failed == 0
        assert len(client.completions.calls) == 2  # batch_size 2
        first = client.completions.calls[0]
        assert first["model"] == "test-model"
        assert first["response_format"]["type"] == "json_schema"
        assert "Wind-energy" in first["messages"][0]["content"]
        scored = {r["OPPORTUNITY_NUMBER"]: r for r in out.records}
        assert scored["A"]["RELEVANCE_SCORE"] == 4
        assert scored["A"]["SUMMARY"] == "Funds A"
        assert scored["A"]["RELEVANCE_PROFILE"] == score_key(PROFILE, "test-model")
        assert records[0].get("RELEVANCE_SCORE") is None  # input not mutated
        assert set(out.new_entries) == {"A", "B", "C"}

    def test_cached_entries_skip_requests(self):
        client = FakeClient(_echo_scores(score=2))
        cached = {"A": {"score": 5, "reason": "cached", "summary": "Cached A", "model": "old", "scored_at": "2026-01-01T00:00:00"}}
        out = score_records(_records("A", "B"), PROFILE, already_scored=cached, settings=SETTINGS, client=client)
        assert out.cached == 1 and out.scored == 1
        assert _numbers_in(client.completions.calls[0]) == ["B"]
        by = {r["OPPORTUNITY_NUMBER"]: r for r in out.records}
        assert by["A"]["RELEVANCE_SCORE"] == 5 and by["A"]["RELEVANCE_MODEL"] == "old"
        assert by["B"]["RELEVANCE_SCORE"] == 2

    def test_max_per_run_prefers_soonest_deadline(self):
        client = FakeClient(_echo_scores())
        records = [
            {"OPPORTUNITY_NUMBER": "LATE", "CLOSE_DATE": "12/31/2030", "FUNDING_DESCRIPTION": ""},
            {"OPPORTUNITY_NUMBER": "NONE", "CLOSE_DATE": None, "FUNDING_DESCRIPTION": ""},
            {"OPPORTUNITY_NUMBER": "SOON", "CLOSE_DATE": "01/15/2027", "FUNDING_DESCRIPTION": ""},
        ]
        out = score_records(records, PROFILE, settings=LLMSettings(model="m", batch_size=5, workers=1, max_per_run=1), client=client)
        assert out.deferred == 2 and out.scored == 1
        assert _numbers_in(client.completions.calls[0]) == ["SOON"]

    def test_unparseable_output_retries_with_looser_format(self):
        def responder(kwargs):
            fmt = (kwargs.get("response_format") or {}).get("type")
            return "not json at all" if fmt == "json_schema" else _echo_scores()(kwargs)

        client = FakeClient(responder)
        out = score_records(_records("A", "B"), PROFILE, settings=SETTINGS, client=client)
        assert out.scored == 2 and out.failed == 0
        formats = [(c.get("response_format") or {}).get("type") for c in client.completions.calls]
        assert formats == ["json_schema", "json_object"]

    def test_persistently_unparseable_batch_is_skipped_not_fatal(self):
        def responder(kwargs):
            return "garbage" if "A" in _nums(kwargs) else _echo_scores()(kwargs)

        client = FakeClient(responder)
        out = score_records(_records("A", "B", "C"), PROFILE, settings=SETTINGS, client=client)
        assert out.scored == 1 and out.failed == 2
        assert len(client.completions.calls) == 4  # three formats for [A, B], one for [C]

    def test_token_limit_stops_scoring_instead_of_changing_format(self):
        # A reasoning model that thinks past max_tokens returns empty content.
        client = FakeClient(lambda kwargs: ("", "length"))
        out = score_records(_records("A", "B", "C", "D"), PROFILE, settings=SETTINGS, client=client)
        assert out.scored == 0 and out.failed == 4
        assert len(client.completions.calls) == 1  # no format fallback, no further batches
        assert client.completions.calls[0]["response_format"]["type"] == "json_schema"

    def test_extra_body_is_sent(self, monkeypatch):
        monkeypatch.setenv("GRANTS_LLM_MODEL", "m")
        monkeypatch.setenv("GRANTS_LLM_EXTRA_BODY", '{"chat_template_kwargs": {"enable_thinking": false}}')
        settings = LLMSettings.from_env()
        assert settings.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
        client = FakeClient(_echo_scores())
        score_records(_records("A"), PROFILE, settings=settings, client=client)
        assert client.completions.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
        monkeypatch.setenv("GRANTS_LLM_EXTRA_BODY", "not json")
        assert LLMSettings.from_env().extra_body == {}

    def test_completion_tokens_leave_room_for_reasoning(self):
        assert LLMSettings(model="m", batch_size=3).completion_tokens(3) == 2048
        assert LLMSettings(model="m").completion_tokens(10) == 4000
        assert LLMSettings(model="m", max_tokens=777).completion_tokens(10) == 777

    def test_falls_back_when_json_schema_rejected(self, monkeypatch):
        class FakeBadRequest(Exception):
            message = "response_format not supported"

        monkeypatch.setattr(relevance, "BadRequestError", FakeBadRequest)

        def responder(kwargs):
            fmt = kwargs.get("response_format") or {}
            if fmt.get("type") == "json_schema":
                raise FakeBadRequest()
            return _echo_scores()(kwargs)

        client = FakeClient(responder)
        out = score_records(_records("A", "B"), PROFILE, settings=SETTINGS, client=client)
        assert out.scored == 2
        formats = [c.get("response_format", {}).get("type") for c in client.completions.calls]
        assert formats == ["json_schema", "json_object"]


class TestCache:
    def test_roundtrip_and_profile_mismatch(self, tmp_path):
        path = tmp_path / "cache.json"
        save_cached("fp1", {"A": {"score": 4}}, path)
        save_cached("fp1", {"B": {"score": 1}}, path)
        assert set(load_cached("fp1", path)) == {"A", "B"}
        assert load_cached("other", path) == {}
        save_cached("fp2", {"C": {"score": 2}}, path)
        assert set(load_cached("fp2", path)) == {"C"}


ADOPTED_PROMPT = """Judge fit from the substance of the description, not from keyword overlap.
Eligibility: give 0 only when the text clearly excludes a US public university
(for example only states, tribes, small businesses, individuals, named
institutions, or foreign entities may apply). Eligibility lists are often
bulleted; read every item. If any item covers institutions of higher
education, the group is eligible. If eligibility is unclear, judge on fit.

Broad programs: agency-wide research announcements, early-career awards
(CAREER, young investigator, young faculty), EPSCoR, instrumentation and
"any field of science" calls are open to almost everyone. Give them 2 or 3
unless the text itself names one of the group's research topics; 4 if it
names one as a listed interest; 5 only for programs whose stated purpose is
one of the group's research lines."""


class TestPrompt:
    def test_has_the_adopted_eligibility_and_calibration_text(self):
        assert ADOPTED_PROMPT in relevance._SYSTEM_PROMPT

    def test_a_wrong_eligibility_is_no_longer_an_automatic_zero(self):
        assert "wrong eligibility" not in relevance._SYSTEM_PROMPT
        assert "give 0 only when the text clearly excludes" in relevance._SYSTEM_PROMPT

    def test_reaches_the_model_ahead_of_the_profile(self):
        system = relevance.RelevanceScorer(PROFILE, SETTINGS)._system_prompt()
        assert ADOPTED_PROMPT in system
        assert system.index(ADOPTED_PROMPT) < system.index("Research profile:")


def _eligibility_line(text):
    block = _record_block({"OPPORTUNITY_NUMBER": "A", "ADDITIONAL_INFORMATION_ON_ELIGIBILITY": text, "FUNDING_DESCRIPTION": "d"}, 3000)
    lines = [line for line in block.splitlines() if line.startswith("Eligibility: ")]
    return lines[0] if lines else None


def _bulleted_eligibility(bullet_at):
    """A bulleted list whose last item, the universities, starts near ``bullet_at``."""
    filler = "<li>For-profit organizations other than small businesses</li>"
    text = ""
    while len(strip_html(text)) < bullet_at:
        text += filler
    return text + "<li>Institutions of Higher Education</li>"


class TestEligibilityText:
    def test_limit_is_2000_characters(self):
        assert ELIGIBILITY_CHARS == 2000

    def test_a_bullet_past_the_old_700_limit_reaches_the_model(self):
        # PESOSE's universities bullet starts at character 822; the latest in the Oct 30 data is at 1,881.
        for start in (822, 1881):
            line = _eligibility_line(_bulleted_eligibility(start))
            assert "Institutions of Higher Education" in line
            assert "[truncated]" not in line

    def test_text_past_the_limit_is_cut_and_marked(self):
        text = _bulleted_eligibility(2100)
        line = _eligibility_line(text)
        assert "Institutions of Higher Education" not in line
        assert line.endswith(" [truncated]")
        assert line.startswith("Eligibility: " + strip_html(text)[:100])

    def test_a_grant_without_eligibility_text_has_no_line(self):
        assert _eligibility_line(None) is None
        assert _eligibility_line("   ") is None

    @given(st.text(alphabet="abcdefg hij.,;-", min_size=1, max_size=40), st.integers(0, 4500))
    def test_sent_eligibility_is_a_bounded_prefix_of_the_text(self, unit, length):
        text = (unit * length)[:length]  # lengths spread evenly across the limit
        plain = strip_html(text)
        line = _eligibility_line(text)
        if not plain:
            assert line is None
            return
        sent = line[len("Eligibility: "):]
        if len(plain) <= ELIGIBILITY_CHARS:
            assert sent == plain
        else:
            assert sent.endswith(" [truncated]")
            kept = sent[: -len(" [truncated]")]
            assert len(kept) <= ELIGIBILITY_CHARS and plain.startswith(kept)
            assert plain[len(kept):ELIGIBILITY_CHARS].strip() == ""  # only whitespace is lost before the cut


class TestScoreKey:
    def test_names_the_profile_version_and_model(self):
        key = score_key(PROFILE, "qwen3.6")
        assert key.startswith(PROFILE.fingerprint()) and f":v{SCORER_VERSION}:" in key and key.endswith("qwen3.6")

    def test_bumping_the_scorer_version_changes_the_key(self):
        before = score_key(PROFILE, "m")
        with mock.patch.object(relevance, "SCORER_VERSION", SCORER_VERSION + 1):
            assert score_key(PROFILE, "m") != before

    def test_a_new_model_or_profile_changes_the_key(self):
        assert score_key(PROFILE, "m1") != score_key(PROFILE, "m2")
        assert score_key(PROFILE, "m") != score_key(Profile(summary="Something else."), "m")

    def test_keywords_and_thresholds_do_not_change_it(self):
        other = Profile(summary=PROFILE.summary, institution=PROFILE.institution, keywords=["x"], keyword_threshold=3, min_score_to_notify=2)
        assert score_key(other, "m") == score_key(PROFILE, "m")

    @given(
        st.tuples(st.sampled_from(["A", "B"]), st.integers(1, 3), st.text("mq:.-3", max_size=5)),
        st.tuples(st.sampled_from(["A", "B"]), st.integers(1, 3), st.text("mq:.-3", max_size=5)),
    )
    def test_the_key_is_equal_exactly_when_profile_version_and_model_are_(self, one, two):
        def key(parts):
            summary, version, model = parts
            with mock.patch.object(relevance, "SCORER_VERSION", version):
                return score_key(Profile(summary=summary), model)

        assert (key(one) == key(two)) == (one == two)


class TestPipelineKey:
    """One key everywhere: the stored column, the cache file, the lookup and the records."""

    @pytest.fixture
    def wired(self, monkeypatch):
        store = {"db": {}, "file": {}}
        seen = {"load": [], "fetch": [], "save": []}
        client = FakeClient(_echo_scores(score=4))

        def load(key):
            seen["load"].append(key)
            return dict(store["file"].get(key, {}))

        def fetch(key):
            seen["fetch"].append(key)
            return dict(store["db"].get(key, {}))

        def save(key, entries):
            seen["save"].append(key)
            store["file"].setdefault(key, {}).update(entries)

        monkeypatch.setenv("GRANTS_LLM_MODEL", "model-one")
        monkeypatch.setenv("GRANTS_LLM_BATCH_SIZE", "5")
        monkeypatch.setattr(pipeline, "load_cached", load)
        monkeypatch.setattr(pipeline, "fetch_relevance", fetch)
        monkeypatch.setattr(pipeline, "save_cached", save)
        real = pipeline.score_records
        monkeypatch.setattr(
            pipeline, "score_records",
            lambda records, profile, already_scored, settings: real(records, profile, already_scored=already_scored, settings=settings, client=client),
        )
        pipeline.onlyTheGoodStuff.last_stats = {}
        return SimpleNamespace(store=store, seen=seen, client=client)

    def test_the_lookup_the_records_and_the_save_use_the_same_key(self, wired):
        out = pipeline._score_relevance(_records("A", "B"), PROFILE)
        key = score_key(PROFILE, "model-one")
        assert wired.seen == {"load": [key], "fetch": [key], "save": [key]}
        assert {r["RELEVANCE_PROFILE"] for r in out} == {key}

    def test_the_second_run_scores_nothing_new(self, wired):
        pipeline._score_relevance(_records("A", "B"), PROFILE)
        assert len(wired.client.completions.calls) == 1
        out = pipeline._score_relevance(_records("A", "B"), PROFILE)
        assert len(wired.client.completions.calls) == 1
        assert pipeline.onlyTheGoodStuff.last_stats["cached"] == 2
        assert [r["RELEVANCE_SCORE"] for r in out] == [4, 4]

    def test_stored_database_scores_are_reused_under_the_same_key(self, wired):
        key = score_key(PROFILE, "model-one")
        wired.store["db"][key] = {"A": {"score": 5, "reason": "r", "summary": "s", "model": "model-one", "scored_at": None}}
        pipeline._score_relevance(_records("A", "B"), PROFILE)
        assert _numbers_in(wired.client.completions.calls[0]) == ["B"]

    def test_scores_stored_under_the_old_bare_fingerprint_are_rescored(self, wired):
        # What the database holds from before the key carried a version and a model.
        wired.store["db"][PROFILE.fingerprint()] = {"A": {"score": 5, "reason": "r", "summary": "s", "model": "model-one", "scored_at": None}}
        pipeline._score_relevance(_records("A"), PROFILE)
        assert _numbers_in(wired.client.completions.calls[0]) == ["A"]

    def test_a_new_model_rescores_once_then_caches_again(self, wired, monkeypatch):
        pipeline._score_relevance(_records("A"), PROFILE)
        monkeypatch.setenv("GRANTS_LLM_MODEL", "model-two")
        pipeline._score_relevance(_records("A"), PROFILE)
        assert len(wired.client.completions.calls) == 2
        pipeline._score_relevance(_records("A"), PROFILE)
        assert len(wired.client.completions.calls) == 2
        assert set(wired.store["file"]) == {score_key(PROFILE, "model-one"), score_key(PROFILE, "model-two")}

    def test_a_bumped_scorer_version_rescores_once(self, wired):
        pipeline._score_relevance(_records("A"), PROFILE)
        with mock.patch.object(relevance, "SCORER_VERSION", SCORER_VERSION + 1):
            pipeline._score_relevance(_records("A"), PROFILE)
            assert len(wired.client.completions.calls) == 2
            pipeline._score_relevance(_records("A"), PROFILE)
            assert len(wired.client.completions.calls) == 2


def _bad_request(message, **body):
    """A 400 as the openai SDK raises it, with the error body the server sent."""
    request = httpx2.Request("POST", "http://models.test/v1/chat/completions")
    return BadRequestError(message, response=httpx2.Response(400, request=request), body={"message": message, **body})


# The wording of a prompt that does not fit, as each kind of server reports it.
CONTEXT_ERRORS = [
    pytest.param("This model's maximum context length is 8192 tokens. However, your messages resulted in 9100 tokens.",
                 {"code": "context_length_exceeded", "type": "invalid_request_error"}, id="openai"),
    pytest.param("This model's maximum context length is 4096 tokens. However, you requested 5000 tokens (4000 in the messages).",
                 {"code": 400, "type": "BadRequestError"}, id="vllm"),
    pytest.param("the request exceeds the available context size, try increasing it", {"type": "exceed_context_size_error"}, id="llama.cpp"),
    pytest.param("prompt too long; exceeded max context length by 120 tokens", {}, id="ollama"),
]


class TestContextLengthError:
    @pytest.mark.parametrize("message, body", CONTEXT_ERRORS)
    def test_is_recognised(self, message, body):
        assert _is_context_error(_bad_request(message, **body))

    @pytest.mark.parametrize("message", [
        "response_format of type json_schema is not supported with this model",
        "'response_format.type' must be 'json_object' or 'text'",
        "Invalid request: unknown field extra_body",
    ])
    def test_a_rejected_format_is_not_one(self, message):
        assert not _is_context_error(_bad_request(message, code="invalid_request"))

    def test_an_exception_with_only_a_message_is_still_checked(self):
        class Bare(Exception):
            message = "maximum context length is 2048 tokens"

        assert _is_context_error(Bare())

    @pytest.mark.parametrize("message, body", CONTEXT_ERRORS)
    def test_does_not_advance_the_response_format_or_stop_the_other_batches(self, message, body):
        def responder(kwargs):
            if "A" in _numbers_in(kwargs):
                raise _bad_request(message, **body)
            return _echo_scores()(kwargs)

        client = FakeClient(responder)
        out = score_records(_records("A", "B", "C", "D"), PROFILE, settings=SETTINGS, client=client)
        formats = [(c.get("response_format") or {}).get("type") for c in client.completions.calls]
        assert formats == ["json_schema", "json_schema"]  # one request per batch, none retried in a looser format
        assert out.scored == 2 and out.failed == 2 and out.abort_reason is None
        assert {r["OPPORTUNITY_NUMBER"] for r in out.records if r.get("RELEVANCE_SCORE") is not None} == {"C", "D"}

    def test_a_rejected_format_still_falls_back(self):
        def responder(kwargs):
            if (kwargs.get("response_format") or {}).get("type") == "json_schema":
                raise _bad_request("response_format of type json_schema is not supported with this model")
            return _echo_scores()(kwargs)

        client = FakeClient(responder)
        out = score_records(_records("A", "B"), PROFILE, settings=SETTINGS, client=client)
        assert out.scored == 2
        assert [(c.get("response_format") or {}).get("type") for c in client.completions.calls] == ["json_schema", "json_object"]

    def test_the_message_names_the_fixes(self):
        scorer = RelevanceScorer(PROFILE, SETTINGS, client=FakeClient(lambda kwargs: (_ for _ in ()).throw(_bad_request("maximum context length is 2048 tokens"))))
        with pytest.raises(PromptTooLong) as raised:
            scorer.score_batch(_records("A"))
        assert "12k tokens" in str(raised.value) and all(name in str(raised.value) for name in ("GRANTS_LLM_MAX_CHARS", "GRANTS_LLM_BATCH_SIZE", "GRANTS_LLM_MAX_TOKENS"))

    @given(
        st.sampled_from([0, 1, 2]),
        st.sampled_from(["context length", "context_length_exceeded", "context size", "context window", "prompt is too long", "too many tokens"]),
        st.text(max_size=40),
        st.text(max_size=40),
    )
    def test_never_moves_the_response_format_from_any_position(self, start, phrase, before, after):
        client = FakeClient(lambda kwargs: (_ for _ in ()).throw(_bad_request(f"{before}{phrase}{after}")))
        scorer = RelevanceScorer(PROFILE, SETTINGS, client=client)
        scorer._format_index = start
        with pytest.raises(PromptTooLong):
            scorer.score_batch(_records("A"))
        assert scorer._current_format() == start
        assert len(client.completions.calls) == 1
