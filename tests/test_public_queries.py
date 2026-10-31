"""The visitor's own grant query: public columns only, no score anywhere in it (no database)."""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from hypothesis import given, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grants.display import PUBLIC_DETAIL_FIELDS, PUBLIC_FIELDS  # noqa: E402
from grants.queries import (  # noqa: E402
    GrantQuery, build_public_sql, count_public_grants, get_public_grant, public_status, query_public_grants, serialize_public_row,
)
from support import patched  # noqa: E402

TODAY = date(2026, 10, 30)
# Words that mean the team's view of a grant; none may appear anywhere in a public statement.
INTERNAL_SQL = ("relevance", "watch", "w.opp_id", "matched_keywords", "g.summary", "first_seen", "member_", "grant_feedback", "votes")


def queries_with_everything():
    return st.builds(
        GrantQuery,
        q=st.text(max_size=5), min_score=st.one_of(st.none(), st.integers(0, 5)), closing_within=st.one_of(st.none(), st.integers(1, 365)),
        include_forecasted=st.booleans(), include_closed=st.booleans(), watched_only=st.booleans(),
        stage=st.sampled_from([None, "concept", "full"]),
        due_from=st.one_of(st.none(), st.dates(date(2026, 1, 1), date(2027, 1, 1))), due_to=st.one_of(st.none(), st.dates(date(2026, 1, 1), date(2027, 1, 1))),
        limit=st.integers(1, 3000), team_relevant=st.lists(st.text(min_size=1, max_size=4), max_size=3).map(tuple), today=st.just(TODAY),
    )


class TestBuildPublicSql:
    def test_it_selects_grants_gov_columns_and_the_start_of_the_description(self):
        sql, params = build_public_sql(GrantQuery(today=TODAY))
        assert "LEFT(g.description, 321) AS description_head" in sql  # the cut is 320 characters; one more shows it was cut
        assert "FROM grants g" in sql and "JOIN" not in sql
        assert params == [300]

    def test_it_lists_open_grants_by_deadline_and_never_by_a_score(self):
        sql, _ = build_public_sql(GrantQuery())
        assert "g.close_date >= CURRENT_DATE" in sql and "g.archive_date >= CURRENT_DATE" in sql  # open, as everywhere
        assert "ORDER BY g.close_date NULLS LAST, g.post_date DESC, g.title, g.opp_id\n" in sql
        assert sql.index("ORDER BY") < sql.index("LIMIT")  # so the limit cuts the same grants whoever asks

    def test_the_search_reads_the_description_and_not_the_models_summary(self):
        sql, params = build_public_sql(GrantQuery(q="wind"))
        assert "g.title ILIKE %s OR g.agency ILIKE %s OR g.opp_id ILIKE %s OR g.description ILIKE %s" in sql
        assert "g.summary" not in sql and "keywords" not in sql
        assert params == ["%wind%"] * 4 + [300]

    def test_the_visitors_filters_and_their_parameter_order(self):
        query = GrantQuery(closing_within=30, include_forecasted=False, stage="full", due_from=date(2026, 1, 1), due_to=date(2026, 2, 1),
                           q="x", limit=5000, today=date(2026, 9, 1))
        sql, params = build_public_sql(query)
        assert "opportunity_status <> 'Forecasted'" in sql and "g.close_date IS NOT NULL" in sql
        assert params == [date(2026, 10, 1), "full", date(2026, 1, 1), date(2026, 2, 1), "%x%", "%x%", "%x%", "%x%", 1000]

    def test_a_score_floor_the_watchlist_and_closed_grants_are_not_the_visitors_to_ask_for(self):
        plain = build_public_sql(GrantQuery(today=TODAY))
        asked = build_public_sql(GrantQuery(min_score=5, watched_only=True, include_closed=True, team_relevant=("A-1",), today=TODAY))
        assert asked == plain

    @given(query=queries_with_everything())
    def test_no_statement_names_anything_the_team_knows_and_every_placeholder_has_a_value(self, query):
        sql, params = build_public_sql(query)
        assert not [word for word in INTERNAL_SQL if word in sql]
        assert sql.count("%s") == len(params)

    @given(query=queries_with_everything(), floor=st.integers(0, 5), watched=st.booleans(), closed=st.booleans(), team=st.lists(st.text(min_size=1, max_size=3), max_size=3).map(tuple))
    def test_the_statement_is_the_same_whatever_the_score_floor_the_watchlist_or_the_team_marks(self, query, floor, watched, closed, team):
        other = replace(query, min_score=floor, watched_only=watched, include_closed=closed, team_relevant=team)
        assert build_public_sql(other) == build_public_sql(query)


class TestSerializePublicRow:
    def row(self, **extra):
        data = {
            "opp_id": "A-1", "title": "Wind", "stage": "full", "opportunity_status": "Posted", "opportunity_category": "D",
            "funding_categories": "Energy", "agency": "DOE", "agency_code": "DOE", "url": "https://x/A-1", "post_date": datetime(2026, 9, 1),
            "close_date": datetime(2026, 11, 20), "archive_date": None, "award_ceiling": 999999999, "estimated_total_funding": 1500000,
            "description_head": "Funds wind research. " * 40,
        }
        data.update(extra)
        return data

    def test_the_summary_is_the_description_cut_as_the_pipeline_cuts_it(self):
        out = serialize_public_row(self.row(), TODAY)
        assert out["summary"] == ("Funds wind research. " * 40)[:320].rstrip() + "..."
        assert "description_head" not in out and "description" not in out

    def test_a_short_description_is_the_whole_summary_and_none_is_empty(self):
        assert serialize_public_row(self.row(description_head="Short."), TODAY)["summary"] == "Short."
        assert serialize_public_row(self.row(description_head=None), TODAY)["summary"] == ""

    def test_the_placeholder_rules_still_apply(self):
        out = serialize_public_row(self.row(close_date=datetime(2076, 1, 1)), TODAY)
        assert out["award_ceiling"] is None and out["days_left"] is None and out["firm_deadline"] is False

    def test_a_row_holds_the_public_fields_and_the_detail_row_adds_the_description(self):
        assert set(serialize_public_row(self.row(), TODAY)) == set(PUBLIC_FIELDS)
        detail = serialize_public_row(self.row(description="The whole call."), TODAY, detail=True)
        assert set(detail) == set(PUBLIC_DETAIL_FIELDS) and detail["description"] == "The whole call."

    def test_a_column_that_should_not_be_there_does_not_get_through(self):
        out = serialize_public_row(self.row(relevance_score=5, relevance_reason="ours", matched_keywords="wind", watched=True, summary="the team's"), TODAY)
        assert "relevance_score" not in out and "matched_keywords" not in out and "watched" not in out
        assert out["summary"] != "the team's"


class TestPublicReads:
    def test_the_list_runs_the_public_statement_and_serializes_each_row(self, monkeypatch):
        row = {"opp_id": "A-1", "title": "Wind", "close_date": datetime(2026, 11, 6), "opportunity_status": "Posted", "description_head": "Words."}
        db = patched(monkeypatch, (None, [row]))
        out = query_public_grants(GrantQuery(today=TODAY))
        assert db.executed[0][0] == build_public_sql(GrantQuery(today=TODAY))[0]
        assert out[0]["days_left"] == 7 and out[0]["summary"] == "Words." and "relevance_score" not in out[0]

    def test_the_count_is_the_listing_less_its_limit_and_order(self, monkeypatch):
        db = patched(monkeypatch, ((812,), []))
        query = GrantQuery(q="wind", closing_within=30, today=TODAY, limit=500)
        assert count_public_grants(query) == 812
        sql, params = db.executed[0]
        assert sql.startswith("SELECT COUNT(*) FROM grants g") and "LIMIT" not in sql and "ORDER BY" not in sql
        assert params == build_public_sql(query)[1][:-1]

    def test_one_grant_comes_with_the_whole_description(self, monkeypatch):
        row = {"opp_id": "A-1", "title": "Wind", "description_head": "Start.", "description": "Start. And the rest of the call."}
        db = patched(monkeypatch, (row, []))
        out = get_public_grant(" A-1 ")
        assert db.executed[0][1] == ("A-1",) and "relevance" not in db.executed[0][0] and "JOIN" not in db.executed[0][0]
        assert out["description"] == "Start. And the rest of the call." and out["summary"] == "Start."

    def test_a_grant_that_is_not_there_is_none(self, monkeypatch):
        patched(monkeypatch, (None, []))
        assert get_public_grant("NOPE") is None

    def test_the_status_is_the_open_count_and_when_the_list_was_loaded_and_nothing_else(self, monkeypatch):
        db = patched(monkeypatch, ({"open": 1371, "last_seen_at": datetime(2026, 10, 30, 6, 0)}, []))
        assert public_status() == {"open": 1371, "data_as_of": "2026-10-30T06:00:00"}
        sql = db.executed[0][0]
        assert "relevance" not in sql and "grant_watchlist" not in sql and "g.summary" not in sql

    def test_an_empty_database_is_zero_open_and_no_date(self, monkeypatch):
        patched(monkeypatch, ({"open": 0, "last_seen_at": None}, []))
        assert public_status() == {"open": 0, "data_as_of": None}
