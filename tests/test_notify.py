"""Examples of the notify rules, run through run_notify with an in-memory store."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from notifications.notify import run_notify
from notifications.planner import NotifySettings, Recipient, overview_due
from support import CaptureTransport, MemoryStore, sections
from test_digest import _grant

MONDAY = date(2026, 9, 14)
ME = Recipient(id=1, email="me@und.edu", unsubscribe_token="t1")
QUIET = NotifySettings(overview_day="never")


def _run(store, transport, day, settings=QUIET):
    return run_notify(store=store, transport=transport, today=day, settings=settings)


class TestAnnounceOnce:
    def test_grant_scored_after_insert_day_is_still_announced(self):
        # The bug this replaces: "new" meant "inserted this run", so a grant
        # loaded unscored (model down, per-run cap) was never announced.
        store, out = MemoryStore([ME], [_grant("A", 40, score=5), _grant("LATE", 40, score=None)]), CaptureTransport()
        assert _run(store, out, MONDAY) == 0
        assert sections(out.messages[-1])["New grants that fit"] == ["A"]
        store.grant_list[1] = replace(store.grant_list[1], score=5)
        assert _run(store, out, MONDAY + timedelta(days=1)) == 0
        assert sections(out.messages[-1])["New grants that fit"] == ["LATE"]

    def test_rerun_on_identical_data_sends_nothing(self):
        store, out = MemoryStore([ME], [_grant("A", 40, score=5)]), CaptureTransport()
        _run(store, out, MONDAY, NotifySettings())
        _run(store, out, MONDAY, NotifySettings())
        _run(store, out, MONDAY + timedelta(days=1), NotifySettings())
        assert len(out.messages) == 1

    def test_failed_send_is_retried_and_not_recorded(self):
        store, out = MemoryStore([ME], [_grant("A", 40, score=5)]), CaptureTransport()
        out.failing.add("me@und.edu")
        assert _run(store, out, MONDAY) == 1
        assert store.rows == []
        out.failing.clear()
        assert _run(store, out, MONDAY + timedelta(days=1)) == 0
        assert sections(out.messages[-1])["New grants that fit"] == ["A"]

    def test_program_posted_by_several_services_is_announced_once(self):
        postings = [replace(_grant(f"D{i}", 20, score=5), title=f"Grant D{i} (DURIP)", agency_code=f"DOD-{i}") for i in range(3)]
        store, out = MemoryStore([ME], postings), CaptureTransport()
        _run(store, out, MONDAY)
        assert sections(out.messages[-1])["New grants that fit"] == ["D0 (DURIP)"]  # one row for the three postings
        assert sorted(r.opp_id for r in store.rows if r.kind == "new") == ["D0", "D1", "D2"]
        store.grant_list.append(replace(postings[0], opp_id="D3", agency_code="DOD-3"))  # a fourth service joins later
        _run(store, out, MONDAY + timedelta(days=1))
        assert len(out.messages) == 1

    def test_per_recipient_threshold(self):
        picky = Recipient(id=2, email="picky@und.edu", min_score=5)
        store, out = MemoryStore([ME, picky], [_grant("FOUR", 40, score=4), _grant("FIVE", 40, score=5)]), CaptureTransport()
        _run(store, out, MONDAY)
        got = {m["To"]: sections(m)["New grants that fit"] for m in out.messages}
        assert got == {"me@und.edu": ["FIVE", "FOUR"], "picky@und.edu": ["FIVE"]}


class TestTrackedDeadlines:
    def test_one_reminder_per_mark_and_reset_when_the_date_moves(self):
        tracked = _grant("T", 20, score=None, watched=True)
        store, out = MemoryStore([ME], [tracked]), CaptureTransport()
        settings = replace(QUIET, reminder_days=[14, 7])
        days = {}
        for offset in range(0, 15):
            before = len(out.messages)
            _run(store, out, MONDAY + timedelta(days=offset), settings)
            if len(out.messages) > before:
                days[offset] = sections(out.messages[-1])
        # 20 days out: reminders at 14 days left (day 6) and 7 days left (day 13).
        assert sorted(days) == [6, 13]
        assert all(s == {"Deadline reminders": ["T"]} for s in days.values())

        moved = replace(tracked, close_date=tracked.close_date + timedelta(days=30))
        store.grant_list = [moved]
        _run(store, out, MONDAY + timedelta(days=15), settings)
        assert sections(out.messages[-1]) == {"Deadlines that moved": ["T"]}
        assert out.messages[-1]["Subject"] == "GrantWatch: 1 deadline change"
        assert "deadline moved from Oct 04, 2026 to Nov 03, 2026" in out.messages[-1].get_body(("plain",)).get_content()
        _run(store, out, MONDAY + timedelta(days=16), settings)
        assert len(out.messages) == 3  # announced once

    def test_withdrawn_tracked_grant_is_announced_once(self):
        store, out = MemoryStore([ME], [_grant("W", 50, score=None, watched=True, listed=False)]), CaptureTransport()
        _run(store, out, MONDAY)
        _run(store, out, MONDAY + timedelta(days=1))
        assert len(out.messages) == 1
        assert sections(out.messages[0]) == {"Tracked grants no longer open": ["W"]}
        assert "no longer listed on Grants.gov" in out.messages[0].get_body(("plain",)).get_content()

    def test_untracked_grants_get_no_reminders(self):
        store, out = MemoryStore([ME], [_grant("A", 5, score=5)]), CaptureTransport()
        _run(store, out, MONDAY)
        _run(store, out, MONDAY + timedelta(days=3))
        assert len(out.messages) == 1  # announced, then left to the weekly overview


class TestOverview:
    def test_schedule(self):
        tue = MONDAY + timedelta(days=1)
        assert overview_due(None, tue, "monday") is True  # first email ever
        assert overview_due(MONDAY, tue, "monday") is False
        assert overview_due(MONDAY, MONDAY + timedelta(days=7), "monday") is True
        assert overview_due(MONDAY - timedelta(days=8), tue, "monday") is True  # missed one; catch up
        assert overview_due(MONDAY, MONDAY, "daily") is False
        assert overview_due(MONDAY, tue, "daily") is True
        assert overview_due(None, MONDAY, "never") is False

    def test_weekly_overview_sent_once(self):
        store, out = MemoryStore([ME], [_grant("A", 20, score=5)]), CaptureTransport()
        for offset in range(0, 14):  # two Mondays
            _run(store, out, MONDAY + timedelta(days=offset), NotifySettings())
        subjects = [m["Subject"] for m in out.messages]
        assert subjects == ["GrantWatch: 1 new", "GrantWatch: the week ahead, 1 deadline in the next 90 days"]


class TestRunNotify:
    def test_no_transport_is_a_failure(self, monkeypatch):
        for name in ("GRANTS_EMAIL_TRANSPORT", "GRANTS_SMTP_HOST", "GMAIL_TOKEN_FILE", "GMAIL_TOKEN_JSON", "GRANTS_EMAIL_OUTBOX"):
            monkeypatch.delenv(name, raising=False)
        store = MemoryStore([ME], [_grant("A", 40, score=5)])
        assert run_notify(store=store, today=MONDAY, settings=QUIET) == 1

    def test_no_recipients_is_fine(self):
        assert run_notify(store=MemoryStore([], []), today=MONDAY, settings=QUIET) == 0

    def test_lock_held_elsewhere_skips(self):
        store, out = MemoryStore([ME], [_grant("A", 40, score=5)]), CaptureTransport()
        store.locked = True
        assert _run(store, out, MONDAY) == 0
        assert out.messages == [] and store.rows == []

    def test_dry_run_sends_and_records_nothing(self, capsys):
        store = MemoryStore([ME], [_grant("A", 40, score=5)])
        assert run_notify(store=store, today=MONDAY, settings=QUIET, dry_run=True) == 0
        assert "Subject: GrantWatch: 1 new" in capsys.readouterr().out
        assert store.rows == [] and store.locked is False

    def test_env_recipients_are_synced(self, monkeypatch):
        monkeypatch.setenv("GRANTS_NOTIFY_RECIPIENTS", "a@und.edu, b@und.edu")
        store = MemoryStore([], [])
        run_notify(store=store, today=MONDAY, settings=QUIET)
        assert store.synced == ["a@und.edu", "b@und.edu"]


class TestTestEmail:
    def test_sends_through_the_outbox(self, monkeypatch, tmp_path, capsys):
        import main

        for name in ("GRANTS_EMAIL_TRANSPORT", "GRANTS_SMTP_HOST", "GMAIL_TOKEN_FILE", "GMAIL_TOKEN_JSON"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("GRANTS_EMAIL_OUTBOX", str(tmp_path))
        monkeypatch.setenv("GRANTS_EMAIL_FROM", "GrantWatch <gw@example.org>")
        assert main.main(["test-email", "me@und.edu"]) == 0
        [sent] = list(tmp_path.glob("*.eml"))
        assert b"Subject: GrantWatch test email" in sent.read_bytes()
        assert "Handed to outbox" in capsys.readouterr().out

    def test_no_transport_is_explained(self, monkeypatch, capsys):
        import main

        for name in ("GRANTS_EMAIL_TRANSPORT", "GRANTS_SMTP_HOST", "GMAIL_TOKEN_FILE", "GMAIL_TOKEN_JSON", "GRANTS_EMAIL_OUTBOX"):
            monkeypatch.delenv(name, raising=False)
        assert main.main(["test-email", "me@und.edu"]) == 1
        assert "No email transport configured" in capsys.readouterr().out
