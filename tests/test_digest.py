"""Tests for rendering a plan as an email."""
from __future__ import annotations

from datetime import date, timedelta

from notifications.digest import render_email
from notifications.planner import GrantState, NotifySettings, Recipient, plan_email
from notifications.transport import build_message

TODAY = date(2026, 9, 14)  # a Monday
ME = Recipient(id=1, email="me@und.edu", unsubscribe_token="tok123")


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
        assert "Grants below 4/5 are left out unless tracked" in email.text

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

    def test_backlog_rolls_over(self):
        grants = [_grant(f"N{i:02d}", 100 + i, score=5) for i in range(30)]
        email = _email(grants, NotifySettings(max_new=25, overview_day="never"))
        assert email.text.count("- [5/5]") == 25
        assert "5 more grants qualify and will follow in the next emails." in email.text
        assert len([n for n in email.shown if n.kind == "new"]) == 25

    def test_forecasted_marked_estimated(self):
        email = _email([_grant("F", 10, score=4, forecasted=True)])
        assert "10 days left, estimated" in email.text

    def test_without_scores_keyword_matches_are_announced(self):
        grants = [_grant("FEW", 3, score=None, matches=1), _grant("MANY", 20, score=None, matches=3)]
        email = _email(grants)
        assert "New grants that fit (2)" in email.text
        assert email.text.index("Grant MANY") < email.text.index("Grant FEW")  # most keyword hits first
        assert "match the research keywords" in email.text
        assert email.subject == "GrantWatch: 2 new"

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
