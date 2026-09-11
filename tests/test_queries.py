"""Tests for the shared grant query builder (no database)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from grants.queries import GrantQuery, build_grants_sql, serialize_row


class TestBuildGrantsSql:
    def test_defaults_hide_closed_and_order_by_deadline(self):
        sql, params = build_grants_sql(GrantQuery())
        assert "g.close_date >= CURRENT_DATE" in sql
        assert "opportunity_status <> 'Forecasted'" not in sql
        assert "ORDER BY (g.close_date IS NULL), g.close_date, g.relevance_score DESC NULLS LAST" in sql
        assert params == [300]

    def test_filters_and_param_order(self):
        query = GrantQuery(
            q="wind", min_score=4, closing_within=30, include_forecasted=False,
            watched_only=True, stage="full", due_from=date(2026, 1, 1), due_to=date(2026, 2, 1), limit=5000,
        )
        sql, params = build_grants_sql(query)
        assert "g.close_date < CURRENT_DATE + (%s || ' days')::interval" in sql
        assert "opportunity_status <> 'Forecasted'" in sql
        assert "w.opp_id IS NOT NULL" in sql
        assert "(g.relevance_score >= %s OR w.opp_id IS NOT NULL)" in sql
        assert "g.title ILIKE %s" in sql
        assert params == [30, 4, "full", date(2026, 1, 1), date(2026, 2, 1), "%wind%", "%wind%", "%wind%", "%wind%", 1000]

    def test_min_score_zero_adds_no_condition(self):
        sql, params = build_grants_sql(GrantQuery(min_score=0, include_closed=True))
        assert "relevance_score >=" not in sql
        assert "WHERE" not in sql
        assert params == [300]

    def test_description_only_when_asked(self):
        assert "g.description" not in build_grants_sql(GrantQuery())[0]
        assert "g.description" in build_grants_sql(GrantQuery(), with_description=True)[0]


class TestSerializeRow:
    def test_days_left_and_types(self):
        row = {
            "opp_id": "A", "title": "T", "opportunity_status": "Forecasted",
            "close_date": datetime(2026, 9, 21, 0, 0), "post_date": date(2026, 9, 1),
            "award_ceiling": Decimal("250000.00"), "watched": None, "relevance_score": 4,
        }
        out = serialize_row(row, today=date(2026, 9, 11))
        assert out["days_left"] == 10
        assert out["forecasted"] is True
        assert out["close_date"] == "2026-09-21T00:00:00"
        assert out["post_date"] == "2026-09-01"
        assert out["award_ceiling"] == 250000.0
        assert out["watched"] is False
        assert "description" not in out

    def test_missing_close_date(self):
        out = serialize_row({"opp_id": "A", "close_date": None}, today=date(2026, 9, 11))
        assert out["days_left"] is None and out["forecasted"] is False
