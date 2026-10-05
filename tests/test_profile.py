"""Tests for the research profile loader."""
from __future__ import annotations

import textwrap

from grants.profile import Profile, load_profile
from llm_utils.keywords_gen import keyword_extractor


def _write(tmp_path, body: str):
    path = tmp_path / "profile.yml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class TestLoadProfile:
    def test_template_is_not_configured(self):
        profile = load_profile()
        assert profile.source is not None
        assert profile.is_configured is False
        assert profile.keywords == ["research", "education", "innovation", "technology", "infrastructure"]
        assert profile.deadline_horizons == [7, 30, 60, 90]

    def test_filled_profile_is_configured(self, tmp_path):
        path = _write(
            tmp_path,
            """
            summary: We study wind-energy forecasting for rural grids.
            institution: UND
            keywords: [wind, grid]
            keyword_threshold: 2
            agencies_exclude: [HHS-NIH, "Department of Defense"]
            min_score_to_notify: 3
            deadline_horizons: [14, 45]
            include_forecasted: false
            """,
        )
        profile = load_profile(path)
        assert profile.is_configured is True
        assert profile.keywords == ["wind", "grid"]
        assert profile.keyword_threshold == 2
        assert profile.min_score_to_notify == 3
        assert profile.deadline_horizons == [14, 45]
        assert profile.include_forecasted is False
        assert profile.excludes_agency("HHS-NIH-NCI", "National Cancer Institute")
        assert profile.excludes_agency("DOD-ONR", "Department of Defense, Office of Naval Research")
        assert not profile.excludes_agency("NSF", "National Science Foundation")

    def test_env_overrides_file(self, tmp_path, monkeypatch):
        path = _write(tmp_path, "summary: x\nkeywords: [a]\nkeyword_threshold: 1\n")
        monkeypatch.setenv("GRANTS_KEYWORDS", "solar, storage")
        monkeypatch.setenv("GRANTS_KEYWORD_THRESHOLD", "2")
        monkeypatch.setenv("GRANTS_INCLUDE_FORECAST", "false")
        monkeypatch.setenv("GRANTS_MIN_SCORE", "9")
        profile = load_profile(path)
        assert profile.keywords == ["solar", "storage"]
        assert profile.keyword_threshold == 2
        assert profile.include_forecasted is False
        assert profile.min_score_to_notify == 5  # clamped

    def test_missing_file_falls_back_to_defaults(self, tmp_path):
        profile = load_profile(tmp_path / "nope.yml")
        assert profile.source is None
        assert profile.is_configured is False
        assert profile.keyword_threshold == 1

    def test_fingerprint_tracks_summary_only(self):
        a = Profile(summary="A", keywords=["x"])
        b = Profile(summary="A", keywords=["y"], min_score_to_notify=1)
        c = Profile(summary="B")
        assert a.fingerprint() == b.fingerprint()
        assert a.fingerprint() != c.fingerprint()

    def test_prompt_text_includes_institution(self):
        profile = Profile(summary="Grid work.", institution="UND", applicant_type="public university")
        text = profile.as_prompt_text()
        assert "Grid work." in text and "Institution: UND." in text and "public university" in text


class TestKeywordExtractor:
    def test_reads_profile(self):
        profile = Profile(summary="s", keywords=["k1", "k2"], keyword_threshold=2, include_forecasted=False)
        assert keyword_extractor(profile) == (["k1", "k2"], 2, False)


class TestPrivateOverrides:
    def test_local_file_beats_template(self, tmp_path, monkeypatch):
        from grants import profile as mod

        template = tmp_path / "research_profile.yml"
        template.write_text("summary: REPLACE ME\nkeywords: [a]\n", encoding="utf-8")
        local = tmp_path / "research_profile.local.yml"
        local.write_text("summary: Real work.\nkeywords: [b]\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_DEFAULT_PATH", template)
        monkeypatch.setattr(mod, "_LOCAL_PATH", local)
        monkeypatch.delenv("GRANTS_PROFILE_FILE", raising=False)
        loaded = load_profile()
        assert loaded.source == local and loaded.is_configured and loaded.keywords == ["b"]

    def test_env_summary_configures_without_a_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GRANTS_PROFILE_SUMMARY", "Grid resilience research.")
        monkeypatch.setenv("GRANTS_PROFILE_INSTITUTION", "UND")
        loaded = load_profile(tmp_path / "missing.yml")
        assert loaded.is_configured and loaded.institution == "UND"
