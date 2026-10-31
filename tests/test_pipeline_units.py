"""Unit tests for the pure data-pipeline helpers."""
from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grants_data.date_filter_data import date_filter_json_data
from grants_data.keyword_filter_data import filter_grants_by_keywords
from grants_data.normalize import normalize_records, strip_html
from llm_utils.gpt_summarizer import description_summarizer


class TestStripHtml:
    def test_removes_tags_and_entities(self):
        assert strip_html("<p>Hello &amp; <b>world</b></p>") == "Hello & world"

    def test_collapses_whitespace(self):
        assert strip_html("a\n\n  b\t c") == "a b c"

    def test_handles_none_and_empty(self):
        assert strip_html(None) == ""
        assert strip_html("") == ""


class TestNormalizeRecords:
    def test_fills_canonical_keys_from_export_aliases(self):
        records = [
            {
                "AGENCY_NAME": "Rural Utilities Service",
                "OPPORTUNITY_NUMBER_LINK": "https://www.grants.gov/search-results-detail/360670",
                "CATEGORY_OF_FUNDING_ACTIVITY": "Community Development",
            }
        ]
        out = normalize_records(records)[0]
        assert out["AGENCY"] == "Rural Utilities Service"
        assert out["OPPORTUNITY_URL"].endswith("/360670")
        assert out["FUNDING_CATEGORIES"] == "Community Development"

    def test_forecasted_export_dates_fall_back_to_estimates(self):
        records = [{"OPPORTUNITY_STATUS": "Forecasted", "POSTED_DATE": None, "CLOSE_DATE": None,
                    "ESTIMATED_POST_DATE": "10/01/2026", "ESTIMATED_APPLICATION_DUE_DATE": "12/01/2026",
                    "ELIGIBLE_APPLICANTS": "State governments"}]
        out = normalize_records(records)[0]
        assert out["POSTED_DATE"] == "10/01/2026"
        assert out["CLOSE_DATE"] == "12/01/2026"
        assert out["ADDITIONAL_INFORMATION_ON_ELIGIBILITY"] == "State governments"

    def test_does_not_clobber_existing_canonical_values(self):
        records = [{"AGENCY": "Existing", "AGENCY_NAME": "Alias"}]
        assert normalize_records(records)[0]["AGENCY"] == "Existing"

    def test_original_records_are_not_mutated(self):
        record = {"AGENCY_NAME": "X"}
        normalize_records([record])
        assert "AGENCY" not in record

    def test_title_and_agency_entities_are_unescaped_once(self):
        record = {"OPPORTUNITY_TITLE": "Women&apos;s Health &amp;amp; Equity", "AGENCY_NAME": "Arts &amp; Culture"}
        out = normalize_records([record])[0]
        assert out["OPPORTUNITY_TITLE"] == "Women's Health &amp; Equity"  # one pass, not two
        assert out["AGENCY"] == "Arts & Culture"
        assert record["OPPORTUNITY_TITLE"] == "Women&apos;s Health &amp;amp; Equity"

    def test_description_is_left_for_strip_html(self):
        record = {"FUNDING_DESCRIPTION": "<p>Use &lt;b&gt; &amp; more</p>", "OPPORTUNITY_TITLE": None}
        out = normalize_records([record])[0]
        assert out["FUNDING_DESCRIPTION"] == record["FUNDING_DESCRIPTION"]
        assert out["OPPORTUNITY_TITLE"] is None

    @given(st.text(max_size=40).filter(lambda t: "&" not in t))
    def test_a_title_without_an_ampersand_is_unchanged(self, title):
        assert normalize_records([{"OPPORTUNITY_TITLE": title}])[0]["OPPORTUNITY_TITLE"] == title


class TestDateFilter:
    def test_none_and_old_dates_are_dropped(self, monkeypatch):
        monkeypatch.setenv("GRANTS_GOV_LOOKBACK_DAYS", "90")
        from datetime import datetime, timedelta, timezone

        recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%m/%d/%Y")
        records = [
            {"POSTED_DATE": None},
            {"POSTED_DATE": ""},
            {"POSTED_DATE": "01/01/2010"},
            {"POSTED_DATE": recent},
        ]
        out = date_filter_json_data(records)
        assert len(out) == 1
        assert out[0]["POSTED_DATE"] == recent

    def test_invalid_lookback_falls_back(self, monkeypatch):
        monkeypatch.setenv("GRANTS_GOV_LOOKBACK_DAYS", "ninety")
        assert date_filter_json_data([]) == []

    def test_no_window_by_default(self, monkeypatch):
        # Long-running solicitations were posted years ago and are still open.
        monkeypatch.delenv("GRANTS_GOV_LOOKBACK_DAYS", raising=False)
        records = [{"POSTED_DATE": "01/01/2010"}, {"POSTED_DATE": None}]
        assert date_filter_json_data(records) == records


class TestKeywordFilter:
    def test_matches_ignore_html_markup(self):
        records = [
            {"FUNDING_DESCRIPTION": '<p style="research">unrelated content</p>'},
            {"FUNDING_DESCRIPTION": "<p>advanced research program</p>"},
        ]
        out = filter_grants_by_keywords(records, "FUNDING_DESCRIPTION", ["research"], 1)
        assert len(out) == 1
        assert out[0]["MATCHED_KEYWORDS"] == ["research"]

    def test_threshold_respected(self):
        records = [{"FUNDING_DESCRIPTION": "education only"}]
        out = filter_grants_by_keywords(
            records, "FUNDING_DESCRIPTION", ["education", "research"], 2
        )
        assert out == []

    def test_word_boundaries_prevent_substring_matches(self):
        records = [
            {"FUNDING_DESCRIPTION": "this particular program"},
            {"FUNDING_DESCRIPTION": "funding for art programs"},
            {"FUNDING_DESCRIPTION": "Art, culture, and heritage"},
        ]
        out = filter_grants_by_keywords(records, "FUNDING_DESCRIPTION", ["art"], 1)
        assert len(out) == 2
        assert all("particular" not in r["FUNDING_DESCRIPTION"] for r in out)


class TestSummarizer:
    def test_summary_is_plain_text(self):
        records = [{"FUNDING_DESCRIPTION": "<p>" + "word " * 200 + "&nbsp;</p>"}]
        out = description_summarizer(records)
        assert "<" not in out[0]["SUMMARY"]
        assert "&nbsp;" not in out[0]["SUMMARY"]
        assert len(out[0]["SUMMARY"]) <= 323  # 320 + ellipsis

    def test_short_description_kept_verbatim(self):
        out = description_summarizer([{"FUNDING_DESCRIPTION": "Short text."}])
        assert out[0]["SUMMARY"] == "Short text."


class TestDropClosed:
    def test_past_deadlines_dropped_and_undated_kept(self):
        from datetime import date
        from grants_data.date_filter_data import drop_closed_grants

        records = [
            {"OPPORTUNITY_NUMBER": "PAST", "CLOSE_DATE": "09/01/2026"},
            {"OPPORTUNITY_NUMBER": "TODAY", "CLOSE_DATE": "09/11/2026"},
            {"OPPORTUNITY_NUMBER": "FUTURE", "CLOSE_DATE": "2026-10-01"},
            {"OPPORTUNITY_NUMBER": "NONE", "CLOSE_DATE": None},
            {"OPPORTUNITY_NUMBER": "BAD", "CLOSE_DATE": "soon"},
        ]
        kept = [r["OPPORTUNITY_NUMBER"] for r in drop_closed_grants(records, today=date(2026, 9, 11))]
        assert kept == ["TODAY", "FUTURE", "NONE", "BAD"]

    def test_archived_grants_dropped_even_with_a_later_close_date(self):
        from datetime import date
        from grants_data.date_filter_data import drop_closed_grants

        records = [
            {"OPPORTUNITY_NUMBER": "ARCHIVED", "CLOSE_DATE": "12/01/2026", "ARCHIVE_DATE": "09/01/2026"},
            {"OPPORTUNITY_NUMBER": "OPEN", "CLOSE_DATE": "12/01/2026", "ARCHIVE_DATE": "01/01/2027"},
            {"OPPORTUNITY_NUMBER": "ROLLING", "CLOSE_DATE": None, "ARCHIVE_DATE": None},
        ]
        kept = [r["OPPORTUNITY_NUMBER"] for r in drop_closed_grants(records, today=date(2026, 9, 11))]
        assert kept == ["OPEN", "ROLLING"]


class TestStage:
    def test_whole_phrases_only(self):
        from grants.data.loader import derive_stage

        assert derive_stage("Exploit detection for conceptual models", "") == "full"
        assert derive_stage("Grid security", "A letter of intent is due in March.") == "concept"
        assert derive_stage("Pre-proposals invited", "") == "concept"
        assert derive_stage("Program", "Submit an LOI by email.") == "concept"
