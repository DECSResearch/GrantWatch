"""Tests for the shared grant query builder (no database)."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from hypothesis import given, strategies as st

from grants import queries
from grants.queries import (
    NOT_SCORED_YET, GrantQuery, build_grants_sql, count_grants, data_status, fetch_member_state, get_grant, member_coverage, serialize_row,
)
from support import patched


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


OWN = "ana@und.edu"
OWN_SCORE = "CASE WHEN g.relevance_model = 'rule' THEN 0 ELSE s.score END"


class TestOwnScores:
    """A member's own scores in place of the team's: a join with member_scores, and the rule's 0 for a ruled-out grant."""

    def test_the_join_takes_the_members_address_before_any_filter(self):
        sql, params = build_grants_sql(GrantQuery(scores_for=OWN, min_score=4, q="x", today=date(2026, 9, 1)))
        assert "LEFT JOIN member_scores s ON s.opp_id = g.opp_id AND s.email = %s" in sql
        assert sql.index("LEFT JOIN member_scores") < sql.index("WHERE")
        assert params == [OWN, 4, "%x%", "%x%", "%x%", "%x%", 300]

    def test_the_score_reason_model_and_time_are_theirs_and_a_ruled_out_grant_keeps_the_rules_zero_and_reason(self):
        sql, _ = build_grants_sql(GrantQuery(scores_for=OWN))
        assert f"{OWN_SCORE} AS relevance_score" in sql
        assert "CASE WHEN g.relevance_model = 'rule' THEN g.relevance_reason WHEN s.score IS NULL THEN 'Not scored for your profile yet' ELSE s.reason END AS relevance_reason" in sql
        assert "CASE WHEN g.relevance_model = 'rule' THEN g.relevance_model ELSE s.model END AS relevance_model" in sql
        assert "CASE WHEN g.relevance_model = 'rule' THEN g.relevance_scored_at ELSE s.scored_at END AS relevance_scored_at" in sql
        assert NOT_SCORED_YET == "Not scored for your profile yet"
        assert not re.search(r"g\.relevance_score\b", sql) and "g.relevance_reason," not in sql  # nothing of the team's score is read as theirs

    def test_the_floor_and_the_order_use_their_score_and_tracking_and_their_own_relevant_marks_still_pass(self):
        sql, _ = build_grants_sql(GrantQuery(scores_for=OWN, min_score=4, team_relevant=("A-1",)))
        assert f"({OWN_SCORE} >= %s OR w.opp_id IS NOT NULL OR g.opp_id = ANY(%s))" in sql
        assert f"{OWN_SCORE} DESC NULLS LAST" in sql

    def test_without_an_address_nothing_changes(self):
        sql, params = build_grants_sql(GrantQuery(min_score=4))
        assert "member_scores" not in sql and "ORDER BY (g.close_date IS NULL), g.close_date, g.relevance_score DESC NULLS LAST" in sql
        assert params == [4, 300]

    @given(
        q=st.text(max_size=5), min_score=st.one_of(st.none(), st.integers(0, 5)), closing_within=st.one_of(st.none(), st.integers(1, 365)),
        watched_only=st.booleans(), stage=st.sampled_from([None, "full"]), team=st.lists(st.text(min_size=1, max_size=6), max_size=3).map(tuple),
        who=st.one_of(st.none(), st.just(OWN)), with_description=st.booleans(),
    )
    def test_every_placeholder_has_a_parameter_and_the_count_reads_the_same_statement(self, q, min_score, closing_within, watched_only, stage, team, who, with_description):
        query = GrantQuery(q=q, min_score=min_score, closing_within=closing_within, watched_only=watched_only, stage=stage, team_relevant=team,
                           scores_for=who, today=date(2026, 9, 1))
        sql, params = build_grants_sql(query, with_description=with_description)
        assert sql.count("%s") == len(params)
        assert ("member_scores" in sql) == bool(who)

    def test_the_count_joins_the_scores_the_floor_reads(self, monkeypatch):
        db = patched(monkeypatch, ((9,), []))
        query = GrantQuery(scores_for=OWN, min_score=4, today=date(2026, 9, 1), limit=500)
        assert count_grants(query) == 9
        sql, params = db.executed[0]
        assert "LEFT JOIN member_scores s" in sql and "LIMIT" not in sql and params == build_grants_sql(query)[1][:-1]

    def test_one_grant_for_a_member_reads_their_score(self, monkeypatch):
        db = patched(monkeypatch, ({"opp_id": "A-1", "relevance_score": 3, "relevance_reason": "theirs"}, []))
        out = get_grant(" A-1 ", scores_for=OWN)
        sql, params = db.executed[0]
        assert "LEFT JOIN member_scores s" in sql and params == [OWN, "A-1"]
        assert (out["relevance_score"], out["relevance_reason"]) == (3, "theirs")

    def test_one_grant_for_the_team_is_read_as_before(self, monkeypatch):
        db = patched(monkeypatch, ({"opp_id": "A-1"}, []))
        get_grant("A-1")
        sql, params = db.executed[0]
        assert "member_scores" not in sql and params == ["A-1"]


class TestFitCountByTheirScores:
    def test_it_counts_programs_by_the_members_own_score(self, monkeypatch):
        status = {"total": 5, "open": 4, "scored": 4, "watched": 0, "last_seen_at": None}
        fit = [{"opp_id": "A", "title": "One", "agency_code": "X", "close_date": "2026-11-06", "post_date": "2026-08-01", "opportunity_status": "Posted", "relevance_score": 5, "watched": False}]
        db = patched(monkeypatch, (status, []), (None, fit))
        assert data_status(4, scores_for=OWN)["fit_count"] == 1
        sql, params = db.executed[1]
        assert f"{OWN_SCORE} >= %s" in sql and "LEFT JOIN member_scores s" in sql and params == (OWN, 4)
        assert "member_scores" not in db.executed[0][0]  # the open and scored counts stay the team's

    def test_for_the_team_it_is_as_before(self, monkeypatch):
        db = patched(monkeypatch, ({"total": 1, "open": 1, "scored": 1, "watched": 0, "last_seen_at": None}, []), (None, []))
        data_status(4)
        sql, params = db.executed[1]
        assert "g.relevance_score >= %s" in sql and "member_scores" not in sql and params == (4,)


class TestMemberState:
    def test_a_member_with_a_profile_and_scores(self, monkeypatch):
        patched(monkeypatch, (("I work on batteries.", datetime(2026, 10, 30, 8, 0), True), []))
        assert fetch_member_state(OWN) == {"has_profile": True, "summary": "I work on batteries.", "updated_at": datetime(2026, 10, 30, 8, 0), "has_scores": True}

    def test_one_with_a_profile_and_no_scores_yet(self, monkeypatch):
        patched(monkeypatch, (("I work on batteries.", datetime(2026, 10, 30, 8, 0), False), []))
        assert fetch_member_state(OWN)["has_scores"] is False

    def test_one_on_the_team_profile_has_none_of_it(self, monkeypatch):
        db = patched(monkeypatch, (None, []))
        assert fetch_member_state(OWN) == {"has_profile": False, "summary": None, "updated_at": None, "has_scores": False}
        sql, params = db.executed[0]
        assert "EXISTS (SELECT 1 FROM member_scores s WHERE s.email = p.email)" in sql and "score_key" not in sql  # any score counts, whatever its key
        assert params == (OWN,)


class TestMemberCoverage:
    def test_it_counts_open_grants_ruled_out_or_scored_under_the_key(self, monkeypatch):
        db = patched(monkeypatch, ((1371, 412), []))
        assert member_coverage(OWN, "abc:p1") == {"open": 1371, "scored": 412}
        sql, params = db.executed[0]
        assert "g.relevance_model = 'rule' OR s.score_key = %s" in sql and "LEFT JOIN member_scores s ON s.opp_id = g.opp_id AND s.email = %s" in sql
        assert "CURRENT_DATE" in sql  # open grants only
        assert params == ("abc:p1", OWN)

    def test_a_profile_that_can_never_be_scored_has_no_key_and_only_the_ruled_out_count(self, monkeypatch):
        db = patched(monkeypatch, ((10, 2), []))
        assert member_coverage(OWN, None) == {"open": 10, "scored": 2}
        assert db.executed[0][1] == (None, OWN)
