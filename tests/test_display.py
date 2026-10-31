"""Rules for showing Grants.gov data without passing off placeholders as facts."""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from hypothesis import given, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grants.display import (  # noqa: E402
    COUNTDOWN_DAYS,
    FIRM_DEADLINE_DAYS,
    PLACEHOLDER_CEILING,
    base_title,
    days_until_deadline,
    firm_deadline,
    group_rows,
    real_ceiling,
    trailing_acronym,
)
from grants.queries import serialize_row  # noqa: E402
from notifications.digest import render_email  # noqa: E402
from notifications.planner import GrantState, NotifySettings, Recipient, plan_email  # noqa: E402
from web import app as webapp  # noqa: E402

TODAY = date(2026, 9, 14)
ME = Recipient(id=1, email="me@und.edu", unsubscribe_token="tok123")
TWO_YEARS = 730  # days; the rule is written out here, not read from the code under test


def _email(*grants, settings=None):
    settings = settings or NotifySettings()
    return render_email(plan_email(ME, grants, [], TODAY, settings), settings, TODAY)


def _grant(opp, close, **kw):
    return GrantState(opp_id=opp, title=f"Grant {opp}", agency="NSF", url=f"https://x/{opp}",
                      close_date=close, score=kw.pop("score", 5), reason="fits", **kw)


class TestPlaceholderDeadlines:
    def test_boundary_is_five_years(self):
        assert days_until_deadline(TODAY + timedelta(days=FIRM_DEADLINE_DAYS), TODAY) == FIRM_DEADLINE_DAYS
        assert days_until_deadline(TODAY + timedelta(days=FIRM_DEADLINE_DAYS + 1), TODAY) is None
        assert firm_deadline(TODAY + timedelta(days=FIRM_DEADLINE_DAYS), TODAY)
        assert not firm_deadline(TODAY + timedelta(days=FIRM_DEADLINE_DAYS + 1), TODAY)

    def test_no_date_and_past_dates(self):
        assert days_until_deadline(None, TODAY) is None and not firm_deadline(None, TODAY)
        assert days_until_deadline(TODAY - timedelta(days=3), TODAY) == -3
        assert firm_deadline(TODAY - timedelta(days=3), TODAY)

    @given(st.integers(-5000, 40000))
    def test_a_day_count_never_comes_from_a_date_over_five_years_out(self, offset):
        days = days_until_deadline(TODAY + timedelta(days=offset), TODAY)
        assert (days is None) == (offset > FIRM_DEADLINE_DAYS)
        assert days in (None, offset)

    def test_grant_state_reads_the_same_rule(self):
        placeholder = _grant("P", date(2076, 10, 5))
        assert placeholder.days_left(TODAY) is None and not placeholder.firm_deadline(TODAY)
        real = _grant("R", TODAY + timedelta(days=9))
        assert real.days_left(TODAY) == 9 and real.firm_deadline(TODAY)
        assert not _grant("N", None).firm_deadline(TODAY)

    def test_email_says_no_firm_deadline_and_never_the_year(self):
        email = _email(_grant("P", date(2076, 10, 5)))
        assert "no firm deadline" in email.text
        assert "2076" not in email.text and "2076" not in email.html
        assert "days left" not in email.text

    def test_email_drops_the_day_count_past_a_year(self):
        far = TODAY + timedelta(days=COUNTDOWN_DAYS + 1)
        text = _email(_grant("F", far)).text
        assert f"closes {far.strftime('%b %d, %Y')}" in text and "days left" not in text
        near = TODAY + timedelta(days=COUNTDOWN_DAYS)
        assert "(365 days left)" in _email(_grant("N", near)).text

    @given(st.integers(0, 40000))
    def test_email_counts_days_only_for_a_real_deadline_within_a_year(self, offset):
        text = _email(_grant("G", TODAY + timedelta(days=offset))).text
        assert ("left)" in text) == (offset <= COUNTDOWN_DAYS)
        assert ("no firm deadline" in text) == (offset > FIRM_DEADLINE_DAYS)

    def test_placeholder_date_is_not_a_reminder_or_in_the_overview(self):
        tracked = _grant("T", date(2076, 10, 5), watched=True, score=None)
        scored = _grant("S", date(2076, 10, 5))
        plan = plan_email(ME, [tracked, scored], [], TODAY, NotifySettings(overview_day="daily"))
        assert plan.reminders == [] and plan.overview == []
        assert [n.grant.opp_id for n in plan.new] == ["S"]  # still announced, just without a deadline


class TestPlaceholderCeilings:
    def test_placeholder_and_missing(self):
        assert real_ceiling(None) is None
        assert real_ceiling(PLACEHOLDER_CEILING) is None
        assert real_ceiling(PLACEHOLDER_CEILING + 1) is None
        assert real_ceiling(PLACEHOLDER_CEILING - 1) == PLACEHOLDER_CEILING - 1
        assert real_ceiling(0) == 0

    @given(st.floats(min_value=0, max_value=1e12, allow_nan=False))
    def test_a_ceiling_at_or_over_the_placeholder_is_never_shown(self, value):
        shown = real_ceiling(value)
        assert shown is None or shown < PLACEHOLDER_CEILING
        assert (shown is None) == (value >= PLACEHOLDER_CEILING)


def row(opp_id, title, code, close, post=None, status="Posted", score=None, watched=False, agency=None):
    """An API row, shaped by serialize_row from what the database returns."""
    return serialize_row(
        {
            "opp_id": opp_id, "title": title, "agency_code": code, "agency": agency or code,
            "url": f"https://www.grants.gov/search-results-detail/{opp_id}",
            "close_date": datetime.fromisoformat(close) if close else None,
            "post_date": datetime.fromisoformat(post) if post else None,
            "opportunity_status": status, "relevance_score": score, "watched": watched,
        },
        today=TODAY,
    )


def shape(rows):
    """(lead, also posted, newer solicitation) of each row."""
    return [
        (r["opp_id"], [a["opp_id"] for a in r["also_posted"]], r["superseded_by"] and r["superseded_by"]["opp_id"])
        for r in rows
    ]


class TestTitles:
    def test_trailing_acronym(self):
        assert trailing_acronym("Instrumentation Program (DURIP)") == "DURIP"
        assert trailing_acronym("Instrumentation Program ( DURIP ) ") == "DURIP"
        assert trailing_acronym("Scholars (S-STEM)") == "S-STEM"
        assert trailing_acronym("Water Research (EFRI-WBC)") == "EFRI-WBC"
        for title in ["Librarian Program (2027)", "Program (FY27)", "Phase (II)", "Program (Pilot)", "Program (A)",
                      "Program (two words)", "Program (DURIP) and more", "(DURIP) Program", "Program", "", None]:
            assert trailing_acronym(title) is None, title

    def test_an_activity_code_is_not_a_program_acronym(self):
        for code in ["R01", "U01", "K01", "R21", "P30", "S-STEM2"]:
            assert trailing_acronym(f"Research Project Grant ({code})") is None, code

    def test_base_title_drops_one_trailing_parenthetical(self):
        assert base_title("  Expeditions   in Computing (CISE) ") == "expeditions in computing"
        assert base_title("Expeditions in Computing") == "expeditions in computing"
        assert base_title("Foo (a) (b)") == "foo (a)"
        assert base_title("(DURIP)") == "" and base_title(None) == ""


_WORDS = st.from_regex(r"[A-Z]{1,5}(-[A-Z]{1,5}){0,2}", fullmatch=True).filter(lambda t: len(t) > 1 and not set(t) <= set("IVX"))
_NUMBERED = st.from_regex(r"[A-Z0-9-]{2,8}", fullmatch=True).filter(lambda t: any(c.isdigit() for c in t))


class TestAcronymProperties:
    @given(_WORDS)
    def test_capitals_and_hyphens_make_an_acronym(self, token):
        assert trailing_acronym(f"Award ({token})") == token

    @given(_NUMBERED)
    def test_a_token_with_a_digit_never_does(self, token):
        assert trailing_acronym(f"Award ({token})") is None

    @given(_WORDS, _NUMBERED)
    def test_two_services_fold_on_an_acronym_and_never_on_a_numbered_code(self, acronym, code):
        def pair(tag):
            return group_rows([row("A", f"CDC Award ({tag})", "HHS-CDC", "2027-02-05"), row("B", f"FDA Award ({tag})", "HHS-FDA", "2027-02-05")])

        assert [len(r["also_posted"]) for r in pair(acronym)] == [1]
        assert [r["also_posted"] for r in pair(code)] == [[], []]


class TestSamePrograms:
    DURIP = "Fiscal Year 2027 Defense University Research Instrumentation Program (DURIP)"

    def triple(self, **scores):
        return [
            row("ARMY", self.DURIP, "DOD-AMC", "2026-11-06", score=scores.get("ARMY", 4), agency="Army Research Office"),
            row("ONR", self.DURIP, "DOD-ONR", "2026-11-06", score=scores.get("ONR", 4), agency="Office of Naval Research"),
            row("AFOSR", self.DURIP, "DOD-AFOSR", "2026-11-06", score=scores.get("AFOSR", 4), agency="Air Force Office of Scientific Research"),
        ]

    def test_one_program_from_three_services_is_one_row(self):
        out = group_rows(self.triple())
        assert shape(out) == [("AFOSR", ["ARMY", "ONR"], None)]  # tied scores: the lowest opp_id leads
        assert out[0]["also_posted"][0] == {
            "opp_id": "ARMY", "agency": "Army Research Office",
            "url": "https://www.grants.gov/search-results-detail/ARMY", "watched": False,
        }

    def test_the_best_scored_posting_leads_and_a_tracked_one_before_that(self):
        assert shape(group_rows(self.triple(ONR=5))) == [("ONR", ["AFOSR", "ARMY"], None)]
        rows = self.triple(ONR=5)
        rows[0] = row("ARMY", self.DURIP, "DOD-AMC", "2026-11-06", score=1, watched=True)
        out = group_rows(rows)
        assert shape(out) == [("ARMY", ["AFOSR", "ONR"], None)]
        assert out[0]["watched"] is True

    def test_a_group_row_is_its_lead_plus_two_keys(self):
        rows = self.triple(ONR=5)
        out = group_rows(rows)[0]
        assert {k: v for k, v in out.items() if k not in ("also_posted", "superseded_by")} == rows[1]

    def test_a_row_alone_has_empty_notes(self):
        out = group_rows([row("A", "Plain title", "NSF", "2026-11-06")])
        assert out[0]["also_posted"] == [] and out[0]["superseded_by"] is None

    def test_the_same_agency_is_never_folded(self):
        rows = [row(f"CESU{i}", "Cooperative Ecosystem Studies Unit (CESU)", "DOI-USGS1", "2026-11-14") for i in range(3)]
        assert shape(group_rows(rows)) == [("CESU0", [], None), ("CESU1", [], None), ("CESU2", [], None)]

    def test_one_repeated_agency_keeps_the_whole_bucket_apart(self):
        rows = self.triple() + [row("ARMY2", self.DURIP, "DOD-AMC", "2026-11-06")]
        assert [r["also_posted"] for r in group_rows(rows)] == [[], [], [], []]

    def test_different_close_dates_are_not_folded(self):
        rows = self.triple()
        rows[2] = row("AFOSR", self.DURIP, "DOD-AFOSR", "2026-11-07")
        assert shape(group_rows(rows)) == [("ARMY", ["ONR"], None), ("AFOSR", [], None)]

    def test_a_missing_agency_code_or_close_date_never_joins_a_group(self):
        rows = self.triple()
        rows[1] = row("ONR", self.DURIP, None, "2026-11-06")
        assert shape(group_rows(rows)) == [("ONR", [], None), ("AFOSR", ["ARMY"], None)]
        no_close = [row(i, self.DURIP, code, None) for i, code in [("A", "X-1"), ("B", "X-2")]]
        assert shape(group_rows(no_close)) == [("A", [], None), ("B", [], None)]

    def test_a_year_or_fiscal_year_tag_is_not_a_program(self):
        for tag in ["(2027)", "(FY27)", "(II)"]:
            rows = [row("A", f"Library Program {tag}", "IMLS", "2026-11-13"), row("B", f"Museum Program {tag}", "NEH", "2026-11-13")]
            assert [r["also_posted"] for r in group_rows(rows)] == [[], []], tag


class TestSupersededSolicitations:
    def pair(self, gap=TWO_YEARS, **newer):
        old_post = date(2020, 2, 10)
        fields = {"title": "Expeditions in Computing", "code": "NSF", "status": "Posted", **newer}
        return [
            row("20-544", "Expeditions in Computing", "NSF", "2026-12-01", post=old_post.isoformat(), score=3),
            row("26-525", fields["title"], fields["code"], "2027-01-12", post=(old_post + timedelta(days=gap)).isoformat(),
                status=fields["status"], score=4),
        ]

    def test_the_older_solicitation_is_annotated_not_hidden(self):
        out = group_rows(self.pair())
        assert shape(out) == [("20-544", [], "26-525"), ("26-525", [], None)]
        assert out[0]["superseded_by"] == {"opp_id": "26-525", "url": "https://www.grants.gov/search-results-detail/26-525"}

    def test_the_gap_is_two_years(self):
        assert group_rows(self.pair(gap=TWO_YEARS))[0]["superseded_by"] is not None
        assert group_rows(self.pair(gap=TWO_YEARS - 1))[0]["superseded_by"] is None

    def test_it_takes_the_same_agency_the_same_title_and_a_posted_notice(self):
        assert group_rows(self.pair(code="NIH"))[0]["superseded_by"] is None
        assert group_rows(self.pair(title="Expeditions in Engineering"))[0]["superseded_by"] is None
        assert group_rows(self.pair(status="Forecasted"))[0]["superseded_by"] is None
        assert group_rows(self.pair(title="EXPEDITIONS  in computing (CISE)"))[0]["superseded_by"] is not None

    def test_it_names_the_newest_replacement(self):
        rows = self.pair() + [row("30-001", "Expeditions in Computing", "NSF", "2031-01-01", post="2030-06-01")]
        out = group_rows(rows)
        assert [r["superseded_by"] and r["superseded_by"]["opp_id"] for r in out] == ["30-001", "30-001", None]

    def test_no_posting_date_means_no_note(self):
        rows = self.pair()
        rows[0] = row("20-544", "Expeditions in Computing", "NSF", "2026-12-01", post=None)
        assert group_rows(rows)[0]["superseded_by"] is None


class TestGolden:
    """A realistic mix, in Grants.gov's own words: only the DURIP and MURI triples fold."""

    DURIP = "Fiscal Year 2027 Defense University Research Instrumentation Program (DURIP)"
    MURI = "Fiscal Year (FY) 2027 Department of War (DoW) Multidisciplinary University Research Initiative (MURI)"
    PTI = "NIH Pathway to Independence Award (Parent K99/R00 Independent Basic Experimental Studies with Humans Required)"
    PTI2 = "NIH Pathway to Independence Award (Parent K99/R00 Independent Clinical Trial Required)"

    ROWS = [
        row("W911NF26S0095", DURIP, "DOD-AMC", "2026-11-06", post="2026-08-01", score=4),
        row("LB21-FY27", "Laura Bush 21st Century Librarian Program (2027)", "IMLS", "2026-11-13", post="2026-09-01", score=1),
        row("NAG-BASIC-FY27", "National Leadership Grants for Libraries (2027)", "IMLS", "2026-11-13", post="2026-09-01", score=1),
        row("N0001426SF003", DURIP, "DOD-ONR", "2026-11-06", post="2026-08-02", score=4),
        row("G27AS00079", "Cooperative Agreement with the Great Lakes-Northern Forest CESU (CESU)", "DOI-USGS1", "2026-11-14", post="2026-09-02", score=0),
        row("G27AS00077", "Cooperative Agreement with the Pacific Northwest CESU (CESU)", "DOI-USGS1", "2026-11-14", post="2026-09-02", score=0),
        row("NOFOAFRLAFOSR20260001", DURIP, "DOD-AFOSR", "2026-11-06", post="2026-08-03", score=4),
        row("20-544", "Expeditions in Computing", "NSF", "2026-12-01", post="2020-02-10", score=2),
        row("26-525", "Expeditions in Computing", "NSF", "2027-01-12", post="2026-06-15", score=4),
        row("PA-24-193", PTI, "HHS-NIH11", "2027-05-07", post="2024-04-24", score=1),
        row("PA-24-194", PTI2, "HHS-NIH11", "2027-05-07", post="2024-04-24", score=1),
        row("24-570", "Research Experiences for Undergraduates (REU)", "NSF", "2026-11-20", post="2026-07-01", score=3),
        row("NEH-REU", "Summer Research Program (REU)", "NEH", "2026-11-21", post="2026-07-01", score=1),
        row("N0001426SF002", MURI, "DOD-ONR", "2027-04-09", post="2026-08-02", score=3),
        row("W911NF26S1000", MURI, "DOD-AMC", "2027-04-09", post="2026-08-01", score=3),
        row("NOFOAFRLAFOSR20260002", MURI, "DOD-AFOSR", "2027-04-09", post="2026-08-03", score=2),
        # Two services, one day, one NIH activity code: two research grants, not one program posted twice.
        row("CDC-R01", "CDC Research Project Grant (R01)", "HHS-CDC", "2027-02-05", post="2026-09-01", score=2),
        row("FDA-R01", "FDA Research Project Grant (R01)", "HHS-FDA", "2027-02-05", post="2026-09-01", score=2),
    ]

    def test_only_the_two_triples_fold(self):
        assert shape(group_rows(self.ROWS)) == [
            ("LB21-FY27", [], None),
            ("NAG-BASIC-FY27", [], None),
            ("N0001426SF003", ["NOFOAFRLAFOSR20260001", "W911NF26S0095"], None),  # the tied 4s: the lowest opp_id leads
            ("G27AS00079", [], None),
            ("G27AS00077", [], None),
            ("20-544", [], "26-525"),  # annotated, still listed
            ("26-525", [], None),
            ("PA-24-193", [], None),
            ("PA-24-194", [], None),
            ("24-570", [], None),
            ("NEH-REU", [], None),
            ("N0001426SF002", ["NOFOAFRLAFOSR20260002", "W911NF26S1000"], None),
            ("CDC-R01", [], None),
            ("FDA-R01", [], None),
        ]

    def test_the_input_rows_are_not_changed(self):
        before = [dict(r) for r in self.ROWS]
        group_rows(self.ROWS)
        assert self.ROWS == before


class TestApi:
    def test_grants_come_back_grouped_with_the_existing_fields(self, monkeypatch):
        rows = TestGolden.ROWS
        monkeypatch.setattr(webapp, "query_grants", lambda query: list(rows))
        monkeypatch.setattr(webapp, "fetch_vote_counts", lambda opp_ids, email=None: {})
        body = TestClient(webapp.app, base_url="http://localhost").get("/api/grants").json()
        assert body["count"] == len(body["results"]) == len(rows) - 4
        durip = next(r for r in body["results"] if r["opp_id"] == "N0001426SF003")
        assert [a["opp_id"] for a in durip["also_posted"]] == ["NOFOAFRLAFOSR20260001", "W911NF26S0095"]
        assert durip["also_posted"][0]["url"].endswith("/NOFOAFRLAFOSR20260001")
        assert durip["url"].endswith("/N0001426SF003") and durip["title"] == TestGolden.DURIP
        assert {"days_left", "firm_deadline", "relevance_score", "watched", "close_date"} <= set(durip)
        old = next(r for r in body["results"] if r["opp_id"] == "20-544")
        assert old["superseded_by"]["opp_id"] == "26-525"


# Few codes, dates and titles, so postings often collide and the rules get exercised.
_CODES = st.sampled_from(["DOD-AMC", "DOD-ONR", "NSF", None])
_CLOSES = st.sampled_from(["2026-11-06", "2026-11-13", None])
_POSTS = st.sampled_from(["2020-03-01", "2022-03-01", "2024-03-01", "2026-03-01", "2026-09-01", None])
_STATUS = st.sampled_from(["Posted", "Posted", "Forecasted"])
_NAMES = st.sampled_from(["Instrumentation Program", "Expeditions in Computing"])


def _postings(acronyms):
    tags = st.sampled_from(acronyms)

    @st.composite
    def build(draw):
        rows = []
        for i in range(draw(st.integers(0, 12))):
            rows.append(row(
                f"G{i:02d}", draw(_NAMES) + draw(tags), draw(_CODES), draw(_CLOSES), post=draw(_POSTS),
                status=draw(_STATUS), score=draw(st.one_of(st.none(), st.integers(0, 5))), watched=draw(st.booleans()),
            ))
        return rows

    return build()


ANY = _postings(["", " (DURIP)", " (MURI)", " (CISE)", " (FY27)", " (R01)"])
NO_ACRONYMS = _postings(["", " (Pilot)", " (a pilot)"])


def members(r):
    return [r["opp_id"]] + [a["opp_id"] for a in r["also_posted"]]


class TestGroupingProperties:
    @given(ANY)
    def test_every_grant_appears_exactly_once(self, rows):
        out = group_rows(rows)
        assert sorted(i for r in out for i in members(r)) == sorted(r["opp_id"] for r in rows)

    @given(ANY)
    def test_never_groups_two_postings_of_one_agency(self, rows):
        by_id = {r["opp_id"]: r for r in rows}
        for r in group_rows(rows):
            codes = [by_id[i]["agency_code"] for i in members(r)]
            if len(codes) > 1:
                assert None not in codes and len(set(codes)) == len(codes)

    @given(ANY)
    def test_never_mixes_close_dates(self, rows):
        by_id = {r["opp_id"]: r for r in rows}
        for r in group_rows(rows):
            dates = {by_id[i]["close_date"] for i in members(r)}
            assert len(dates) == 1
            if len(members(r)) > 1:
                assert None not in dates

    @given(ANY)
    def test_a_group_shares_one_acronym(self, rows):
        by_id = {r["opp_id"]: r for r in rows}
        for r in group_rows(rows):
            if len(members(r)) > 1:
                assert len({trailing_acronym(by_id[i]["title"]) for i in members(r)}) == 1
                assert trailing_acronym(r["title"])

    @given(ANY)
    def test_a_group_is_never_built_on_a_token_with_a_digit(self, rows):
        for r in group_rows(rows):
            if len(members(r)) > 1:
                assert not any(c.isdigit() for c in trailing_acronym(r["title"]))

    @given(ANY, st.data())
    def test_the_result_does_not_depend_on_the_input_order(self, rows, data):
        shuffled = data.draw(st.permutations(rows))
        assert sorted(group_rows(rows), key=lambda r: r["opp_id"]) == sorted(group_rows(shuffled), key=lambda r: r["opp_id"])

    @given(ANY)
    def test_the_lead_is_unchanged_and_the_best_of_its_group(self, rows):
        by_id = {r["opp_id"]: r for r in rows}
        for r in group_rows(rows):
            assert {k: v for k, v in r.items() if k not in ("also_posted", "superseded_by")} == by_id[r["opp_id"]]
            key = lambda i: (not by_id[i]["watched"], -(by_id[i]["relevance_score"] if by_id[i]["relevance_score"] is not None else -1), i)
            assert key(r["opp_id"]) == min(key(i) for i in members(r))

    @given(ANY)
    def test_rows_keep_the_input_order(self, rows):
        position = {r["opp_id"]: n for n, r in enumerate(rows)}
        leads = [position[r["opp_id"]] for r in group_rows(rows)]
        assert leads == sorted(leads)


class TestSupersededProperties:
    @given(NO_ACRONYMS)
    def test_a_superseded_grant_is_only_ever_annotated(self, rows):
        out = group_rows(rows)
        assert [r["opp_id"] for r in out] == [r["opp_id"] for r in rows]  # nothing hidden, nothing reordered

    @given(NO_ACRONYMS)
    def test_annotated_exactly_when_a_posting_two_years_newer_replaces_it(self, rows):
        for r, original in zip(group_rows(rows), rows):
            replacements = [
                o for o in rows
                if original["agency_code"] and o["agency_code"] == original["agency_code"]
                and original["opportunity_status"] == o["opportunity_status"] == "Posted"
                and original["post_date"] and o["post_date"]
                and base_title(o["title"]) == base_title(original["title"])
                and (date.fromisoformat(o["post_date"][:10]) - date.fromisoformat(original["post_date"][:10])).days >= TWO_YEARS
            ]
            assert (r["superseded_by"] is not None) == bool(replacements)
            if replacements:
                assert r["superseded_by"]["opp_id"] in {o["opp_id"] for o in replacements}
