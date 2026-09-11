"""Tests for the deadline-first digest builder."""
from __future__ import annotations

from datetime import date

from notifications.digest import DigestItem, build_digest, merge_items

TODAY = date(2026, 9, 11)


def _item(opp, days, score=4, **kw):
    return DigestItem(opp_id=opp, title=f"Grant {opp}", agency="NSF", url=f"https://x/{opp}",
                      close_date=date.fromordinal(TODAY.toordinal() + days) if days is not None else None,
                      score=score, reason=f"why {opp}", **kw)


class TestBuildDigest:
    def test_none_when_nothing_relevant(self):
        items = [_item("A", 5, score=2), _item("B", 500, score=5)]
        assert build_digest(items, today=TODAY, horizons=[7, 30], min_score=4) is None

    def test_sections_and_subject(self):
        items = [
            _item("URGENT", 3, score=5, is_new=True),
            _item("MONTH", 20, score=4),
            _item("LOW", 2, score=1),
            _item("TRACKED", 50, score=None, watched=True),
            _item("PAST", -1, score=5),
            _item("NODATE", None, score=5, is_new=True),
        ]
        digest = build_digest(items, today=TODAY, horizons=[7, 30, 60], min_score=4, profile_line="UND")
        assert digest is not None
        assert digest.subject == "GrantWatch: 2 new, 1 closing within 7 days"
        assert digest.new_count == 2 and digest.deadline_count == 3
        text = digest.text
        assert "New this run (2)" in text
        assert "Closes within 7 days (1)" in text
        assert "Closes in 8 to 30 days (1)" in text
        assert "Closes in 31 to 60 days (1)" in text
        assert "Grant LOW" not in text and "Grant PAST" not in text
        assert "Grant TRACKED" in text and "tracked" in text
        assert "(3 days left)" in text
        assert "[unscored]" in text
        assert "UND" in text

    def test_html_escapes_and_links(self):
        item = _item("A", 2, score=5, is_new=True)
        item.title = "Cats & <Dogs>"
        digest = build_digest([item], today=TODAY, horizons=[7], min_score=4)
        assert "Cats &amp; &lt;Dogs&gt;" in digest.html
        assert 'href="https://x/A"' in digest.html
        assert "<Dogs>" not in digest.html

    def test_forecasted_marked_estimated(self):
        digest = build_digest([_item("F", 10, score=4, forecasted=True)], today=TODAY, horizons=[7, 30], min_score=4)
        assert "estimated" in digest.text
        assert digest.subject == "GrantWatch: 1 deadline in the next 30 days"


class TestMergeItems:
    def test_run_items_override_but_keep_flags(self):
        db = [_item("A", 10, score=5, watched=True), _item("B", 12, score=3)]
        run = [_item("A", 11, score=None, is_new=False), _item("C", 3, score=4, is_new=True)]
        run[0].reason = ""
        merged = {i.opp_id: i for i in merge_items(db, run)}
        assert merged["A"].watched is True
        assert merged["A"].score == 5 and merged["A"].reason == "why A"  # carried from db
        assert merged["A"].close_date == date(2026, 9, 22)  # fresher date from the run
        assert merged["C"].is_new is True
        assert set(merged) == {"A", "B", "C"}


class TestFromRecord:
    def test_maps_pipeline_fields(self):
        record = {
            "OPPORTUNITY_NUMBER": "X-1", "OPPORTUNITY_TITLE": "T", "AGENCY": "DOE",
            "OPPORTUNITY_URL": "u", "CLOSE_DATE": "10/01/2026", "OPPORTUNITY_STATUS": "Forecasted",
            "RELEVANCE_SCORE": "4", "RELEVANCE_REASON": "r", "SUMMARY": "s",
        }
        item = DigestItem.from_record(record, is_new=True)
        assert item.close_date == date(2026, 10, 1) and item.forecasted and item.score == 4
        assert item.days_left(TODAY) == 20
