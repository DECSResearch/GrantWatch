"""Tests for the OpenAI-compatible relevance scorer (no network)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from grants.profile import Profile
from llm_utils import relevance
from llm_utils.relevance import LLMSettings, parse_scores, score_records
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
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


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
        assert scored["A"]["RELEVANCE_PROFILE"] == PROFILE.fingerprint()
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
