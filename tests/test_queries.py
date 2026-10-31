"""Tests for the shared grant query builder (no database)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from hypothesis import given, strategies as st

from grants import queries
from grants.queries import GrantQuery, build_grants_sql, serialize_row


class TestBuildGrantsSql:
    def test_defaults_hide_closed_and_order_by_deadline(self):
        sql, params = build_grants_sql(GrantQuery())
        assert "g.close_date >= CURRENT_DATE" in sql
        assert "g.archive_date >= CURRENT_DATE" in sql  # archived grants are closed
        assert "SELECT MAX(last_seen_at) FROM grants" in sql  # so are ones gone from the extract
        assert "opportunity_status <> 'Forecasted'" not in sql
        assert "ORDER BY (g.close_date IS NULL), g.close_date, g.relevance_score DESC NULLS LAST" in sql
        assert params == [300]

    def test_filters_and_param_order(self):
        query = GrantQuery(
            q="wind", min_score=4, closing_within=30, include_forecasted=False,
            watched_only=True, stage="full", due_from=date(2026, 1, 1), due_to=date(2026, 2, 1), limit=5000,
            today=date(2026, 9, 1),
        )
        sql, params = build_grants_sql(query)
        assert "g.close_date <= %s" in sql and "CURRENT_DATE +" not in sql  # inclusive, with a date worked out in Python
        assert "opportunity_status <> 'Forecasted'" in sql
        assert "w.opp_id IS NOT NULL" in sql
        assert "(g.relevance_score >= %s OR w.opp_id IS NOT NULL)" in sql
        assert "g.title ILIKE %s" in sql
        assert params == [date(2026, 10, 1), 4, "full", date(2026, 1, 1), date(2026, 2, 1), "%wind%", "%wind%", "%wind%", "%wind%", 1000]

    def test_min_score_zero_adds_no_condition(self):
        sql, params = build_grants_sql(GrantQuery(min_score=0, include_closed=True))
        assert "relevance_score >=" not in sql
        assert "WHERE" not in sql
        assert params == [300]

    def test_a_posting_the_team_marked_relevant_passes_the_score_floor_like_a_tracked_one(self):
        query = GrantQuery(min_score=4, team_relevant=("B-2", "A-1"), today=date(2026, 9, 1))
        sql, params = build_grants_sql(query)
        assert "(g.relevance_score >= %s OR w.opp_id IS NOT NULL OR g.opp_id = ANY(%s))" in sql
        assert params == [4, ["B-2", "A-1"], 300]  # a list, which psycopg2 sends as an array (a tuple would be a row)
        assert "g.opp_id = ANY" not in build_grants_sql(GrantQuery(min_score=4))[0]  # nobody marked, nothing added

    def test_the_team_does_not_widen_a_list_with_no_floor(self):
        sql, params = build_grants_sql(GrantQuery(min_score=0, team_relevant=("A-1",), include_closed=True))
        assert "ANY" not in sql and "relevance_score >=" not in sql and params == [300]
        assert "ANY" not in build_grants_sql(GrantQuery(team_relevant=("A-1",)))[0]

    def test_it_widens_only_the_score_condition(self):
        # The other filters still apply to a grant the team marked: closed, out of the window, or off the search.
        sql, params = build_grants_sql(GrantQuery(q="wind", min_score=4, closing_within=30, team_relevant=("A-1",), today=date(2026, 9, 1)))
        where = sql.split("WHERE")[1].split("ORDER BY")[0]
        assert " AND (g.relevance_score >= %s OR w.opp_id IS NOT NULL OR g.opp_id = ANY(%s)) AND (g.title ILIKE %s" in where  # one term of the AND
        assert "g.close_date >= CURRENT_DATE" in where and "g.close_date <= %s" in where
        assert params == [date(2026, 10, 1), 4, ["A-1"], "%wind%", "%wind%", "%wind%", "%wind%", 300]

    @given(
        q=st.text(max_size=5),
        min_score=st.one_of(st.none(), st.integers(0, 5)),
        closing_within=st.one_of(st.none(), st.integers(1, 365)),
        watched_only=st.booleans(),
        include_closed=st.booleans(),
        stage=st.sampled_from([None, "concept", "full"]),
        team=st.lists(st.text(min_size=1, max_size=6), max_size=4).map(tuple),
    )
    def test_every_placeholder_has_a_parameter_and_the_count_reads_the_same_condition(self, q, min_score, closing_within, watched_only, include_closed, stage, team):
        query = GrantQuery(q=q, min_score=min_score, closing_within=closing_within, watched_only=watched_only,
                           include_closed=include_closed, stage=stage, team_relevant=team, today=date(2026, 9, 1))
        sql, params = build_grants_sql(query)
        assert sql.count("%s") == len(params)
        where, where_params = queries._where(query)
        assert where_params == params[:-1] and where in sql  # count_grants adds this where and nothing else
        assert ("g.opp_id = ANY(%s)" in sql) == bool(team and min_score)

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
        assert out["firm_deadline"] is False

    def test_a_real_deadline_is_firm(self):
        out = serialize_row({"opp_id": "A", "close_date": datetime(2026, 9, 21)}, today=date(2026, 9, 11))
        assert out["firm_deadline"] is True and out["days_left"] == 10

    def test_placeholder_close_date_has_no_day_count(self):
        out = serialize_row({"opp_id": "A", "close_date": datetime(2076, 10, 5)}, today=date(2026, 9, 11))
        assert out["days_left"] is None and out["firm_deadline"] is False
        assert out["close_date"] == "2076-10-05T00:00:00"  # the raw date stays; only the claims are withdrawn

    def test_placeholder_award_ceiling_is_null(self):
        for ceiling, shown in [(Decimal("999999999.00"), None), (Decimal("1000000000"), None), (Decimal("999999998"), 999999998.0)]:
            assert serialize_row({"opp_id": "A", "award_ceiling": ceiling})["award_ceiling"] == shown
