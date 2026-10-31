"""What a visitor who is not signed in may see of a grant (no database).

``public_row`` is an allow list, so these tests walk what it makes for any input and check that
every key is on the list, at every depth. Grouping is checked on its own terms: which posting
leads a program must not depend on a score or on tracking, which a visitor must not learn.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

from hypothesis import given, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grants.display import (  # noqa: E402
    PUBLIC_ALSO_FIELDS, PUBLIC_DETAIL_FIELDS, PUBLIC_FIELDS, PUBLIC_NEWER_FIELDS, group_rows, public_row, public_rows,
)
from grants.queries import serialize_row  # noqa: E402

TODAY = date(2026, 10, 30)
ALLOWED_KEYS = set(PUBLIC_DETAIL_FIELDS) | set(PUBLIC_ALSO_FIELDS) | set(PUBLIC_NEWER_FIELDS) | {"also_posted", "superseded_by"}
# Every key the API gives a member (and then some) that must never reach a visitor.
INTERNAL = (
    "relevance_score", "relevance_reason", "relevance_model", "relevance_scored_at", "relevance_profile", "matched_keywords",
    "watched", "watch_note", "my_verdict", "my_vote_opp_id", "votes_up", "votes_down", "team_verdict", "hidden",
    "first_seen_at", "last_seen_at", "fit_count", "scored", "summary_text", "institution", "min_score", "horizons",
)


def api_row(opp_id="A-1", **extra):
    """A member's row as ``serialize_row`` makes it, every internal field filled in."""
    source = {
        "opp_id": opp_id, "title": "Wind Resilience (WIND)", "stage": "full", "opportunity_status": "Posted",
        "opportunity_category": "D", "funding_categories": "Energy", "agency": "Department of Energy", "agency_code": "DOE",
        "url": f"https://www.grants.gov/{opp_id}", "post_date": date(2026, 9, 1), "close_date": datetime(2026, 11, 20),
        "archive_date": date(2026, 12, 20), "summary": "Funds wind research.", "matched_keywords": "wind; grid",
        "award_ceiling": 500000, "estimated_total_funding": 2_000_000, "relevance_score": 5, "relevance_reason": "squarely ours",
        "relevance_model": "m", "relevance_scored_at": datetime(2026, 10, 1), "first_seen_at": datetime(2026, 9, 2),
        "last_seen_at": datetime(2026, 10, 30), "watched": True, "watch_note": "Ana is writing it",
    }
    source.update(extra)
    return serialize_row(source, TODAY)


class TestPublicRow:
    def test_a_members_row_comes_out_as_grants_gov_facts_and_nothing_else(self):
        out = public_row(api_row())
        assert set(out) == set(PUBLIC_FIELDS)
        assert out["title"] == "Wind Resilience (WIND)" and out["days_left"] == 21 and out["firm_deadline"] is True
        assert out["summary"] == "Funds wind research." and out["award_ceiling"] == 500000
        assert not set(out) & set(INTERNAL)
        assert "ana" not in repr(out).lower() and "squarely" not in repr(out)

    def test_the_description_is_for_the_detail_view_only(self):
        row = api_row(description="Full text of the call.")
        assert "description" not in public_row(row)
        assert public_row(row, detail=True)["description"] == "Full text of the call."

    def test_a_grouped_row_keeps_the_others_postings_without_the_tracking_flag(self):
        row = {**api_row(), "also_posted": [{"opp_id": "B-1", "agency": "ONR", "url": "u", "watched": True, "note": "x"}],
               "superseded_by": {"opp_id": "C-1", "url": "v", "title": "x"}}
        out = public_row(row)
        assert out["also_posted"] == [{"opp_id": "B-1", "agency": "ONR", "url": "u"}]
        assert out["superseded_by"] == {"opp_id": "C-1", "url": "v"}
        assert public_row({**row, "superseded_by": None})["superseded_by"] is None

    def test_a_field_that_holds_a_structure_is_dropped_not_passed_through(self):
        out = public_row({"opp_id": "A", "title": {"relevance_score": 5}, "agency": ["watched"], "url": "u"})
        assert out == {"opp_id": "A", "url": "u"}

    def test_the_input_is_not_changed(self):
        row = {**api_row(), "also_posted": [{"opp_id": "B", "watched": True}], "superseded_by": None}
        before = repr(row)
        public_row(row)
        assert repr(row) == before


scalars = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.text(max_size=8))
junk = st.recursive(scalars, lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=6), inner, max_size=3)), max_leaves=8)
names = st.one_of(st.sampled_from([*PUBLIC_DETAIL_FIELDS, *INTERNAL, "also_posted", "superseded_by", "opp_id"]), st.text(max_size=6))


def keys_at_every_depth(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from keys_at_every_depth(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from keys_at_every_depth(inner)


text_or_none = st.one_of(st.none(), st.text(max_size=10))
iso_or_none = st.one_of(st.none(), st.dates(date(2020, 1, 1), date(2030, 12, 31)).map(date.isoformat))
# Rows as the database gives them to group_rows, with anything at all in the fields a visitor must not see.
typed_rows = st.fixed_dictionaries(
    {"opp_id": st.text(min_size=1, max_size=4)},
    optional={
        "title": text_or_none, "agency": text_or_none, "agency_code": text_or_none, "url": text_or_none,
        "close_date": iso_or_none, "post_date": iso_or_none, "opportunity_status": st.sampled_from(["Posted", "Forecasted", None]),
        **{name: junk for name in (*INTERNAL, "also_posted", "superseded_by")},
    },
)


class TestAllowListProperty:
    @given(row=st.dictionaries(names, junk, max_size=24), detail=st.booleans())
    def test_no_key_outside_the_list_survives_at_any_depth_for_any_input(self, row, detail):
        out = public_row(row, detail=detail)
        assert set(keys_at_every_depth(out)) <= ALLOWED_KEYS
        assert "description" not in out or detail

    @given(row=st.dictionaries(names, junk, max_size=24))
    def test_what_it_keeps_it_keeps_unchanged_and_it_is_idempotent(self, row):
        out = public_row(row)
        assert all(out[key] == row[key] for key in out if key in PUBLIC_FIELDS)
        assert public_row(out) == out

    @given(rows=st.lists(typed_rows, max_size=6, unique_by=lambda r: r["opp_id"]))
    def test_nor_does_anything_survive_grouping(self, rows):
        assert set(keys_at_every_depth(public_rows(rows))) <= ALLOWED_KEYS


@st.composite
def programs(draw):
    """One program posted by several services, and a lone grant: rows as the API makes them, with the internal fields blank."""
    count = draw(st.integers(1, 4))
    ids = draw(st.lists(st.text(alphabet="ABCDE-12", min_size=1, max_size=4), min_size=count + 1, max_size=count + 1, unique=True))
    codes = draw(st.permutations(["DOD-AMC", "DOD-ONR", "DOD-AFOSR", "NSF"]))
    rows = [api_row(opp, title="Instrumentation Program (DURIP)", agency_code=codes[i], agency=codes[i]) for i, opp in enumerate(ids[:count])]
    rows.append(api_row(ids[count], title="Something else", agency_code="DOE"))
    return rows


internal_values = st.fixed_dictionaries({
    "relevance_score": st.one_of(st.none(), st.integers(0, 5)), "watched": st.booleans(),
    "relevance_reason": st.text(max_size=12), "matched_keywords": st.text(max_size=12), "watch_note": st.one_of(st.none(), st.text(max_size=12)),
    "relevance_model": st.sampled_from(["m", "rule", None]),
})


class TestWhatTheTeamKnowsDoesNotShow:
    @given(rows=programs(), first=internal_values, second=internal_values)
    def test_changing_scores_tracking_reasons_or_keywords_changes_nothing_a_visitor_sees(self, rows, first, second):
        def with_internal(values):
            return [serialize_row({**r, **values, "close_date": datetime(2026, 11, 20), "post_date": date(2026, 9, 1)}, TODAY) for r in rows]

        assert public_rows(with_internal(first)) == public_rows(with_internal(second))

    @given(rows=programs(), scores=st.lists(st.one_of(st.none(), st.integers(0, 5)), min_size=5, max_size=5), tracked=st.lists(st.booleans(), min_size=5, max_size=5))
    def test_the_lead_posting_of_a_program_is_the_lowest_opportunity_number_whatever_it_scores_or_whoever_tracks_it(self, rows, scores, tracked):
        shaped = [{**row, "relevance_score": scores[i], "watched": tracked[i]} for i, row in enumerate(rows)]
        folded = [row for row in public_rows(shaped) if row["title"].startswith("Instrumentation")]
        durip = sorted(row["opp_id"] for row in rows if row["title"].startswith("Instrumentation"))
        assert [row["opp_id"] for row in folded] == durip[:1]
        assert sorted(a["opp_id"] for a in folded[0]["also_posted"]) == durip[1:]
        assert all(set(a) == set(PUBLIC_ALSO_FIELDS) for a in folded[0]["also_posted"])

    def test_the_lead_is_not_the_best_scored_posting_as_it_is_for_a_member(self):
        rows = [api_row("B-1", title="Instrumentation Program (DURIP)", agency_code="DOD-ONR", relevance_score=5),
                api_row("A-1", title="Instrumentation Program (DURIP)", agency_code="DOD-AMC", relevance_score=1, watched=False)]
        assert group_rows(rows)[0]["opp_id"] == "B-1"  # the member's list leads with the best score
        assert [r["opp_id"] for r in public_rows(rows)] == ["A-1"]  # the visitor's cannot

    def test_a_newer_solicitation_is_still_noted(self):
        old = api_row("EXP-2020", title="Expeditions in Computing", agency_code="NSF", post_date=date(2020, 9, 1), close_date=datetime(2026, 11, 20))
        new = api_row("EXP-2026", title="Expeditions in Computing", agency_code="NSF", post_date=date(2026, 9, 1), close_date=datetime(2026, 12, 20))
        out = {row["opp_id"]: row for row in public_rows([old, new])}
        assert out["EXP-2020"]["superseded_by"] == {"opp_id": "EXP-2026", "url": "https://www.grants.gov/EXP-2026"}
        assert out["EXP-2026"]["superseded_by"] is None
