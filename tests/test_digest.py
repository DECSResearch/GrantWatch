"""Tests for rendering a plan as an email."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from hypothesis import assume, given, strategies as st

from grants.profile import Profile
from notifications.digest import render_email
from notifications.notify import settings_from_profile
from notifications.planner import NEW, OVERVIEW, GrantState, NotifySettings, Recipient, SentRecord, plan_email
from notifications.transport import build_message

TODAY = date(2026, 9, 14)  # a Monday
ME = Recipient(id=1, email="me@und.edu", unsubscribe_token="tok123")
PLACEHOLDER = date(2076, 12, 31)  # what Grants.gov writes when it has no close date
FIVE_YEARS = 5 * 365  # days; the rule is written out here, not read from the code under test
COUNTDOWN = 365  # days; the longest day count the email gives


def _grant(opp, days, score=4, **kw):
    close = TODAY + timedelta(days=days) if days is not None else None
    return GrantState(opp_id=opp, title=f"Grant {opp}", agency="NSF", url=f"https://x/{opp}",
                      close_date=close, score=score, reason=f"why {opp}", **kw)


def _email(grants, settings=None, sent=(), recipient=ME, today=TODAY):
    settings = settings or NotifySettings()
    return render_email(plan_email(recipient, grants, sent, today, settings), settings, today)


class TestRender:
    def test_nothing_to_say(self):
        assert _email([_grant("LOW", 5, score=2)], NotifySettings(overview_day="never")) is None

    def test_sections_subject_and_footnote(self):
        grants = [
            _grant("FIT", 3, score=5),
            _grant("TRACKED", 6, score=None, watched=True),
            _grant("LOW", 2, score=1),
            _grant("PAST", -1, score=5),
        ]
        email = _email(grants)
        assert email.subject == "GrantWatch: 1 new, 1 deadline reminder"
        assert "New grants that fit (1)" in email.text and "Grant FIT" in email.text
        assert "Deadline reminders (1)" in email.text and "Grant TRACKED" in email.text
        assert "Grant LOW" not in email.text and "Grant PAST" not in email.text
        assert "(3 days left)" in email.text
        assert "Grants below 4/5 are left out unless tracked or marked Relevant by the team." in email.text

    def test_the_scored_footnote_names_each_way_a_low_score_gets_listed(self):
        marked = _grant("MARKED", 40, score=1, net_votes=1)
        email = _email([marked, _grant("LOW", 41, score=1)], NotifySettings(overview_day="never"))
        assert "Grant MARKED" in email.text and "Grant LOW" not in email.text  # the team's Relevant mark gets it in
        footnote = "Grants below 4/5 are left out unless tracked or marked Relevant by the team."
        assert footnote in email.text and footnote in email.html

    def test_the_keyword_footnote_gives_the_order_the_list_is_in(self):
        email = _email([_grant("FEW", 40, score=None, matches=1)], NotifySettings(overview_day="never"))
        order = "those closing within 30 days first, then most matches first"
        assert order in email.text and order in email.html
        assert "Set up relevance scoring to rank them by fit." in email.text

    def test_html_escapes_and_links(self):
        g = GrantState(opp_id="A", title="Cats & <Dogs>", url="https://x/A", close_date=TODAY + timedelta(days=9), score=5)
        email = _email([g], NotifySettings(dashboard_url="https://gw.example.edu"))
        assert "Cats &amp; &lt;Dogs&gt;" in email.html and "<Dogs>" not in email.html
        assert 'href="https://x/A"' in email.html
        assert email.unsubscribe_url == "https://gw.example.edu/unsubscribe?token=tok123"
        assert "Unsubscribe: https://gw.example.edu/unsubscribe?token=tok123" in email.text
        assert 'href="https://gw.example.edu"' in email.html

    def test_overview_buckets_do_not_overlap(self):
        grants = [_grant("D7", 7, score=5), _grant("D8", 8, score=5), _grant("D30", 30, score=5)]
        settings = NotifySettings(horizons=[7, 30])
        overview = dict(plan_email(ME, grants, [], TODAY, settings).overview)
        assert [g.opp_id for g in overview["Closes within 7 days"]] == ["D7"]
        assert [g.opp_id for g in overview["Closes in 8 to 30 days"]] == ["D8", "D30"]

    def test_overview_skips_grants_already_listed_above(self):
        email = _email([_grant("NEW", 10, score=5)], NotifySettings(horizons=[7, 30]))
        assert email.text.count("Grant NEW") == 1
        assert email.overview_shown is True  # delivered; recorded so it is not resent tomorrow
        # ... and the email says so instead of leaving the overview out without a word.
        assert "\nThis week's deadlines are all in the list above.\n" in email.text
        assert "This week&#x27;s deadlines are all in the list above." in email.html
        assert email.html.count("Grant NEW") == 1

    def test_overview_with_something_new_to_say_has_no_such_line(self):
        email = _email([_grant("NEW", 10, score=5), _grant("TRACKED", 40, score=None, watched=True)], NotifySettings(horizons=[7, 30, 60]))
        assert "Closes in 31 to 60 days (1)" in email.text
        assert "all in the list above" not in email.text

    def test_overview_with_no_deadlines_is_not_recorded(self):
        """An overview section was never rendered, so the admin page must not count it as sent."""
        email = _email([_grant("OPEN", None, score=5)])
        assert "Grant OPEN" in email.text and "deadlines" not in email.text
        assert email.overview_shown is False
        assert OVERVIEW not in [row.kind for row in email.records(ME.id, TODAY)]

    def test_rendered_overview_is_recorded(self):
        email = _email([_grant("NEW", 10, score=5)])
        assert [row.kind for row in email.records(ME.id, TODAY)] == [NEW, OVERVIEW]

    def test_backlog_rolls_over(self):
        grants = [_grant(f"N{i:02d}", 100 + i, score=5) for i in range(30)]
        email = _email(grants, NotifySettings(max_new=25, overview_day="never"))
        assert email.text.count("- [5/5]") == 25
        assert "5 more grants qualify and will follow in the next emails." in email.text
        # Subject, heading and footer count the same 25 grants.
        assert email.subject == "GrantWatch: 25 new"
        assert "New grants that fit (25 of 30)" in email.text
        assert "(25 of 30)" in email.html
        assert len([n for n in email.shown if n.kind == "new"]) == 25

    def test_forecasted_marked_estimated(self):
        email = _email([_grant("F", 10, score=4, forecasted=True)])
        assert "10 days left, estimated" in email.text

    def test_without_scores_keyword_matches_are_announced(self):
        grants = [_grant("FEW", 40, score=None, matches=1), _grant("MANY", 50, score=None, matches=3)]
        email = _email(grants)
        assert "New grants that fit (2)" in email.text
        assert email.text.index("Grant MANY") < email.text.index("Grant FEW")  # most keyword hits first
        assert "match the research keywords" in email.text
        assert email.subject == "GrantWatch: 2 new"

    def test_without_scores_a_grant_that_matched_nothing_is_not_announced(self):
        """A model that scores every open grant also loads ones that matched no keyword."""
        grants = [_grant("NONE", 3, score=None, matches=0), _grant("ONE", 4, score=None, matches=1)]
        email = _email(grants)
        assert "Grant ONE" in email.text and "Grant NONE" not in email.text
        assert _email(grants[:1], NotifySettings(overview_day="never")) is None

    def test_without_scores_the_keyword_threshold_is_the_floor_but_never_below_one(self):
        grants = [_grant("ONE", 3, score=None, matches=1), _grant("TWO", 4, score=None, matches=2)]
        email = _email(grants, NotifySettings(keyword_threshold=2))
        assert "Grant TWO" in email.text and "Grant ONE" not in email.text
        none_and_one = [_grant("NONE", 3, score=None, matches=0), _grant("ONE", 4, score=None, matches=1)]
        email = _email(none_and_one, NotifySettings(keyword_threshold=0))
        assert "Grant ONE" in email.text and "Grant NONE" not in email.text

    def test_the_profiles_keyword_threshold_reaches_the_settings(self):
        assert settings_from_profile(Profile(summary="s", keyword_threshold=3)).keyword_threshold == 3

    def test_trims_to_size_and_reports_only_what_fits(self):
        long = "Ünïcode ’quoted’ title " * 20
        grants = [GrantState(opp_id=f"G{i}", title=long + str(i), reason=long, url="https://x", score=5,
                             close_date=TODAY + timedelta(days=i % 90)) for i in range(400)]
        settings = NotifySettings(max_new=400, max_per_bucket=400, max_bytes=20_000)
        plan = plan_email(ME, grants, [], TODAY, settings)
        email = render_email(plan, settings, TODAY)
        assert email.size <= 20_000
        shown_new = [n.grant.opp_id for n in email.shown if n.kind == "new"]
        assert 0 < len(shown_new) < 400
        assert shown_new == [n.grant.opp_id for n in plan.new[: len(shown_new)]]  # best first, the rest roll over


class TestMessage:
    def test_headers_and_parts(self):
        email = _email([_grant("A", 9, score=5)], NotifySettings(dashboard_url="https://gw.example.edu"))
        message = build_message("GrantWatch <gw@und.edu>", "me@und.edu", email.subject, email.text, email.html, email.unsubscribe_url)
        assert message["List-Unsubscribe"] == "<https://gw.example.edu/unsubscribe?token=tok123>"
        assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert message["Message-ID"].endswith("@und.edu>")
        assert message.get_body(("plain",)).get_content().startswith("GrantWatch for Monday")
        assert "Grant A" in message.get_body(("html",)).get_content()
        assert b"quoted-printable" in message.as_bytes()


def _stamp(day):
    return day.strftime("%b %d, %Y")


def _moved(previous, now):
    """A tracked grant last announced as closing ``previous``, which now closes ``now``."""
    g = GrantState(opp_id="T", title="Grant T", agency="NSF", url="https://x/T", close_date=now, watched=True)
    sent = [SentRecord(ME.id, "T", NEW, -1, previous, 4)]
    return _email([g], NotifySettings(overview_day="never"), sent=sent)


def _archived(close):
    """A tracked grant that Grants.gov archived yesterday, before ``close``."""
    g = GrantState(opp_id="T", title="Grant T", agency="NSF", url="https://x/T", close_date=close,
                   archive_date=TODAY - timedelta(days=1), watched=True)
    return _email([g], NotifySettings(overview_day="never"))


def _no_year_2076(email):
    assert "2076" not in email.text and "2076" not in email.html


class TestMovedDeadlines:
    def test_both_dates_firm_say_from_and_to(self):
        email = _moved(date(2027, 2, 5), date(2026, 12, 29))
        assert "Deadlines that moved (1)" in email.text
        assert "NSF; deadline moved from Feb 05, 2027 to Dec 29, 2026; 106 days left" in email.text
        assert "deadline moved from Feb 05, 2027 to Dec 29, 2026" in email.html
        assert email.subject == "GrantWatch: 1 deadline change"

    def test_from_a_placeholder_the_email_only_says_when_it_closes_now(self):
        email = _moved(PLACEHOLDER, date(2026, 12, 29))
        assert "Deadlines that moved (1)" in email.text
        assert "NSF; now closes Dec 29, 2026; 106 days left" in email.text
        assert "now closes Dec 29, 2026" in email.html
        assert "deadline moved from" not in email.text and "deadline moved from" not in email.html
        _no_year_2076(email)

    def test_to_a_placeholder_the_email_says_there_is_no_firm_deadline_now(self):
        email = _moved(date(2027, 2, 5), PLACEHOLDER)
        assert "Deadlines that moved (1)" in email.text
        assert "NSF; deadline moved from Feb 05, 2027; no firm deadline now" in email.text
        assert "deadline moved from Feb 05, 2027; no firm deadline now" in email.html
        assert "days left" not in email.text
        _no_year_2076(email)

    def test_from_one_placeholder_to_another_there_is_nothing_to_say(self):
        assert _moved(PLACEHOLDER, date(2077, 1, 15)) is None

    def test_the_day_count_is_left_out_past_a_year(self):
        email = _moved(date(2026, 12, 29), TODAY + timedelta(days=COUNTDOWN + 1))
        assert "NSF; deadline moved from Dec 29, 2026 to " in email.text and "days left" not in email.text
        assert "days left" not in email.html
        email = _moved(date(2026, 12, 29), TODAY + timedelta(days=COUNTDOWN))
        assert f"; {COUNTDOWN} days left" in email.text and f"; {COUNTDOWN} days left" in email.html

    @given(new=st.integers(0, 40000), old=st.integers(-3000, 40000))
    def test_a_date_is_named_only_when_it_is_firm(self, new, old):
        assume(new != old)
        email = _moved(TODAY + timedelta(days=old), TODAY + timedelta(days=new))
        if min(old, new) > FIVE_YEARS:
            assert email is None  # two placeholders: no news
            return
        assert "Deadlines that moved (1)" in email.text
        for offset in (old, new):
            stamp = _stamp(TODAY + timedelta(days=offset))
            assert (stamp in email.text) == (offset <= FIVE_YEARS), (offset, email.text)
            assert (stamp in email.html) == (offset <= FIVE_YEARS), (offset, email.html)


class TestArchivedGrants:
    def test_a_firm_deadline_is_named(self):
        email = _archived(date(2026, 12, 29))
        assert "Tracked grants no longer open (1)" in email.text
        assert "NSF; archived on Sep 13, 2026, before its Dec 29, 2026 deadline" in email.text
        assert "archived on Sep 13, 2026, before its Dec 29, 2026 deadline" in email.html

    def test_a_placeholder_deadline_is_left_out(self):
        email = _archived(PLACEHOLDER)
        assert "Tracked grants no longer open (1)" in email.text
        assert "NSF; archived on Sep 13, 2026" in email.text and "before its" not in email.text
        assert "archived on Sep 13, 2026" in email.html and "before its" not in email.html
        _no_year_2076(email)

    def test_no_close_date_is_left_out_too(self):
        email = _archived(None)
        assert "NSF; archived on Sep 13, 2026" in email.text
        assert "before its" not in email.text and "no date" not in email.text

    @given(st.integers(0, 40000))
    def test_the_close_date_is_named_only_when_it_is_firm(self, offset):
        email = _archived(TODAY + timedelta(days=offset))
        assert "archived on Sep 13, 2026" in email.text
        stamp = _stamp(TODAY + timedelta(days=offset))
        firm = offset <= FIVE_YEARS
        assert ("before its" in email.text) == firm and ("before its" in email.html) == firm
        assert (stamp in email.text) == firm and (stamp in email.html) == firm


def _new_ids(email):
    return [n.grant.opp_id for n in email.shown if n.kind == NEW]


class TestOrder:
    def test_grants_closing_within_30_days_come_first_soonest_first_then_by_score(self):
        grants = [
            _grant("FAR5", 100, score=5),
            _grant("D20", 20, score=4),
            _grant("D7", 7, score=4),
            _grant("D31", 31, score=5),
            _grant("D30", 30, score=4),
            _grant("NONE5", None, score=5),
            _grant("FAR4", 200, score=4),
        ]
        email = _email(grants, NotifySettings(overview_day="never"))
        # 30 days is urgent, 31 is not; the rest go by score, then deadline (no deadline last).
        assert _new_ids(email) == ["D7", "D20", "D30", "D31", "FAR5", "NONE5", "FAR4"]
        assert [m.split("Grant ")[1] for m in email.text.splitlines() if m.startswith("- [")] == _new_ids(email)

    def test_a_placeholder_deadline_is_not_urgent(self):
        far = GrantState(opp_id="P", title="Grant P", close_date=date(2076, 10, 5), score=5)
        email = _email([far, _grant("SOON", 29, score=4)], NotifySettings(overview_day="never"))
        assert _new_ids(email) == ["SOON", "P"]

    def test_the_cap_keeps_the_urgent_grants_and_cuts_the_rest(self):
        grants = [_grant(f"U{i}", 2 + i, score=4) for i in range(2)] + [_grant(f"L{i}", 100 + i, score=5) for i in range(5)]
        email = _email(grants, NotifySettings(max_new=3, overview_day="never"))
        assert _new_ids(email) == ["U0", "U1", "L0"]
        assert "New grants that fit (3 of 7)" in email.text
        assert "4 more grants qualify" in email.text

    def test_more_urgent_grants_than_the_cap_never_show_a_later_one(self):
        grants = [_grant(f"U{i}", 2 + i, score=1 + i % 5) for i in range(6)] + [_grant("L", 90, score=5)]
        email = _email(grants, NotifySettings(max_new=4, overview_day="never", min_score=1))
        assert _new_ids(email) == ["U0", "U1", "U2", "U3"]

    def test_keyword_matches_use_the_same_split(self):
        grants = [
            _grant("MANY", 90, score=None, matches=5),
            _grant("SOON", 10, score=None, matches=1),
            _grant("SOONER", 3, score=None, matches=1),
            _grant("FEW", 60, score=None, matches=2),
        ]
        email = _email(grants, NotifySettings(overview_day="never"))
        assert _new_ids(email) == ["SOONER", "SOON", "MANY", "FEW"]


def _triple(score=(4, 4, 4), close=14, announced=(), **kw):
    """A joint call posted by three services, as Grants.gov lists DURIP."""
    return [
        GrantState(opp_id=f"DURIP-{i}", title="Defense University Research Instrumentation Program (DURIP)",
                   agency=f"Service {i}", agency_code=f"DOD-{i}", url=f"https://x/durip/{i}",
                   close_date=TODAY + timedelta(days=close), score=score[i], reason=f"fits {i}", **kw)
        for i in range(3)
    ]


class TestPrograms:
    def test_a_program_posted_by_three_services_is_one_row(self):
        email = _email(_triple((3, 5, 4)) + [_grant("OTHER", 50, score=5)], NotifySettings(overview_day="never"))
        assert email.subject == "GrantWatch: 2 new"
        assert "New grants that fit (2)" in email.text
        assert email.text.count("Defense University Research") == 1
        assert "Also posted by: Service 0 (https://x/durip/0), Service 2 (https://x/durip/2)" in email.text
        assert 'href="https://x/durip/0"' in email.html and "Also posted by:" in email.html
        assert "[5/5] Defense" in email.text  # the program's score is its best posting's

    def test_the_cap_counts_programs(self):
        grants = _triple() + [_grant(f"N{i}", 40 + i, score=5) for i in range(3)]
        email = _email(grants, NotifySettings(max_new=2, overview_day="never"))
        assert len(_new_ids(email)) == 2 and "DURIP-0" in _new_ids(email)
        assert "New grants that fit (2 of 4)" in email.text and "2 more grants qualify" in email.text

    def test_every_posting_is_recorded_when_the_row_is_shown(self):
        email = _email(_triple(), NotifySettings(overview_day="never"))
        rows = email.records(ME.id, TODAY)
        assert sorted(r.opp_id for r in rows if r.kind == NEW) == ["DURIP-0", "DURIP-1", "DURIP-2"]
        assert {r.ref_date for r in rows} == {TODAY + timedelta(days=14)}

    def test_one_posting_announced_earlier_announces_the_program(self):
        sent = [SentRecord(ME.id, "DURIP-1", NEW, -1, TODAY, 4)]
        assert _email(_triple(), NotifySettings(overview_day="never"), sent=sent) is None

    def test_a_tracked_posting_means_the_team_has_the_program(self):
        grants = _triple()
        grants[2] = replace(grants[2], watched=True)
        email = _email(grants, NotifySettings(overview_day="never"))
        assert not [n for n in email.shown if n.kind == NEW]

    def test_a_program_qualifies_on_its_best_score_and_is_ordered_by_it(self):
        grants = _triple((1, 2, 5)) + [_grant("FOUR", 100, score=4), _grant("FIVE", 101, score=5)]
        assert _new_ids(_email(grants, NotifySettings(overview_day="never"))) == ["DURIP-2", "FIVE", "FOUR"]
        assert _email(_triple((1, 2, 3)), NotifySettings(overview_day="never")) is None

    def test_a_program_shown_under_new_is_not_repeated_in_the_overview(self):
        email = _email(_triple(), NotifySettings(horizons=[30]))
        assert email.text.count("Defense University Research") == 1
        assert "This week's deadlines are all in the list above." in email.text

    def test_the_overview_lists_a_program_once(self):
        sent = [SentRecord(ME.id, "DURIP-0", NEW, -1, TODAY, 4)]
        email = _email(_triple(), NotifySettings(horizons=[30]), sent=sent)
        assert "Closes within 30 days (1)" in email.text
        assert email.text.count("Defense University Research") == 1
        assert email.subject == "GrantWatch: the week ahead, 1 deadline in the next 30 days"


class TestLayout:
    def test_summary_is_a_muted_line_above_the_reason(self):
        g = _grant("A", 40, score=4, summary="Open to public universities; up to $500k.")
        email = _email([g], NotifySettings(overview_day="never"))
        lines = [line.strip() for line in email.text.splitlines()]
        assert lines.index("Open to public universities; up to $500k.") + 1 == lines.index("why A")
        assert email.html.index("Open to public universities") < email.html.index("why A")
        assert 'color:#5b6660">Open to public universities' in email.html

    def test_without_a_summary_only_the_reason_shows(self):
        email = _email([_grant("A", 40, score=4)], NotifySettings(overview_day="never"))
        assert "why A" in email.text and "why A" in email.html

    def test_without_a_reason_the_summary_still_shows(self):
        g = GrantState(opp_id="K", title="Grant K", close_date=TODAY + timedelta(days=40), summary="A keyword match.", matches=2)
        email = _email([g], NotifySettings(overview_day="never"))
        assert "A keyword match." in email.text and "A keyword match." in email.html

    def test_html_is_a_light_only_document(self):
        html = _email([_grant("A", 9, score=5)]).html
        assert html.startswith("<!doctype html><html")
        assert html.endswith("</body></html>")
        assert '<meta charset="utf-8">' in html
        assert '<meta name="color-scheme" content="light">' in html
        assert "color-scheme:light" in html and "<title>" in html
        assert html.count("<body") == 1 and html.count("</html>") == 1

    def test_reminder_marks_are_mentioned_only_when_the_recipient_tracks_grants(self, monkeypatch):
        tracked = _grant("T", 6, score=None, watched=True)
        for secret in ("", "s3cret"):
            monkeypatch.setenv("GRANTS_SECRET_KEY", secret)
            with_tracked = _email([_grant("A", 40, score=5), tracked])
            without = _email([_grant("A", 40, score=5)])
            assert "Tracked grants get a reminder at each of these marks" in with_tracked.text
            assert "Tracked grants get a reminder at each of these marks" in with_tracked.html
            assert "Tracked grants get a reminder" not in without.text
            assert "Tracked grants get a reminder" not in without.html
