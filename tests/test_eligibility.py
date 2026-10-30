"""The eligibility pre-check from Grants.gov applicant codes, and scoring every open grant when a model is set up."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from hypothesis import given, strategies as st

from grants import profile as profile_module
from grants.data.loader import LoadSummary
from grants.profile import APPLICANT_CODES, Profile, load_profile
from grants_data import pipeline
from llm_utils.relevance import RULE_MODEL, score_by_rule, score_key
from test_relevance import FakeClient, _echo_scores, _numbers_in

CODES = sorted(APPLICANT_CODES)
# Records can carry a code the table does not know, or one that is not a number at all.
RECORD_CODES = CODES + ["03", "77", "XX"]


def _outside(codes, mine):
    """Independent statement of the rule: known codes, none of them ours, unrestricted or "others"."""
    return bool(codes) and all(code in APPLICANT_CODES for code in codes) and not set(codes) & (set(mine) | {"25", "99"})


@st.composite
def records(draw):
    out = []
    for index in range(draw(st.integers(0, 12))):
        record = {"OPPORTUNITY_NUMBER": f"G-{index}"}
        if draw(st.booleans()):  # otherwise the key is absent: the codes are unknown
            record["ELIGIBLE_APPLICANT_CODES"] = draw(st.lists(st.sampled_from(RECORD_CODES), max_size=6))
        out.append(record)
    return out


applicant_choices = st.lists(st.sampled_from(CODES), min_size=1, max_size=5, unique=True).map(sorted)


class TestPreCheckProperties:
    @given(records())
    def test_no_applicant_codes_is_the_identity(self, rows):
        assert pipeline._split_by_eligibility(rows, Profile()) == (rows, [])

    @given(records(), applicant_choices)
    def test_records_without_codes_are_kept(self, rows, mine):
        profile = Profile(applicant_codes=mine)
        _, dropped = pipeline._split_by_eligibility(rows, profile)
        assert all(row.get("ELIGIBLE_APPLICANT_CODES") for row in dropped)

    @given(records(), applicant_choices)
    def test_drops_exactly_the_known_codes_outside_the_set(self, rows, mine):
        kept, dropped = pipeline._split_by_eligibility(rows, Profile(applicant_codes=mine))
        assert dropped == [row for row in rows if _outside(row.get("ELIGIBLE_APPLICANT_CODES"), mine)]
        assert kept == [row for row in rows if not _outside(row.get("ELIGIBLE_APPLICANT_CODES"), mine)]

    @given(records(), applicant_choices)
    def test_a_grant_open_to_us_unrestricted_or_to_others_is_never_dropped(self, rows, mine):
        _, dropped = pipeline._split_by_eligibility(rows, Profile(applicant_codes=mine))
        for row in dropped:
            assert not set(row["ELIGIBLE_APPLICANT_CODES"]) & (set(mine) | {"25", "99"})

    def test_examples(self):
        university = Profile(applicant_codes=["06"])
        assert university.excludes_applicants(["00", "07"]) is True
        assert university.excludes_applicants(["00", "06"]) is False
        assert university.excludes_applicants(["23", "25"]) is False  # others, see text
        assert university.excludes_applicants(["99"]) is False
        assert university.excludes_applicants(["00", "03"]) is False  # 03 is not a code Grants.gov lists
        assert university.excludes_applicants([]) is False and university.excludes_applicants(None) is False
        assert university.excludes_applicants(["6", "0"]) is False  # one digit: read as 06 and 00
        assert university.excludes_applicants(["0", "7"]) is True


class TestApplicantCodesInTheProfile:
    @given(st.integers(0, 99))
    def test_a_yaml_code_loads_as_its_two_digits(self, number):
        code = f"{number:02d}"
        loaded = yaml.safe_load(f"applicant_codes: [{code}]")  # 06 is read as the number 6, and 08 as text
        assert profile_module._applicant_codes(loaded["applicant_codes"]) == ([code] if code in APPLICANT_CODES else [])

    def test_unquoted_06_loads_as_06(self, tmp_path):
        path = tmp_path / "profile.yml"
        path.write_text("summary: Grid work.\napplicant_codes: [06, 20, 08]\n", encoding="utf-8")
        assert load_profile(path).applicant_codes == ["06", "08", "20"]

    def test_quoted_text_and_a_single_value_work_too(self, tmp_path):
        path = tmp_path / "profile.yml"
        path.write_text('summary: x\napplicant_codes: ["06", "99"]\n', encoding="utf-8")
        assert load_profile(path).applicant_codes == ["06", "99"]
        path.write_text("summary: x\napplicant_codes: 06\n", encoding="utf-8")
        assert load_profile(path).applicant_codes == ["06"]
        path.write_text("summary: x\napplicant_codes: 06, 20\n", encoding="utf-8")
        assert load_profile(path).applicant_codes == ["06", "20"]

    def test_unknown_codes_are_warned_about_and_ignored(self, tmp_path, monkeypatch):
        warnings = []
        monkeypatch.setattr(profile_module, "logger", lambda level, message: warnings.append((level, message)))
        path = tmp_path / "profile.yml"
        path.write_text("summary: x\napplicant_codes: [06, 3, 77, public, 6.5]\n", encoding="utf-8")
        assert load_profile(path).applicant_codes == ["06"]
        assert sorted(message.split("'")[1] for level, message in warnings if level == "warning") == ["3", "6.5", "77", "public"]

    def test_the_committed_template_leaves_the_pre_check_off(self):
        assert load_profile().applicant_codes == []

    def test_the_codes_do_not_rescore_anything(self):
        assert score_key(Profile(summary="A"), "m") == score_key(Profile(summary="A", applicant_codes=["06"]), "m")


class TestScoreByRule:
    def test_scores_zero_with_the_reason_and_no_summary(self):
        profile = Profile(summary="Grid work.")
        rows = [{"OPPORTUNITY_NUMBER": "A", "SUMMARY": "kept"}, {"OPPORTUNITY_NUMBER": "B"}]
        scored = score_by_rule(rows, profile, "Not open to us")
        assert [r["RELEVANCE_SCORE"] for r in scored] == [0, 0]
        assert {r["RELEVANCE_MODEL"] for r in scored} == {RULE_MODEL} == {"rule"}
        assert {r["RELEVANCE_REASON"] for r in scored} == {"Not open to us"}
        assert scored[0]["SUMMARY"] == "kept" and "SUMMARY" not in scored[1]
        assert "RELEVANCE_SCORE" not in rows[0]  # the input is not changed

    def test_the_stored_key_is_never_a_models_key(self):
        profile = Profile(summary="Grid work.")
        scored = score_by_rule([{"OPPORTUNITY_NUMBER": "A"}], profile, "r")[0]
        assert scored["RELEVANCE_PROFILE"] == score_key(profile, "rule")
        assert scored["RELEVANCE_PROFILE"] != score_key(profile, "qwen")


def _grant(number, description="", codes=None, **extra):
    record = {
        "OPPORTUNITY_NUMBER": number, "OPPORTUNITY_TITLE": f"Title {number}", "OPPORTUNITY_STATUS": "Posted",
        "FUNDING_DESCRIPTION": description, "CLOSE_DATE": "12/31/2026", **extra,
    }
    if codes is not None:
        record["ELIGIBLE_APPLICANT_CODES"] = codes
    return record


@pytest.fixture
def world(monkeypatch):
    """The ingest pipeline with a fake model and an in-memory stand-in for the database; no network."""

    def fake_extract():
        return True

    fake_extract.last_extract_path = Path("/data/extract.xml")
    state = SimpleNamespace(
        records=[], db={}, loaded=[], client=FakeClient(_echo_scores(score=4)),
        profile=Profile(
            summary="Wind-energy forecasting.", applicant_type="public university", keywords=["wind"],
            keyword_threshold=1, applicant_codes=["06"],
        ),
    )

    def load(rows):
        state.loaded.append([dict(row) for row in rows])
        for row in rows:
            state.db[row["OPPORTUNITY_NUMBER"]] = dict(row)
        return LoadSummary(inserted=len(rows), updated=0, new_ids={row["OPPORTUNITY_NUMBER"] for row in rows})

    def fetch(key):
        return {
            number: {"score": row["RELEVANCE_SCORE"], "reason": row.get("RELEVANCE_REASON") or "",
                     "summary": row.get("SUMMARY") or "", "model": row.get("RELEVANCE_MODEL") or "", "scored_at": None}
            for number, row in state.db.items() if row.get("RELEVANCE_PROFILE") == key and row.get("RELEVANCE_SCORE") is not None
        }

    monkeypatch.setenv("GRANTS_DATA_SOURCE", "extract")
    monkeypatch.setenv("GRANTS_LLM_MODEL", "test-model")
    monkeypatch.setenv("GRANTS_LLM_BATCH_SIZE", "5")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setattr(pipeline, "gen_extract", fake_extract)
    monkeypatch.setattr(pipeline, "process_extract_xml", lambda path: [dict(r) for r in state.records])
    monkeypatch.setattr(pipeline, "load_profile", lambda: state.profile)
    monkeypatch.setattr(pipeline, "normalize_records", lambda rows: rows)
    monkeypatch.setattr(pipeline, "date_filter_json_data", lambda rows: rows)
    monkeypatch.setattr(pipeline, "drop_closed_grants", lambda rows: rows)
    monkeypatch.setattr(pipeline, "load_cached", lambda key: {})
    monkeypatch.setattr(pipeline, "save_cached", lambda *args: None)
    monkeypatch.setattr(pipeline, "fetch_relevance", fetch)
    monkeypatch.setattr(pipeline, "_write_csv", lambda rows: Path("grants.csv"))
    monkeypatch.setattr(pipeline, "load_grants_from_records", load)
    real = pipeline.score_records
    monkeypatch.setattr(
        pipeline, "score_records",
        lambda rows, profile, already_scored, settings: real(rows, profile, already_scored=already_scored, settings=settings, client=state.client),
    )
    return state


def _sent(world):
    return sorted(number for call in world.client.completions.calls for number in _numbers_in(call))


def _loaded(world):
    return {row["OPPORTUNITY_NUMBER"]: row for row in world.loaded[-1]}


STATES_ONLY = ["00"]
OPEN_TO_US = ["06", "20"]


class TestScoringEveryOpenGrant:
    @pytest.fixture
    def mixed(self, world):
        world.records = [
            _grant("WIND", "Wind forecasting for grids.", OPEN_TO_US),
            _grant("NO-WORDS", "A call with no profile keyword.", ["25"]),
            _grant("STATES", "Wind farms owned by states.", STATES_ONLY),
            _grant("NO-CODES", "Another call with nothing to match."),
            _grant("SMALL-BIZ", "Nothing to match.", ["23", "21"]),
        ]
        return world

    def test_the_model_sees_every_grant_that_may_apply_whatever_the_keywords_say(self, mixed):
        assert pipeline.onlyTheGoodStuff()[0] is True
        assert _sent(mixed) == ["NO-CODES", "NO-WORDS", "WIND"]

    def test_a_grant_the_codes_rule_out_is_scored_zero_by_rule_and_still_loaded(self, mixed):
        pipeline.onlyTheGoodStuff()
        rows = _loaded(mixed)
        assert set(rows) == {"WIND", "NO-WORDS", "STATES", "NO-CODES", "SMALL-BIZ"}
        for number in ("STATES", "SMALL-BIZ"):
            assert rows[number]["RELEVANCE_SCORE"] == 0
            assert rows[number]["RELEVANCE_MODEL"] == "rule"
            assert rows[number]["RELEVANCE_REASON"] == "Not open to public university (Grants.gov eligibility codes)"
        assert rows["WIND"]["RELEVANCE_MODEL"] == "test-model" and rows["WIND"]["RELEVANCE_SCORE"] == 4

    def test_keywords_are_still_recorded_for_display(self, mixed):
        pipeline.onlyTheGoodStuff()
        rows = _loaded(mixed)
        assert rows["WIND"]["MATCHED_KEYWORDS"] == ["wind"]
        assert rows["NO-WORDS"]["MATCHED_KEYWORDS"] == []
        assert rows["STATES"]["MATCHED_KEYWORDS"] == ["wind"]

    def test_the_stats_count_what_was_sent_and_what_was_ruled_out(self, mixed):
        pipeline.onlyTheGoodStuff()
        stats = pipeline.onlyTheGoodStuff.last_stats
        assert (stats["after_agency"], stats["ineligible"], stats["sent_to_model"], stats["kept"]) == (5, 2, 3, 5)
        assert stats["scored"] == 3 and "matched" not in stats

    def test_without_applicant_codes_nothing_is_ruled_out(self, mixed):
        mixed.profile.applicant_codes = []
        pipeline.onlyTheGoodStuff()
        assert _sent(mixed) == ["NO-CODES", "NO-WORDS", "SMALL-BIZ", "STATES", "WIND"]
        assert "ineligible" not in pipeline.onlyTheGoodStuff.last_stats
        assert pipeline.onlyTheGoodStuff.last_stats["sent_to_model"] == 5

    def test_the_reason_falls_back_when_the_profile_names_no_applicant_type(self, mixed):
        mixed.profile.applicant_type = ""
        pipeline.onlyTheGoodStuff()
        assert _loaded(mixed)["STATES"]["RELEVANCE_REASON"] == "Not open to your applicant type (Grants.gov eligibility codes)"

    def test_a_rule_score_is_not_a_cached_model_score(self, mixed):
        """Rule-scored, then the profile now includes small businesses: the model scores it."""
        pipeline.onlyTheGoodStuff()
        assert _loaded(mixed)["SMALL-BIZ"]["RELEVANCE_MODEL"] == "rule"
        mixed.client.completions.calls.clear()
        mixed.profile.applicant_codes = ["06", "23"]
        pipeline.onlyTheGoodStuff()
        assert _sent(mixed) == ["SMALL-BIZ"]  # the others are cached under the model's key
        assert mixed.db["SMALL-BIZ"]["RELEVANCE_MODEL"] == "test-model"
        assert mixed.db["STATES"]["RELEVANCE_MODEL"] == "rule"

    def test_a_model_score_gives_way_to_the_rule_when_the_codes_are_added(self, mixed):
        mixed.profile.applicant_codes = []
        pipeline.onlyTheGoodStuff()
        assert mixed.db["STATES"]["RELEVANCE_MODEL"] == "test-model"
        mixed.profile.applicant_codes = ["06"]
        pipeline.onlyTheGoodStuff()
        assert mixed.db["STATES"]["RELEVANCE_SCORE"] == 0 and mixed.db["STATES"]["RELEVANCE_MODEL"] == "rule"

    def test_a_day_where_every_grant_is_ruled_out_still_loads_them(self, world):
        world.records = [_grant("A", "x", STATES_ONLY), _grant("B", "y", ["22"])]
        success, kept = pipeline.onlyTheGoodStuff()
        assert success and len(kept) == 2
        assert world.client.completions.calls == []
        stats = pipeline.onlyTheGoodStuff.last_stats
        assert (stats["ineligible"], stats["sent_to_model"], stats["kept"]) == (2, 0, 2)

    def test_exclusions_that_leave_nothing_end_the_run_cleanly(self, world):
        world.records = [_grant("A", "x", AGENCY_CODE="NSF")]
        world.profile.agencies_exclude = ["NSF"]
        assert pipeline.onlyTheGoodStuff() == (True, [])
        assert pipeline.onlyTheGoodStuff.last_stats["reason"] == "No grants were left after agency exclusions."


class TestWithoutAModel:
    def test_keywords_still_decide_and_nothing_is_scored_by_rule(self, world, monkeypatch):
        """A rule score would make the email service think scoring is on and silence the keyword emails."""
        monkeypatch.delenv("GRANTS_LLM_MODEL")
        world.records = [
            _grant("WIND", "Wind forecasting.", OPEN_TO_US),
            _grant("STATES", "Wind farms owned by states.", STATES_ONLY),
            _grant("NO-WORDS", "Nothing to match.", OPEN_TO_US),
        ]
        assert pipeline.onlyTheGoodStuff()[0] is True
        rows = _loaded(world)
        assert set(rows) == {"WIND", "STATES"}
        assert all(row.get("RELEVANCE_SCORE") is None for row in rows.values())
        stats = pipeline.onlyTheGoodStuff.last_stats
        assert stats["matched"] == 2 and "sent_to_model" not in stats and "ineligible" not in stats

    def test_an_unwritten_profile_keeps_the_keyword_gate_even_with_a_model(self, world):
        world.profile.summary = "REPLACE ME"
        world.records = [_grant("WIND", "Wind forecasting.", OPEN_TO_US), _grant("NO-WORDS", "Nothing.", OPEN_TO_US)]
        pipeline.onlyTheGoodStuff()
        assert set(_loaded(world)) == {"WIND"}
        assert world.client.completions.calls == []
