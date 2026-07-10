"""Unit tests for the pure data-pipeline helpers."""
from __future__ import annotations

import sys
from pathlib import Path

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

    def test_does_not_clobber_existing_canonical_values(self):
        records = [{"AGENCY": "Existing", "AGENCY_NAME": "Alias"}]
        assert normalize_records(records)[0]["AGENCY"] == "Existing"

    def test_original_records_are_not_mutated(self):
        record = {"AGENCY_NAME": "X"}
        normalize_records([record])
        assert "AGENCY" not in record


class TestDateFilter:
    def test_none_and_old_dates_are_dropped(self, monkeypatch):
        monkeypatch.setenv("GRANTS_GOV_LOOKBACK_DAYS", "90")
        from datetime import datetime, timedelta

        recent = (datetime.utcnow() - timedelta(days=5)).strftime("%m/%d/%Y")
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
