"""Rules for showing Grants.gov data without passing off placeholders as facts."""
from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given, strategies as st

from grants.display import (
    COUNTDOWN_DAYS,
    FIRM_DEADLINE_DAYS,
    PLACEHOLDER_CEILING,
    days_until_deadline,
    firm_deadline,
    real_ceiling,
)
from notifications.digest import render_email
from notifications.planner import GrantState, NotifySettings, Recipient, plan_email

TODAY = date(2026, 9, 14)
ME = Recipient(id=1, email="me@und.edu", unsubscribe_token="tok123")


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
