"""The dashboard (#35, #38, #39): its closing window and counts, and what its page does.

No database and no browser. The SQL runs against a recording fake, the routes
against canned rows, and the page is checked as text; its script is also run
through ``node --check`` when node is installed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grants import queries, sql_utils  # noqa: E402
from grants.display import days_until_deadline  # noqa: E402
from grants.profile import Profile  # noqa: E402
from grants.queries import GrantQuery, build_grants_sql, count_grants, data_status  # noqa: E402
from web import app as webapp  # noqa: E402

INDEX = webapp.INDEX_HTML
ADMIN = webapp.ADMIN_HTML
TODAY = date(2026, 10, 30)


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.db.executed.append((sql, params))
        self.result = self.db.script[len(self.db.executed) - 1]

    def fetchone(self):
        return self.result[0]

    def fetchall(self):
        return self.result[1]


class FakeDb:
    """Answers the nth query with the nth (fetchone, fetchall) pair of ``script``."""

    def __init__(self, *script):
        self.executed, self.script = [], script

    def connection(self):
        db = self

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self, cursor_factory=None):
                return FakeCursor(db)

            def close(self):
                pass

        return Conn()


def patched(monkeypatch, *script):
    db = FakeDb(*script)
    monkeypatch.setattr(sql_utils, "get_connection", db.connection)
    return db


class TestClosingWindow:
    def test_a_grant_closing_exactly_n_days_out_is_within_n_days(self):
        sql, params = build_grants_sql(GrantQuery(closing_within=7, today=TODAY))
        assert "g.close_date <= %s" in sql and "CURRENT_DATE +" not in sql
        assert params[0] == date(2026, 11, 6) and (params[0] - TODAY).days == 7

    def test_with_no_window_a_grant_with_no_close_date_is_listed(self):
        sql, _ = build_grants_sql(GrantQuery())
        assert "close_date IS NOT NULL" not in sql and "g.close_date <=" not in sql
        assert "close_date IS NOT NULL" in build_grants_sql(GrantQuery(closing_within=30))[0]

    @given(today=st.dates(date(2020, 1, 1), date(2035, 12, 31)), n=st.integers(1, 1000), offset=st.integers(-3, 3))
    def test_the_boundary_is_the_one_the_groups_and_the_email_use(self, today, n, offset):
        # day n is in the window and day n+1 is out, as days_until_deadline(close) <= n reads
        close = today + timedelta(days=n + offset)
        _, params = build_grants_sql(GrantQuery(closing_within=n, today=today))
        assert (close <= params[0]) == (days_until_deadline(close, today) <= n)

    def test_the_upcoming_deadlines_tool_uses_the_same_boundary(self, monkeypatch):
        db = patched(monkeypatch, (None, []))
        sql_utils.fetch_upcoming(days=14)
        sql, params = db.executed[0]
        assert "g.close_date <= %s" in sql and "CURRENT_DATE +" not in sql
        assert params[0] == date.today() + timedelta(days=14)

    def test_counting_matches_the_listing_without_its_limit(self, monkeypatch):
        db = patched(monkeypatch, ((612,), []))
        query = GrantQuery(q="wind", min_score=4, closing_within=30, today=TODAY, limit=500)
        assert count_grants(query) == 612
        sql, params = db.executed[0]
        assert sql.startswith("SELECT COUNT(*) FROM grants g") and "LIMIT" not in sql and "ORDER BY" not in sql
        assert params == build_grants_sql(query)[1][:-1]  # the listing's parameters, less its LIMIT

    def test_counting_lets_through_the_postings_the_team_marked_relevant_too(self, monkeypatch):
        db = patched(monkeypatch, ((7,), []))
        assert count_grants(GrantQuery(min_score=4, team_relevant=("A-1",), today=TODAY, limit=500)) == 7
        sql, params = db.executed[0]
        assert "OR g.opp_id = ANY(%s))" in sql and params == [4, ["A-1"]]

    def test_one_day_serves_the_window_and_the_day_counts(self, monkeypatch):
        row = {"opp_id": "A", "close_date": datetime(2026, 11, 6), "opportunity_status": "Posted"}
        patched(monkeypatch, (None, [row]))
        out = queries.query_grants(GrantQuery(today=TODAY))
        assert out[0]["days_left"] == 7


class TestFitCount:
    def posting(self, opp, code, close="2026-11-06", title="Instrumentation Program (DURIP)"):
        return {"opp_id": opp, "title": title, "agency_code": code, "close_date": close, "post_date": "2026-08-01",
                "opportunity_status": "Posted", "relevance_score": 4, "watched": False}

    def status_row(self):
        return {"total": 1500, "open": 1464, "scored": 1460, "watched": 2, "last_seen_at": datetime(2026, 10, 30, 6, 0)}

    def test_it_counts_programs_as_the_dashboard_lists_them(self, monkeypatch):
        fit = [self.posting("A", "DOD-AMC"), self.posting("B", "DOD-ONR"), self.posting("C", "DOD-AFOSR"),
               self.posting("D", "NSF", title="Something else"), self.posting("E", "DOE", close="2026-12-01", title="Another")]
        db = patched(monkeypatch, (self.status_row(), []), (None, fit))
        status = data_status(4)
        assert status["fit_count"] == 3  # the DURIP triple is one row
        assert status["open"] == 1464 and status["scored"] == 1460 and status["watched"] == 2
        sql, params = db.executed[1]
        assert "g.relevance_score >= %s" in sql and "CURRENT_DATE" in sql and params == (4,)

    def test_without_a_threshold_there_is_no_count_and_no_second_query(self, monkeypatch):
        db = patched(monkeypatch, (self.status_row(), []))
        assert data_status()["fit_count"] is None and len(db.executed) == 1

    def test_the_profile_route_asks_for_the_team_threshold(self, monkeypatch):
        asked = []

        def status(min_score=None):
            asked.append(min_score)
            return {"total": 3, "open": 3, "scored": 3, "watched": 0, "fit_count": 2, "data_as_of": "2026-10-30T06:00:00"}

        monkeypatch.setattr(webapp, "_profile", lambda: Profile(summary="Cyber research.", min_score_to_notify=3))
        monkeypatch.setattr(webapp, "data_status", status)
        body = TestClient(webapp.app, base_url="http://localhost").get("/api/profile").json()
        assert asked == [3] and body["min_score"] == 3 and body["data"]["fit_count"] == 2


def listing(n):
    return [{"opp_id": f"G{i}", "title": f"Grant {i}", "agency_code": f"A{i}", "close_date": "2026-11-06",
             "post_date": "2026-08-01", "opportunity_status": "Posted", "relevance_score": 4, "watched": False} for i in range(n)]


class TestGrantsTotal:
    @pytest.fixture
    def api(self, monkeypatch):
        seen = {"counted": 0, "queries": []}
        monkeypatch.setattr(webapp, "fetch_vote_counts", lambda ids, email=None: {})

        def run(rows, matching, limit):
            def query(q):
                seen["queries"].append(q)
                return list(rows)

            def count(q):
                seen["counted"] += 1
                return matching

            monkeypatch.setattr(webapp, "query_grants", query)
            monkeypatch.setattr(webapp, "count_grants", count)
            return TestClient(webapp.app, base_url="http://localhost").get(f"/api/grants?limit={limit}").json()

        return run, seen

    def test_a_short_list_is_all_there_is_and_is_not_counted_again(self, api):
        run, seen = api
        body = run(listing(4), matching=99, limit=500)
        assert body["total"] == body["count"] == 4 and seen["counted"] == 0

    def test_a_full_list_reports_how_many_matched(self, api):
        run, seen = api
        body = run(listing(5), matching=612, limit=5)
        assert body["total"] == 612 and body["count"] == 5 and seen["counted"] == 1

    def test_one_day_is_fixed_for_the_whole_request(self, api):
        run, seen = api
        run(listing(1), matching=1, limit=5)
        assert seen["queries"][0].today == date.today()

    @given(n=st.integers(0, 12), limit=st.integers(1, 12), extra=st.integers(0, 50))
    def test_the_total_is_never_less_than_what_is_listed(self, n, limit, extra):
        rows = listing(min(n, limit))  # the query never returns more than its limit
        matching = len(rows) + extra if len(rows) == limit else None
        with patch.object(webapp, "fetch_vote_counts", lambda ids, email=None: {}), \
                patch.object(webapp, "query_grants", lambda q: list(rows)), patch.object(webapp, "count_grants", lambda q: matching):
            body = TestClient(webapp.app, base_url="http://localhost").get(f"/api/grants?limit={limit}").json()
        assert body["total"] >= len(rows) >= body["count"]
        assert body["total"] == (matching if matching is not None else len(rows))


def script():
    return INDEX.split("<script>")[1].split("</script>")[0]


def rule(css, selector):
    """The declarations of the first rule that starts with ``selector``."""
    return re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css).group(1)


STYLE = INDEX.split("<style>")[1].split("</style>")[0]


class TestPageScript:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
    def test_the_script_parses(self, tmp_path):
        for name, text in (("index.js", script()), ("admin.js", ADMIN.split("<script>")[1].split("</script>")[0])):
            path = tmp_path / name
            path.write_text(text, encoding="utf-8")
            done = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
            assert done.returncode == 0, done.stderr


class TestFilters:
    def test_the_closing_window_starts_at_any_date(self):
        js = script()
        assert 'const windows = [{ value: "", label: "Any date" }];' in js
        assert 'fillSelect(els.closingWithin, windows, "");' in js
        assert "state.horizons[state.horizons.length - 1]);" not in js.split("function setupFilters")[1].split("async function loadProfile")[0]

    def test_reset_filters_goes_back_to_any_date_and_the_team_threshold(self):
        assert ">Reset filters</button>" in INDEX and "Clear filters" not in INDEX
        assert 'els.clear.addEventListener("click", () => { setupFilters(); loadGrants(); });' in script()
        setup = script().split("function setupFilters() {")[1].split("\n  }\n")[0]
        assert "p.min_score ?? 4" in setup and "[0, 3, 4, 5, threshold]" in setup and "fillSelect(els.minScore, scoreOptions, threshold);" in setup
        assert "els.showHidden.checked = false" in setup and "els.watchedOnly.checked = false" in setup

    def test_the_status_line_says_how_many_score_at_the_team_threshold(self):
        js = script()
        assert "d.fit_count" in js and "score ${p.min_score} or higher." in js

    def test_a_full_list_says_how_many_rows_are_drawn_not_how_many_were_fetched(self):
        js = script()
        assert "const LIMIT = 500;" in js and 'q.set("limit", String(LIMIT));' in js
        assert "els.count.textContent = countLine(shown.length, state.total);" in js  # shown: folded into programs, Not for us left out
        assert "Showing ${LIMIT}" not in js and "of ${total} postings." in js  # the total counts postings

    @staticmethod
    def count_lines(pairs):
        """What the page's own ``countLine`` says for each (rows drawn, postings matched), run under node."""
        js = script()
        parts = [re.search(pattern, js, re.S).group(0) for pattern in (r"const LIMIT = 500;", r"const plural = [^\n]*", r"const countLine = .*?;\n")]
        program = "\n".join(parts) + f"\nconsole.log(JSON.stringify({json.dumps([list(pair) for pair in pairs])}.map(([d, t]) => countLine(d, t))));"
        done = subprocess.run(["node", "-e", program], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    @pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
    def test_the_line_over_the_list(self):
        short, cut, one = self.count_lines([(12, 12), (212, 640), (1, 501)])
        assert short == "12 grants"
        assert cut == "Showing 212 grants from the first 500 of 640 postings. Narrow the filters to see the rest."
        assert one.startswith("Showing 1 grant from the first 500 of 501 postings.")
        assert self.count_lines([(500, 500), (0, 0)]) == ["500 grants", "0 grants"]  # the limit is not a cut until something is left over

    @pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
    @settings(max_examples=15, deadline=None)  # one node run per example, each over a batch
    @given(st.lists(st.integers(0, 700).flatmap(lambda total: st.tuples(st.integers(0, min(total, 500)), st.just(total))), min_size=1, max_size=40))
    def test_the_line_names_the_rows_drawn_and_only_a_cut_list_names_the_postings(self, pairs):
        for (drawn, total), line in zip(pairs, self.count_lines(pairs)):
            if total <= 500:
                assert line == f"{drawn} grant{'' if drawn == 1 else 's'}"
            else:  # cut short: the rows drawn (never the 500 postings fetched), out of the postings that matched
                assert line.startswith(f"Showing {drawn} grant") and f" of {total} postings. Narrow the filters" in line


class TestRows:
    def test_a_program_posted_by_several_services_names_them_with_links(self):
        js = script()
        assert "Also posted by: ${g.also_posted.map((a) => link(a.agency || a.opp_id, a.url)).join(\", \")}" in js
        assert 'const link = (label, url) => (url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(label)}</a>` : esc(label));' in js

    def test_an_older_solicitation_is_noted_not_hidden(self):
        assert "A newer solicitation exists: ${link(g.superseded_by.opp_id, g.superseded_by.url)}" in script()

    def test_a_placeholder_date_is_never_shown_as_a_date(self):
        js = script()
        assert "const placeholderDate = (g) => Boolean(g.close_date) && g.firm_deadline === false;" in js
        assert 'placeholderDate(g) ? "no firm deadline" : `closes ${fmtDate(g.close_date)}' in js
        assert '(placeholderDate(g) ? "no firm deadline" : fmtDate(g.close_date))' in js  # the details panel too
        assert js.count("fmtDate(g.close_date)") == 2
        assert 'none: "No firm deadline"' in js

    def test_a_missing_ceiling_is_left_out(self):
        assert "if (g.award_ceiling) meta.push(" in script()

    def test_notes_reuse_the_meta_style(self):
        assert script().count('<p class="meta">') >= 3  # the meta line, also posted, newer solicitation


class TestVoting:
    def test_each_row_has_relevant_and_not_for_us_buttons_that_say_which_is_pressed(self):
        js = script()
        assert 'class="vote ${verdict > 0 ? "yes" : "no"}" data-vote="${verdict}" aria-pressed="${g.my_verdict === verdict}"' in js
        assert '"Relevant" : "Not for us"' in js

    def test_a_vote_posts_and_the_same_button_takes_it_back(self):
        cast = script().split("async function castVote(button) {")[1].split("\n  }\n")[0]
        assert 'fetch(`/api/feedback/${encodeURIComponent(target)}`, { method: "DELETE" })' in cast
        assert 'fetch("/api/feedback", { method: "POST"' in cast and "JSON.stringify({ opp_id: target, verdict })" in cast
        assert "const clearing = g.my_verdict === verdict;" in cast
        assert "res.status === 401 || res.status === 503" in cast  # signed out: ask to sign in, change nothing

    def test_a_vote_goes_to_the_posting_that_holds_it_and_the_row_remembers_where_it_sits(self):
        cast = script().split("async function castVote(button) {")[1].split("\n  }\n")[0]
        assert "const target = g.my_vote_opp_id || g.opp_id;" in cast  # the lead when the person has no vote
        assert "g.opp_id" not in cast.split("const res = ")[1].split("if (res.status")[0]  # neither request names the lead
        assert "g.my_vote_opp_id = clearing ? g.opp_id : target;" in cast  # taken back: the next vote goes to the lead

    def test_the_row_stays_after_a_mark_so_it_can_be_taken_back(self):
        cast = script().split("async function castVote(button) {")[1].split("\n  }\n")[0]
        assert "render(" not in cast and "loadGrants(" not in cast

    def test_net_not_for_us_grants_are_hidden_until_asked_for(self):
        js = script()
        assert "state.grants.filter((g) => !g.hidden)" in js and "els.showHidden.checked ? state.grants" in js
        assert 'els.showHidden.addEventListener("change", render);' in js
        assert 'id="showHidden"' in INDEX and 'id="hiddenLabel" hidden' in INDEX
        assert "Show hidden (${n})" in js

    def test_the_strip_leaves_out_what_the_list_leaves_out(self):
        assert "const inRange = visible().filter(" in script()

    def test_signing_out_drops_the_marks_that_were_the_members(self):
        assert "loadGrants();  // the marks shown were the signed-in member's own" in script()


class TestSignInOff:
    def test_the_first_list_waits_for_the_session(self):
        js = script()
        assert "Promise.all([loadSession(), loadProfile()]).then(() => { renderControls(); return loadGrants(); });" in js
        assert js.index("Promise.all([loadSession()") > js.index("els.logout.addEventListener")
        assert "loadSession();\n  loadProfile().then(loadGrants);" not in js

    def test_track_and_the_marks_are_not_drawn(self):
        actions = script().split("function actionsHtml(g) {")[1].split("\n  }\n")[0]
        guard = actions.index('if (!signInOn()) return "";')
        assert guard < actions.index("data-track") and guard < actions.index("data-vote")

    def test_a_tracked_grant_shows_a_label_not_a_button(self):
        cell = script().split("function scoreCell(g) {")[1].split("\n  }\n")[0]
        assert '!signInOn() && g.watched ? `<span class="tracked-note">Tracked</span>`' in cell

    def test_only_tracked_shows_when_signing_in_works_or_something_is_tracked(self):
        controls = script().split("function renderControls() {")[1].split("\n  }\n")[0]
        assert "els.watchedLabel.hidden = !(signInOn() || tracked > 0);" in controls
        assert "state.profile.data.watched" in controls
        assert "els.trackingOff.hidden = signInOn();" in controls
        assert re.search(r'<p class="meta" id="trackingOff" hidden>[^<]*off on this server', INDEX)

    def test_the_hidden_attribute_wins_over_the_label_styles(self):
        assert "[hidden] { display: none !important; }" in STYLE
        assert ".filters label {" in STYLE  # the rule it has to beat


class TestAccessibility:
    def test_the_strip_is_a_labelled_group_not_an_image(self):
        assert 'id="strip" role="group" aria-label="Timeline of upcoming deadlines"' in INDEX
        assert 'role="img"' not in INDEX

    def test_a_jump_moves_focus_to_the_row_which_can_take_it(self):
        js = script()
        assert 'data-id="${esc(g.opp_id)}" tabindex="-1">' in js
        jump = js.split("function jumpTo(idx) {")[1].split("\n  }\n")[0]
        assert "row.focus({ preventScroll: true })" in jump

    def test_rows_titles_and_markers_are_named_by_position_not_by_opportunity_number(self):
        js = script()
        assert 'id="row-${i}" data-idx="${i}"' in js and 'id="title-${i}"' in js
        assert 'data-jump="${g.idx}"' in js and "document.getElementById(`row-${idx}`)" in js
        assert 'id="row-${esc(' not in js

    def test_every_repeated_button_and_link_names_its_grants_title(self):
        js = script()
        row = js.split("function rowHtml(g) {")[1].split("function render()")[0]
        assert row.count('aria-describedby="title-${i}"') == 1 and "${describe}" in row
        assert row.count("${describe}") == 2  # Details and Grants.gov
        actions = js.split("function actionsHtml(g) {")[1].split("\n  }\n")[0]
        assert actions.count("${describe}") == 2 and "${vote(1)}${vote(-1)}" in actions  # Track, and the one template for both marks

    def test_markers_have_a_24px_hit_box_around_the_6px_bar(self):
        marker = rule(STYLE, ".marker")
        assert "width: var(--hit)" in marker and "margin-left: calc(var(--hit) / -2)" in marker
        bar = rule(STYLE, ".marker::before")
        assert "width: 6px" in bar and "height: var(--bar)" in bar
        js = script()
        assert "const HIT = 24, BAR = 6, STEP = 8" in js
        assert "height:${Math.max(h, HIT)}px;--bar:${h}px;--hit:max(${BAR}px, min(${reach.join(\", \")}))" in js

    def test_markers_on_one_day_are_spread_and_a_crowd_at_the_edge_goes_left(self):
        js = script()
        assert "off: dir * k * STEP" in js and "? -1 : 1;" in js
        assert "inRange[end].days_left === day" in js

    def test_the_first_axis_label_goes_on_a_phone(self):
        phone = STYLE.split("@media (max-width: 640px) {")[1]
        assert ".strip .tick-label.first { display: none; }" in phone

    def test_the_search_is_full_width_on_a_phone_with_a_shorter_placeholder(self):
        phone = STYLE.split("@media (max-width: 640px) {")[1]
        assert ".filters .search { flex: 1 1 100%; }" in phone
        assert 'class="search"' in INDEX and 'placeholder="Title, agency, or number"' in INDEX
        assert "or opportunity number" not in INDEX

    def test_a_visible_grants_gov_link_sits_next_to_details(self):
        js = script()
        assert ">Grants.gov</a>" in js
        links = js.split('<div class="links">')[1].split("</div>")[0]
        assert links.index("Details") < links.index("${grantsGov}")


class TestControlBorders:
    @pytest.mark.parametrize("page,light,dark", [(INDEX, "--line: #858f8a;", "--line: #66736d;"), (ADMIN, "--line: #858f8a;", "--line: #66736d;")])
    def test_the_line_token_is_in_both_themes_of_both_pages(self, page, light, dark):
        root, night = page.split("<style>")[1].split("@media (prefers-color-scheme: dark) {")
        assert light in root and dark in night.split("}")[0]

    def test_the_line_token_has_the_3_to_1_contrast_the_field_boundary_needs(self):
        def luminance(hex_color):
            channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        def contrast(a, b):
            hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)

        assert contrast("#858f8a", "#ffffff") >= 3 and contrast("#858f8a", "#f7f8f6") >= 3  # light: field and page
        assert contrast("#66736d", "#16201b") >= 3 and contrast("#66736d", "#0f1613") >= 3  # dark

    def test_fields_and_buttons_use_it_not_the_hairline(self):
        filters = STYLE.split(".filters input[type=\"search\"], .filters select {")[1].split("}")[0]
        assert "border: 1px solid var(--line)" in filters
        assert "border: 1px solid var(--line)" in rule(STYLE, ".account input")
        assert "border: 1px solid var(--line)" in rule(STYLE, ".vote")
        assert "border: 1px solid var(--line)" in rule(ADMIN.split("<style>")[1], ".toggle")
