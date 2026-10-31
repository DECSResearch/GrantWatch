"""The database load keeps a model's summary when the same grant is later scored by rule (#47).

The unit suite replaces `load_grants_from_records`, and the rule lives in the upsert's SQL, so this
needs a real Postgres. Each test starts from an empty database.
"""
from __future__ import annotations

import itertools

from hypothesis import HealthCheck, given, settings, strategies as st

from grants.data.loader import load_grants_from_records
from grants.sql_utils import ensure_schema
from harness import PROFILE
from llm_utils.relevance import RULE_MODEL

MODEL_SUMMARY = "A model-written summary of what this call funds."
TRUNCATED = "The first 320 characters of the description, cut off mid-sen..."
RULE_REASON = "Not open to public university (Grants.gov eligibility codes)"


def record(number, *, summary, score=None, model=None):
    return {
        "OPPORTUNITY_NUMBER": number,
        "OPPORTUNITY_TITLE": f"Grant {number}",
        "FUNDING_DESCRIPTION": "A description.",
        "SUMMARY": summary,
        "RELEVANCE_SCORE": score,
        "RELEVANCE_REASON": RULE_REASON if model == RULE_MODEL else ("fits" if model else None),
        "RELEVANCE_MODEL": model,
    }


def stored(world, number):
    (row,) = world.query("SELECT summary, relevance_score, relevance_model, relevance_reason FROM grants WHERE opp_id = %s", (number,))
    return row


class TestSummaryOnReload:
    def test_a_rule_score_leaves_the_models_summary_alone(self, fresh):
        ensure_schema()
        load_grants_from_records([record("G1", summary=MODEL_SUMMARY, score=4, model="fake-model")])
        load_grants_from_records([record("G1", summary=TRUNCATED, score=0, model=RULE_MODEL)])
        row = stored(fresh, "G1")
        assert row["summary"] == MODEL_SUMMARY
        # The rule still wins the score: the grant is now ruled out.
        assert (row["relevance_score"], row["relevance_model"], row["relevance_reason"]) == (0, RULE_MODEL, RULE_REASON)
        load_grants_from_records([record("G1", summary=TRUNCATED)])  # a run that could not score keeps it too
        assert stored(fresh, "G1")["summary"] == MODEL_SUMMARY
        load_grants_from_records([record("G1", summary="A newer model summary.", score=3, model="fake-model")])
        assert stored(fresh, "G1")["summary"] == "A newer model summary."  # only a model replaces it


_COUNTER = itertools.count()
# What one load of a grant can be: scored by a model, by rule, or not scored (the model was down).
_LOADS = st.lists(st.sampled_from(["model", "rule", "none"]), min_size=1, max_size=8)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(kinds=_LOADS)
def test_the_summary_is_the_last_models_else_the_first_one_stored(fresh, kinds):
    """Whatever order the loads come in, only a model's summary replaces a stored one."""
    ensure_schema()
    number = f"P{next(_COUNTER)}"  # a grant of its own per example, so nothing needs emptying in between
    expected = None
    for index, kind in enumerate(kinds):
        text = f"{kind} summary {index}"
        score, model = {"model": (4, "fake-model"), "rule": (0, RULE_MODEL), "none": (None, None)}[kind]
        load_grants_from_records([record(number, summary=text, score=score, model=model)])
        if kind == "model" or expected is None:
            expected = text
    assert stored(fresh, number)["summary"] == expected


class TestIngestAfterTheCodesAreSet:
    def test_a_grant_the_precheck_now_rules_out_keeps_its_model_summary(self, fresh, monkeypatch, tmp_path):
        """The first ingest after `applicant_codes` is set: E2E-STATES was model-scored before."""
        without_codes = tmp_path / "profile.yml"
        without_codes.write_text(
            "".join(line for line in PROFILE.read_text(encoding="utf-8").splitlines(keepends=True) if not line.startswith("applicant_codes")),
            encoding="utf-8",
        )
        monkeypatch.setenv("GRANTS_PROFILE_FILE", str(without_codes))
        assert fresh.ingest() == 0
        before = stored(fresh, "E2E-STATES")
        assert before["relevance_model"] != RULE_MODEL
        assert before["summary"] == "E2E-STATES funds the group's research."  # what the fake model wrote

        monkeypatch.setenv("GRANTS_PROFILE_FILE", str(PROFILE))
        assert fresh.ingest() == 0
        after = stored(fresh, "E2E-STATES")
        assert (after["relevance_score"], after["relevance_model"]) == (0, RULE_MODEL)
        assert after["summary"] == before["summary"]
