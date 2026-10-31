"""A member's own research description: what is accepted, and the personal profile and key built from it."""
from __future__ import annotations

import unicodedata
from dataclasses import replace

import pytest
from hypothesis import example, given, strategies as st

from grants.feedback import clean_text
from grants.members import MAX_SUMMARY_CHARS, MIN_SUMMARY_CHARS, ProfileError, clean_summary, personal_profile
from grants.profile import Profile
from llm_utils.relevance import PERSONAL_KEY_SUFFIX, personal_key, score_key

SENTENCE = "I study wind-energy forecasting and the resilience of rural power grids."
TEAM = Profile(
    summary="The team studies cybersecurity.", institution="UND", applicant_type="Public university",
    keywords=["cyber"], agencies_exclude=["HHS"], min_score_to_notify=3, deadline_horizons=[14], applicant_codes=["06"],
)


def placeholder(text: str) -> bool:
    return not Profile(summary=text).is_configured


@given(st.text(max_size=2600))
@example("x" * 39)
@example("x" * 40)
@example("x" * 2000)
@example("x" * 2001)
@example("y" * 38 + "\x00‮​")  # only 38 characters are left after cleaning
@example(" ".join(["word"] * 400))  # 1,999 characters
@example("REPLACE ME: " + "x" * 60)
def test_a_summary_is_stored_clean_and_within_bounds_or_refused(text):
    cleaned = clean_text(text)
    acceptable = MIN_SUMMARY_CHARS <= len(cleaned) <= MAX_SUMMARY_CHARS and not placeholder(cleaned)
    if not acceptable:
        with pytest.raises(ProfileError):
            clean_summary(text)
        return
    stored = clean_summary(text)
    assert stored == cleaned  # nothing but cleaning: never cut, never padded
    assert MIN_SUMMARY_CHARS <= len(stored) <= MAX_SUMMARY_CHARS
    assert not any(unicodedata.category(ch)[0] == "C" for ch in stored)  # no control or format character
    assert stored == stored.strip() and "  " not in stored and not any(ch.isspace() and ch != " " for ch in stored)
    assert clean_summary(stored) == stored  # cleaning twice changes nothing


class TestCleanSummary:
    def test_the_bounds_are_40_and_2000_characters_after_cleaning(self):
        assert len(clean_summary("x" * 40)) == 40 and len(clean_summary("x" * 2000)) == 2000
        with pytest.raises(ProfileError, match="at least 40"):
            clean_summary("x" * 39)
        with pytest.raises(ProfileError, match="2,000 characters or fewer; this is 2,001"):
            clean_summary("x" * 2001)

    def test_whitespace_and_hidden_characters_do_not_count_toward_the_length(self):
        assert clean_summary(f"  {SENTENCE}\n\n\t  ") == SENTENCE
        assert clean_summary("wind​" * 10 + " grid" * 6) == "wind" * 10 + " grid" * 6
        with pytest.raises(ProfileError):
            clean_summary("x" * 30 + "‮\x00" * 40)

    def test_the_templates_placeholder_is_refused_however_long(self):
        for text in ("REPLACE ME", "replace me with " + SENTENCE, "  Replace Me: " + SENTENCE):
            with pytest.raises(ProfileError, match="placeholder"):
                clean_summary(text)
        assert clean_summary("Please replace me" + SENTENCE)  # only a start with it is the placeholder

    def test_nothing_is_not_a_description(self):
        for value in ("", "   \n", None):
            with pytest.raises(ProfileError):
                clean_summary(value)


class TestPersonalProfile:
    def test_only_the_summary_is_replaced(self):
        mine = personal_profile(TEAM, SENTENCE)
        assert mine.summary == SENTENCE
        assert replace(mine, summary=TEAM.summary) == TEAM  # everything else is the team's
        assert (mine.institution, mine.applicant_type, mine.applicant_codes, mine.agencies_exclude) == ("UND", "Public university", ["06"], ["HHS"])
        assert (mine.deadline_horizons, mine.min_score_to_notify, mine.keywords) == ([14], 3, ["cyber"])
        assert TEAM.summary == "The team studies cybersecurity."  # the team's profile is not touched
        assert mine.is_configured

    def test_a_team_without_a_summary_still_gives_a_configured_personal_profile(self):
        assert personal_profile(Profile(summary="REPLACE ME"), SENTENCE).is_configured


class TestPersonalKey:
    def test_it_is_the_score_key_plus_the_suffix(self):
        mine = personal_profile(TEAM, SENTENCE)
        assert PERSONAL_KEY_SUFFIX == ":p1"
        assert personal_key(mine, "model-one") == score_key(mine, "model-one") + ":p1"
        assert personal_key(mine, "model-one") != score_key(mine, "model-one")

    def test_the_existing_key_rules_apply(self):
        mine = personal_profile(TEAM, SENTENCE)
        base = personal_key(mine, "m")
        assert personal_key(replace(mine, summary=SENTENCE + " Also batteries."), "m") != base  # an edit
        assert personal_key(replace(mine, institution="NDSU"), "m") != base  # a team change
        assert personal_key(replace(mine, applicant_type="Tribal college"), "m") != base
        assert personal_key(mine, "other-model") != base
        assert personal_key(replace(mine, agencies_exclude=[], keywords=["x"], min_score_to_notify=1), "m") == base  # not what the scorer sees

    def test_it_follows_the_scorer_version(self, monkeypatch):
        from llm_utils import relevance

        mine = personal_profile(TEAM, SENTENCE)
        before = personal_key(mine, "m")
        monkeypatch.setattr(relevance, "SCORER_VERSION", relevance.SCORER_VERSION + 1)
        assert personal_key(mine, "m") != before

    @given(st.text(max_size=80), st.text(max_size=80), st.text(max_size=20))
    def test_a_personal_key_is_never_the_score_key_it_is_built_on(self, team_summary, own_summary, model):
        team = replace(TEAM, summary=team_summary)
        assert personal_key(personal_profile(team, own_summary), model).endswith(f":{model}{PERSONAL_KEY_SUFFIX}")
        assert personal_key(team, model) != score_key(team, model)
