"""The admin page: what it warns about, what it reports, who may open it, and what it serves (no database,
no network)."""
from __future__ import annotations

import base64
import json
import logging
import string
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grants import admin  # noqa: E402
from grants.profile import Profile  # noqa: E402
from llm_utils.relevance import safe_host  # noqa: E402
from web import app as webapp  # noqa: E402

HOUR = 3600
SECRET_VARS = [
    "GRANTS_LLM_API_KEY", "OPENAI_API_KEY", "POSTGRES_PASSWORD", "POSTGRES_URL", "GRANTS_SECRET_KEY",
    "GRANTS_ADMIN_PASSWORD", "GRANTS_SMTP_PASSWORD", "GMAIL_TOKEN_JSON", "GRANTS_LLM_BASE_URL", "OPENAI_BASE_URL",
]
# Everything else the admin page reads from the environment, so a developer's own setup never leaks into a test.
OTHER_VARS = [
    "OPENAI_MODEL", "GRANTS_LLM_MODEL", "GRANTS_LLM_BATCH_SIZE", "GRANTS_LLM_WORKERS", "GRANTS_LLM_TIMEOUT",
    "GRANTS_LLM_MAX_CHARS", "GRANTS_LLM_MAX_TOKENS", "GRANTS_LLM_EXTRA_BODY", "GRANTS_RELEVANCE_MAX_PER_RUN",
    "GRANTS_DATA_SOURCE", "GRANTS_GOV_LOOKBACK_DAYS", "GRANTS_EMAIL_MAX_NEW", "GRANTS_DASHBOARD_URL",
    "GRANTS_EMAIL_TRANSPORT", "GRANTS_SMTP_HOST", "GRANTS_SMTP_PORT", "GRANTS_SMTP_USER", "GRANTS_EMAIL_FROM",
    "GMAIL_SENDER_EMAIL", "GMAIL_TOKEN_FILE", "GRANTS_EMAIL_OUTBOX", "GRANTS_TIMEZONE",
]
SLOW = dict(max_examples=max(20, settings.default.max_examples // 5), deadline=None, suppress_health_check=[HealthCheck.too_slow])


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in SECRET_VARS + OTHER_VARS:
        monkeypatch.delenv(name, raising=False)


def _run(step="ingest", state="ok", detail=None, stats=None):
    return {"step": step, "state": state, "detail": detail, "stats": stats}


def _health(**changes):
    health = {"open_unscored": 0, "last_ok_ingest_age_seconds": 5 * HOUR, "last_ok_notify_age_seconds": 5 * HOUR,
              "data_age_seconds": 5 * HOUR}
    health.update(changes)
    return health


def _warnings(health=None, runs=None, recipients=2, scoring_on=True):
    return admin.warnings_for(health or _health(), [_run("notify"), _run()] if runs is None else runs, recipients, scoring_on)


class TestRunState:
    def test_finished_runs(self):
        assert admin.run_state(datetime(2026, 9, 14), True, 100) == "ok"
        assert admin.run_state(datetime(2026, 9, 14), False, 100) == "failed"

    def test_unfinished_runs_are_running_until_the_jobs_deadline_has_passed(self):
        assert admin.RUNNING_SECONDS == 95 * 60  # activeDeadlineSeconds 5400 plus five minutes
        assert admin.run_state(None, None, 60) == "running"
        assert admin.run_state(None, None, 95 * 60) == "running"

    def test_older_than_the_deadline_did_not_finish(self):
        assert admin.run_state(None, None, 95 * 60 + 1) == "did not finish"
        assert admin.run_state(None, None, 30 * HOUR) == "did not finish"

    def test_a_newer_run_of_the_step_means_the_older_one_did_not_finish(self):
        assert admin.run_state(None, None, 60, superseded=True) == "did not finish"
        assert admin.run_state(None, None, 60, superseded=False) == "running"

    def test_a_finished_run_is_never_superseded_into_something_else(self):
        assert admin.run_state(datetime(2026, 9, 14), True, 100, superseded=True) == "ok"
        assert admin.run_state(datetime(2026, 9, 14), False, 100, superseded=True) == "failed"

    def test_the_deadline_in_the_cronjob_is_what_the_cutoff_follows(self):
        cronjob = (Path(__file__).resolve().parents[1] / "k8s" / "cronjob-daily.yaml").read_text()
        assert "activeDeadlineSeconds: 5400" in cronjob


class TestWarnings:
    def test_all_clear(self):
        assert _warnings() == []

    def test_no_runs_at_all_is_one_sentence(self):
        health = _health(last_ok_ingest_age_seconds=None, last_ok_notify_age_seconds=None)
        assert _warnings(health, runs=[]) == ["No daily run has been recorded yet."]

    def test_an_ingest_old_but_outside_the_recent_list_is_not_called_unrecorded(self):
        found = _warnings(_health(last_ok_ingest_age_seconds=40 * HOUR), runs=[_run("notify")])
        assert len(found) == 1 and "40 hours ago" in found[0]

    def test_stale_ingest_at_thirty_hours(self):
        assert _warnings(_health(last_ok_ingest_age_seconds=30 * HOUR)) == []
        found = _warnings(_health(last_ok_ingest_age_seconds=30 * HOUR + 60))
        assert len(found) == 1 and "last successful ingest" in found[0]

    def test_no_successful_ingest_yet(self):
        found = _warnings(_health(last_ok_ingest_age_seconds=None), runs=[_run(state="failed")])
        assert any("No ingest has finished successfully" in w for w in found)

    def test_failed_newest_ingest_with_its_reason(self):
        runs = [_run(state="failed", detail="Summarizing descriptions failed."), _run()]
        found = _warnings(runs=runs)
        assert found == ["The newest ingest run failed. Summarizing descriptions failed."]

    def test_an_ingest_that_did_not_finish(self):
        found = _warnings(runs=[_run(state="did not finish")])
        assert found == ["The newest ingest run did not finish."]

    def test_a_running_ingest_is_not_a_problem(self):
        assert _warnings(runs=[_run(state="running"), _run()]) == []

    def test_only_the_newest_run_counts(self):
        assert _warnings(runs=[_run(), _run(state="failed")]) == []

    def test_a_failed_notify_is_not_hidden_by_a_missing_ingest(self):
        health = _health(last_ok_ingest_age_seconds=None, last_ok_notify_age_seconds=None)
        found = _warnings(health, runs=[_run("notify", "failed", "cannot take the send lock: down")])
        assert found == ["No daily run has been recorded yet.", "The newest notify run failed. cannot take the send lock: down"]

    def test_failed_newest_notify(self):
        runs = [_run("notify", "failed", "sent 0, failed 2, nothing new for 0 of 2 recipients"), _run()]
        assert _warnings(runs=runs) == ["The newest notify run failed. sent 0, failed 2, nothing new for 0 of 2 recipients"]

    def test_unscored_grants_only_matter_when_scoring_is_on(self):
        found = _warnings(_health(open_unscored=12))
        assert found == [
            "12 open grants have no relevance score yet; the model server may be unreachable, "
            "or the per-run cap left them for later."
        ]
        assert _warnings(_health(open_unscored=12), scoring_on=False) == []

    def test_one_unscored_grant(self):
        assert _warnings(_health(open_unscored=1))[0].startswith("1 open grant has no relevance score yet")

    def test_nobody_to_email(self):
        assert _warnings(recipients=0) == ["Nobody gets email yet: there are no active recipients."]

    def test_old_grants_data_names_its_age(self):
        assert _warnings(_health(data_age_seconds=30 * HOUR)) == []
        assert _warnings(_health(data_age_seconds=40 * HOUR)) == ["Grants.gov data was last updated 40 hours ago."]
        assert _warnings(_health(data_age_seconds=3 * 86400)) == ["Grants.gov data was last updated 3 days ago."]

    def test_no_data_age_is_not_a_warning(self):
        assert _warnings(_health(data_age_seconds=None)) == []
        assert _warnings({"last_ok_ingest_age_seconds": 5 * HOUR}) == []

    def test_an_ingest_that_kept_nothing_says_why(self):
        detail = "83,388 in the extract, 0 kept. No grants matched the profile keywords."
        found = _warnings(runs=[_run("notify"), _run(detail=detail, stats={"extract": 83388, "kept": 0})])
        assert found == [f"The newest ingest kept 0 grants. {detail}"]

    def test_an_ingest_that_kept_nothing_without_a_detail(self):
        assert _warnings(runs=[_run(stats={"kept": 0})]) == ["The newest ingest kept 0 grants."]

    def test_zero_kept_is_read_from_the_newest_ingest_that_worked(self):
        runs = [_run(state="failed", detail="Summarizing descriptions failed."), _run(stats={"kept": 0})]
        assert _warnings(runs=runs) == [
            "The newest ingest run failed. Summarizing descriptions failed.",
            "The newest ingest kept 0 grants.",
        ]

    def test_a_missing_count_is_not_zero(self):
        assert _warnings(runs=[_run(stats={"extract": 5})]) == []
        assert _warnings(runs=[_run(stats=None)]) == []
        assert _warnings(runs=[_run(stats=["not", "a", "dict"])]) == []

    def test_a_stopped_scorer_is_a_warning_even_though_the_run_worked(self):
        reason = "cannot reach http://10.226.31.254:18100: Connection error."
        found = _warnings(runs=[_run(stats={"kept": 399, "scoring_abort": reason}), _run()])
        assert found == [f"Scoring stopped during the newest ingest: {reason}"]

    def test_only_the_newest_ingest_can_have_stopped_scoring(self):
        runs = [_run(stats={"kept": 3}), _run(stats={"kept": 3, "scoring_abort": "authentication failed"})]
        assert _warnings(runs=runs) == []

    def test_everything_wrong_at_once_reads_in_a_steady_order(self):
        health = _health(last_ok_ingest_age_seconds=40 * HOUR, data_age_seconds=40 * HOUR, open_unscored=3)
        runs = [_run("notify", "failed", "down"), _run(state="failed", detail="boom", stats={"scoring_abort": "authentication failed"})]
        found = _warnings(health, runs, recipients=0)
        assert [w.split(" ")[0] for w in found] == ["The", "Grants.gov", "The", "The", "Scoring", "3", "Nobody"]


class FakeDb:
    """Answers admin_status's queries with canned rows and remembers the parameters."""

    def __init__(self, answers):
        self.answers = answers
        self.last = None
        self.params = {}

    def connection(self):
        db = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                db.last = sql
                db.params[sql] = params

            def fetchall(self):
                return db.answers[db.last]

            def fetchone(self):
                rows = db.answers[db.last]
                return rows[0] if rows else None

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self, cursor_factory=None):
                return Cursor()

        return Conn()


STARTED = datetime(2026, 9, 14, 12, 30, tzinfo=timezone.utc)


def _answers(token="tok-1"):
    return {
        admin._RUNS_SQL: [
            {"id": 12, "step": "notify", "started_at": STARTED, "finished_at": STARTED, "ok": True, "detail": "sent 1",
             "stats": {"sent": 1, "failed": 0, "quiet": 0, "recipients": 1}, "has_log": True, "age_seconds": 600.0,
             "duration_seconds": 12.0, "superseded": False, "log": "SECRET LOG TEXT"},
            {"id": 11, "step": "ingest", "started_at": STARTED, "finished_at": None, "ok": None, "detail": None,
             "stats": None, "has_log": False, "age_seconds": 3 * HOUR, "duration_seconds": None, "superseded": False},
            {"id": 9, "step": "ingest", "started_at": STARTED, "finished_at": None, "ok": None, "detail": None,
             "stats": None, "has_log": False, "age_seconds": 20 * 60.0, "duration_seconds": None, "superseded": True},
        ],
        admin._HEALTH_SQL: [{
            "data_as_of": STARTED, "data_age_seconds": 2 * HOUR, "open": 10, "open_scored": 8, "open_unscored": 2, "watched": 1,
            "last_ok_ingest_age_seconds": 2 * HOUR, "last_ok_notify_age_seconds": 600.0, "last_sent": STARTED,
        }],
        admin._RECIPIENTS_SQL: [
            {"email": "a@und.edu", "name": None, "min_score": None, "active": True, "unsubscribed_at": None,
             "last_emailed": STARTED, "unsubscribe_token": token},
            {"email": "b@und.edu", "name": "B", "min_score": 3, "active": False, "unsubscribed_at": STARTED,
             "last_emailed": None, "unsubscribe_token": token},
        ],
        admin._EMAILS_SQL: [
            {"email": "a@und.edu", "day": STARTED.date(), "new": 3, "reminder": 1, "changed": 0, "removed": 0, "overview": 1},
        ],
    }


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDb(_answers())
    monkeypatch.setattr(admin, "db_connection", db.connection)
    monkeypatch.setattr(admin, "_ensure_runs_table", lambda: None)
    monkeypatch.setattr(admin, "load_profile", lambda: Profile(summary="x"))
    return db


class TestAdminStatus:
    def test_shapes_the_rows_and_adds_state_and_warnings(self, fake_db):
        status = admin.admin_status()

        assert [r["state"] for r in status["runs"]] == ["ok", "did not finish", "did not finish"]
        assert status["runs"][0]["started_at"] == "2026-09-14T12:30:00+00:00"
        assert status["runs"][1]["finished_at"] is None
        assert status["health"]["data_as_of"] == "2026-09-14T12:30:00+00:00"
        assert status["health"]["scoring_on"] is True
        assert status["recipients"][1]["unsubscribed_at"] == "2026-09-14T12:30:00+00:00"
        assert status["emails"][0]["day"] == "2026-09-14"
        assert status["warnings"] == [
            "The newest ingest run did not finish.",
            "2 open grants have no relevance score yet; the model server may be unreachable, "
            "or the per-run cap left them for later.",
        ]
        assert [w for w in status["warnings"] if "recipients" in w] == []  # one active recipient

    def test_a_run_replaced_by_a_newer_one_did_not_finish_however_young(self, fake_db):
        assert admin.admin_status()["runs"][2]["state"] == "did not finish"

    def test_runs_carry_their_numbers_and_whether_there_is_a_log_but_never_the_log(self, fake_db):
        runs = admin.admin_status()["runs"]
        assert runs[0]["id"] == 12 and runs[0]["has_log"] is True
        assert runs[0]["stats"] == {"sent": 1, "failed": 0, "quiet": 0, "recipients": 1}
        assert runs[1]["stats"] is None and runs[1]["has_log"] is False
        assert "superseded" not in runs[0]
        assert "SECRET LOG TEXT" not in json.dumps(runs)
        assert set(runs[0]) == {"id", "step", "started_at", "finished_at", "ok", "detail", "stats", "has_log",
                                "age_seconds", "duration_seconds", "state"}

    def test_the_runs_query_covers_fifteen_days_capped_at_200_and_leaves_the_log_out(self):
        sql = " ".join(admin._RUNS_SQL.split())
        assert "INTERVAL '15 days'" in sql and "LIMIT 200" in sql
        assert "r.log IS NOT NULL" in sql and "SELECT r.id" in sql and "r.log," not in sql
        assert "EXISTS (SELECT 1 FROM job_runs n WHERE n.step = r.step AND n.started_at > r.started_at) AS superseded" in sql

    def test_recipients_have_their_own_threshold_and_never_a_token(self, fake_db):
        recipients = admin.admin_status()["recipients"]
        assert [r["min_score"] for r in recipients] == [None, 3]
        assert "unsubscribe_token" not in json.dumps(recipients) and "tok-1" not in json.dumps(admin.admin_status())
        assert "unsubscribe_token" not in admin._RECIPIENTS_SQL

    def test_the_last_send_moves_from_the_health_numbers_into_the_email_block(self, fake_db, monkeypatch):
        monkeypatch.setenv("GRANTS_SMTP_HOST", "smtp.example.org")
        monkeypatch.setenv("GRANTS_EMAIL_FROM", "GrantWatch <alerts@und.edu>")
        status = admin.admin_status()
        assert "last_sent" not in status["health"]
        assert status["email"] == {
            "service": "smtp", "sender": "GrantWatch <alerts@und.edu>", "error": None, "last_sent": "2026-09-14T12:30:00+00:00",
        }
        assert status["health"]["data_age_seconds"] == 2 * HOUR

    def test_emails_are_counted_by_the_teams_day(self, fake_db, monkeypatch):
        admin.admin_status()
        assert fake_db.params[admin._EMAILS_SQL] == {"tz": "America/Chicago"}
        monkeypatch.setenv("GRANTS_TIMEZONE", "Europe/Oslo")
        admin.admin_status()
        assert fake_db.params[admin._EMAILS_SQL] == {"tz": "Europe/Oslo"}
        assert "AT TIME ZONE %(tz)s" in admin._EMAILS_SQL

    @pytest.mark.parametrize("name", ["America/Chicag", "America", "../etc/passwd", "/etc/passwd", "Mars/Olympus"])
    def test_a_bad_time_zone_is_ignored_with_a_warning(self, fake_db, monkeypatch, caplog, name):
        monkeypatch.setenv("GRANTS_TIMEZONE", name)
        with caplog.at_level(logging.WARNING, logger="grantwatch"):
            status = admin.admin_status()
        assert fake_db.params[admin._EMAILS_SQL] == {"tz": "America/Chicago"}
        assert [w for w in status["warnings"] if "GRANTS_TIMEZONE" in w] == [
            f"GRANTS_TIMEZONE is {name!r}, which is not a time zone name. It was ignored and America/Chicago is used."
        ]
        assert "GRANTS_TIMEZONE" in caplog.text and "America/Chicago is used" in caplog.text
        json.dumps(status)

    def test_a_long_bad_time_zone_is_cut_in_the_warning(self, fake_db, monkeypatch):
        monkeypatch.setenv("GRANTS_TIMEZONE", "x" * 500)
        warning = admin._timezone()[1]
        assert "x" * 60 in warning and "x" * 61 not in warning

    @pytest.mark.parametrize("name, used", [("Europe/Oslo", "Europe/Oslo"), ("UTC", "UTC"), ("  Europe/Oslo ", "Europe/Oslo"), ("", "America/Chicago"), ("   ", "America/Chicago")])
    def test_a_good_or_blank_time_zone_gives_no_warning(self, fake_db, monkeypatch, caplog, name, used):
        monkeypatch.setenv("GRANTS_TIMEZONE", name)
        with caplog.at_level(logging.WARNING, logger="grantwatch"):
            status = admin.admin_status()
        assert fake_db.params[admin._EMAILS_SQL] == {"tz": used}
        assert not any("GRANTS_TIMEZONE" in w for w in status["warnings"])
        assert "GRANTS_TIMEZONE" not in caplog.text

    def test_the_profile_and_settings_come_with_it(self, fake_db):
        status = admin.admin_status()
        assert status["profile"]["summary"] == "x" and status["profile"]["configured"] is True
        assert status["settings"]["data_source"] == "extract"

    def test_a_fresh_database_reports_an_empty_page(self, monkeypatch):
        answers = {admin._RUNS_SQL: [], admin._HEALTH_SQL: [{}], admin._RECIPIENTS_SQL: [], admin._EMAILS_SQL: []}
        monkeypatch.setattr(admin, "db_connection", FakeDb(answers).connection)
        monkeypatch.setattr(admin, "_ensure_runs_table", lambda: None)
        monkeypatch.setattr(admin, "load_profile", lambda: Profile())
        status = admin.admin_status()
        assert status["runs"] == [] and status["emails"] == []
        assert status["health"]["open"] is None and status["health"]["scoring_on"] is False
        assert status["warnings"] == ["No daily run has been recorded yet.", "Nobody gets email yet: there are no active recipients."]
        json.dumps(status)

    def test_a_missing_table_or_column_applies_the_schema_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(admin, "ensure_schema", lambda: calls.append("schema"))
        for stale, expected in ((True, ["schema"]), (False, [])):
            calls.clear()
            monkeypatch.setattr(admin, "db_connection", FakeDb({admin._SCHEMA_STALE_SQL: [(stale,)]}).connection)
            admin._ensure_runs_table()
            assert calls == expected

    def test_the_schema_check_looks_for_the_table_and_the_newest_column(self):
        sql = " ".join(admin._SCHEMA_STALE_SQL.split())
        assert "to_regclass('job_runs') IS NULL" in sql
        assert "information_schema.columns" in sql and "column_name = 'stats'" in sql


class TestRunLog:
    def test_returns_the_saved_log(self, monkeypatch):
        db = FakeDb({admin._LOG_SQL: [{"id": 5, "step": "ingest", "log": "line 1\nline 2", "ok": True}]})
        monkeypatch.setattr(admin, "db_connection", db.connection)
        assert admin.run_log(5) == {"id": 5, "step": "ingest", "log": "line 1\nline 2"}
        assert db.params[admin._LOG_SQL] == (5,)

    def test_a_run_with_no_log_has_none(self, monkeypatch):
        monkeypatch.setattr(admin, "db_connection", FakeDb({admin._LOG_SQL: [{"id": 5, "step": "ingest", "log": None}]}).connection)
        assert admin.run_log(5) == {"id": 5, "step": "ingest", "log": None}

    def test_an_unknown_run_is_none(self, monkeypatch):
        monkeypatch.setattr(admin, "db_connection", FakeDb({admin._LOG_SQL: []}).connection)
        assert admin.run_log(404) is None


class TestProfileAndSettings:
    def test_the_profile_block(self):
        profile = Profile(
            summary="Grid resilience research.", institution="UND", keywords=["grid", "storage"], keyword_threshold=2,
            agencies_exclude=["DOD"], min_score_to_notify=4, deadline_horizons=[7, 30], reminder_days=[14, 2],
            overview_day="monday", include_forecasted=False,
        )
        assert admin.profile_summary(profile) == {
            "configured": True, "summary": "Grid resilience research.", "institution": "UND", "keywords": ["grid", "storage"],
            "keyword_threshold": 2, "min_score": 4, "reminder_days": [14, 2], "overview_day": "monday",
            "include_forecasted": False, "agencies_exclude": ["DOD"], "horizons": [7, 30],
        }

    def test_an_unwritten_profile_is_not_configured(self):
        assert admin.profile_summary(Profile(summary="REPLACE ME with the group's work"))["configured"] is False

    def test_the_settings_when_nothing_is_set(self):
        assert admin.settings_summary() == {
            "data_source": "extract", "lookback_days": None,
            "llm": {"host": "http://localhost:11434", "model": "", "batch_size": 3, "workers": 2, "max_per_run": 400,
                    "timeout": 180.0, "max_tokens": 0, "thinking": None},
            "email_max_new": 25, "dashboard_url": "",
        }

    def test_the_settings_from_the_environment(self, monkeypatch):
        for name, value in {
            "GRANTS_DATA_SOURCE": " Export ", "GRANTS_GOV_LOOKBACK_DAYS": "90", "GRANTS_LLM_MODEL": "qwen3.6-35b",
            "GRANTS_LLM_BASE_URL": "http://user:pw@10.226.31.254:18100/v1?key=abc", "GRANTS_LLM_BATCH_SIZE": "4",
            "GRANTS_LLM_WORKERS": "3", "GRANTS_RELEVANCE_MAX_PER_RUN": "1500", "GRANTS_LLM_TIMEOUT": "60",
            "GRANTS_LLM_MAX_TOKENS": "4096", "GRANTS_EMAIL_MAX_NEW": "10", "GRANTS_DASHBOARD_URL": "http://10.0.0.1:30810/",
            "GRANTS_LLM_EXTRA_BODY": '{"chat_template_kwargs": {"enable_thinking": false}}',
        }.items():
            monkeypatch.setenv(name, value)
        assert admin.settings_summary() == {
            "data_source": "export", "lookback_days": 90,
            "llm": {"host": "http://10.226.31.254:18100", "model": "qwen3.6-35b", "batch_size": 4, "workers": 3,
                    "max_per_run": 1500, "timeout": 60.0, "max_tokens": 4096, "thinking": False},
            "email_max_new": 10, "dashboard_url": "http://10.0.0.1:30810",
        }

    @pytest.mark.parametrize("raw, expected", [("ninety", None), ("", None), ("0", 0), ("-5", -5), (" 30 ", 30)])
    def test_the_lookback_is_a_number_or_nothing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("GRANTS_GOV_LOOKBACK_DAYS", raw)
        assert admin.settings_summary()["lookback_days"] == expected

    @pytest.mark.parametrize("extra", ['{"chat_template_kwargs": 5}', '{"chat_template_kwargs": {"enable_thinking": "sk-0123456789"}}', "[]", "not json"])
    def test_odd_thinking_settings_show_nothing_rather_than_raw_text(self, monkeypatch, extra):
        monkeypatch.setenv("GRANTS_LLM_EXTRA_BODY", extra)
        assert admin.settings_summary()["llm"]["thinking"] is None

    def test_thinking_on(self, monkeypatch):
        monkeypatch.setenv("GRANTS_LLM_EXTRA_BODY", '{"chat_template_kwargs": {"enable_thinking": true}}')
        assert admin.settings_summary()["llm"]["thinking"] is True


class TestEmailStatus:
    def test_nothing_set_up(self):
        assert admin.email_status() == {"service": None, "sender": None, "error": None, "last_sent": None}

    @pytest.mark.parametrize("env, service", [
        ({"GRANTS_SMTP_HOST": "smtp.example.org"}, "smtp"),
        ({"GMAIL_TOKEN_JSON": '{"token": "x"}'}, "gmail"),
        ({"GRANTS_EMAIL_OUTBOX": "outbox"}, "outbox"),
    ])
    def test_the_service_in_use(self, monkeypatch, env, service):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        assert admin.email_status()["service"] == service

    def test_the_sender_falls_back_the_way_the_mailer_does(self, monkeypatch):
        monkeypatch.setenv("GRANTS_SMTP_USER", "c2sr.und@gmail.com")
        assert admin.email_status()["sender"] == "c2sr.und@gmail.com"

    def test_the_sender_never_shows_the_smtp_password(self, monkeypatch):
        # Some providers (Postmark) use one token for the SMTP user and password.
        monkeypatch.setenv("GRANTS_SMTP_HOST", "smtp.example.org")
        monkeypatch.setenv("GRANTS_SMTP_USER", "tok-0123456789abcdef")
        monkeypatch.setenv("GRANTS_SMTP_PASSWORD", "tok-0123456789abcdef")
        assert "tok-0123456789abcdef" not in json.dumps(admin.email_status())

    def test_a_misconfiguration_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setenv("GRANTS_EMAIL_TRANSPORT", "smtp")
        assert admin.email_status() == {"service": None, "sender": None, "error": "GRANTS_EMAIL_TRANSPORT=smtp needs GRANTS_SMTP_HOST", "last_sent": None}

    def test_the_last_send_is_a_timestamp(self):
        assert admin.email_status(STARTED)["last_sent"] == "2026-09-14T12:30:00+00:00"


class FakeReply:
    def __init__(self, status=200, body=None, json_ok=True):
        self.status_code, self.body, self.json_ok = status, body, json_ok

    def json(self):
        if not self.json_ok:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self.body


def fake_get(monkeypatch, reply=None, error=None):
    """Replaces requests.get; returns the list of calls it receives."""
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if error is not None:
            raise error
        return reply

    monkeypatch.setattr(admin.requests, "get", get)
    return calls


@pytest.fixture
def model_server(monkeypatch):
    monkeypatch.setenv("GRANTS_LLM_MODEL", "qwen3.6-35b")
    monkeypatch.setenv("GRANTS_LLM_BASE_URL", "http://10.226.31.254:18100/v1/")
    monkeypatch.setenv("GRANTS_LLM_API_KEY", "real-key-1234567")


class TestLlmCheck:
    def test_no_model_means_not_set_up(self, monkeypatch):
        calls = fake_get(monkeypatch, FakeReply())
        assert admin.llm_check() == {"configured": False}
        assert calls == []

    def test_a_server_that_lists_the_model(self, monkeypatch, model_server):
        calls = fake_get(monkeypatch, FakeReply(body={"data": [{"id": "other"}, {"id": "qwen3.6-35b"}]}))
        result = admin.llm_check()
        assert result == {
            "configured": True, "reachable": True, "status": 200, "latency_ms": result["latency_ms"],
            "host": "http://10.226.31.254:18100", "model": "qwen3.6-35b", "model_listed": True,
            "models": ["other", "qwen3.6-35b"], "error": None, "timeout_s": 5.0,
        }
        assert isinstance(result["latency_ms"], int)
        url, kwargs = calls[0]
        assert url == "http://10.226.31.254:18100/v1/models"
        assert kwargs["headers"] == {"Authorization": "Bearer real-key-1234567"} and kwargs["timeout"] == 5.0

    def test_the_default_key_is_still_sent(self, monkeypatch):
        monkeypatch.setenv("GRANTS_LLM_MODEL", "m")
        calls = fake_get(monkeypatch, FakeReply(body={"data": []}))
        admin.llm_check(timeout=2)
        assert calls[0][1]["headers"] == {"Authorization": "Bearer ollama"} and calls[0][1]["timeout"] == 2

    def test_a_server_without_the_model(self, monkeypatch, model_server):
        fake_get(monkeypatch, FakeReply(body={"data": [{"id": "a"}, {"id": "b"}]}))
        result = admin.llm_check()
        assert result["reachable"] is True and result["model_listed"] is False and result["models"] == ["a", "b"]

    def test_ollama_style_lists(self, monkeypatch, model_server):
        fake_get(monkeypatch, FakeReply(body={"models": [{"name": "qwen3.6-35b"}, {"id": "x"}, "junk", {}]}))
        result = admin.llm_check()
        assert result["models"] == ["qwen3.6-35b", "x"] and result["model_listed"] is True

    def test_only_ten_models_are_listed_but_all_are_searched(self, monkeypatch, model_server):
        fake_get(monkeypatch, FakeReply(body={"data": [{"id": f"m{n}"} for n in range(30)] + [{"id": "qwen3.6-35b"}]}))
        result = admin.llm_check()
        assert result["models"] == [f"m{n}" for n in range(10)] and result["model_listed"] is True

    @pytest.mark.parametrize("body", [None, 5, [], ["a"], "text", True, {"data": "x"}, {"data": [1, None]}, {"data": [{"id": None}]}, float("nan")])
    def test_json_that_is_not_a_model_list(self, monkeypatch, model_server, body):
        fake_get(monkeypatch, FakeReply(body=body))
        result = admin.llm_check()
        assert result["reachable"] is True and result["models"] == [] and result["model_listed"] is False

    def test_an_error_status(self, monkeypatch, model_server):
        fake_get(monkeypatch, FakeReply(status=401, body={"error": "bad key real-key-1234567"}))
        result = admin.llm_check()
        assert result["reachable"] is False and result["status"] == 401 and result["model_listed"] is None
        assert result["error"] == "The server answered HTTP 401." and isinstance(result["latency_ms"], int)

    def test_a_page_that_is_not_json(self, monkeypatch, model_server):
        fake_get(monkeypatch, FakeReply(body="<html>", json_ok=False))
        result = admin.llm_check()
        assert result["reachable"] is False and result["status"] == 200 and result["model_listed"] is None
        assert "not with JSON" in result["error"]

    def test_a_server_that_cannot_be_reached(self, monkeypatch, model_server):
        fake_get(monkeypatch, error=requests.ConnectTimeout("Connection to 10.226.31.254 timed out. (connect timeout=5.0)"))
        result = admin.llm_check()
        assert result["reachable"] is False and result["status"] is None and result["latency_ms"] is None
        assert result["error"] == "ConnectTimeout: Connection to 10.226.31.254 timed out. (connect timeout=5.0)"

    def test_the_error_hides_the_url_the_key_and_user_info(self, monkeypatch):
        monkeypatch.setenv("GRANTS_LLM_MODEL", "m")
        monkeypatch.setenv("GRANTS_LLM_BASE_URL", "http://svc:hunter2hunter2@gpu.lab:8000/v1/?key=abcd1234efgh")
        monkeypatch.setenv("GRANTS_LLM_API_KEY", "real-key-1234567")
        text = ("HTTPConnectionPool(host='gpu.lab', port=8000): Max retries exceeded with url: /v1/models?key=abcd1234efgh "
                "(http://svc:hunter2hunter2@gpu.lab:8000/v1/models?key=abcd1234efgh, real-key-1234567)")
        fake_get(monkeypatch, error=requests.ConnectionError(text))
        result = admin.llm_check()
        everything = json.dumps(result)
        for leaked in ("hunter2hunter2", "abcd1234efgh", "real-key-1234567", "svc:"):
            assert leaked not in everything
        assert result["host"] == "http://gpu.lab:8000"

    def test_a_long_error_is_cut_to_200_characters(self, monkeypatch, model_server):
        fake_get(monkeypatch, error=RuntimeError("x" * 1000))
        assert len(admin.llm_check()["error"]) == 200

    def test_an_exception_that_cannot_describe_itself_still_gives_an_answer(self, monkeypatch, model_server):
        class Mute(Exception):
            def __str__(self):
                raise RuntimeError("no text")

        fake_get(monkeypatch, error=Mute())
        assert admin.llm_check()["error"] == "Mute: "

    @settings(**SLOW)
    @given(
        status=st.integers(0, 999),
        body=st.recursive(
            st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
            lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.sampled_from(["data", "models", "id", "name", "x"]), inner, max_size=4),
            max_leaves=12,
        ),
        json_ok=st.booleans(),
        error=st.none() | st.sampled_from([requests.ConnectionError, requests.Timeout, ValueError, RuntimeError, OSError]),
        error_text=st.text(max_size=300),
    )
    def test_never_raises_and_reachable_means_a_200_with_json(self, status, body, json_ok, error, error_text):
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("GRANTS_LLM_MODEL", "qwen3.6-35b")
            patch.setenv("GRANTS_LLM_BASE_URL", "http://10.226.31.254:18100/v1")
            fake_get(patch, FakeReply(status, body, json_ok), error(error_text) if error else None)
            result = admin.llm_check()
        json.dumps(result)
        assert result["configured"] is True
        if error:
            assert result["reachable"] is False and result["status"] is None and result["error"].startswith(error.__name__)
            assert len(result["error"]) <= 200
        else:
            assert result["reachable"] == (status == 200 and json_ok) and result["status"] == status
        assert (result["model_listed"] is None) == (not result["reachable"])
        assert len(result["models"]) <= 10 and all(isinstance(name, str) for name in result["models"])


URL_PARTS = dict(
    scheme=st.sampled_from(["http", "https", "HTTP"]),
    user=st.text(string.ascii_letters + string.digits, max_size=10),
    password=st.text(string.ascii_letters + string.digits + "-._~%", max_size=10),
    host=st.one_of(
        st.text(string.ascii_letters + string.digits + "-.", min_size=1, max_size=20).filter(lambda h: h[0].isalnum()),
        st.sampled_from(["[::1]", "[2001:db8::1]", "10.226.31.254", "localhost"]),
    ),
    port=st.none() | st.integers(1, 65535),
    path=st.text(string.ascii_letters + string.digits + "/-._~", max_size=20),
    query=st.text(string.ascii_letters + string.digits + "=&-._~", max_size=20),
    fragment=st.text(string.ascii_letters + string.digits + "-._~", max_size=10),
)


class TestSafeHost:
    @given(st.fixed_dictionaries(URL_PARTS), st.booleans(), st.booleans(), st.booleans(), st.booleans())
    def test_a_url_keeps_only_scheme_host_and_port(self, parts, with_user, with_path, with_query, with_fragment):
        userinfo = f"{parts['user']}:{parts['password']}@" if with_user else ""
        url = f"{parts['scheme']}://{userinfo}{parts['host']}" + (f":{parts['port']}" if parts["port"] else "")
        url += ("/" + parts["path"] if with_path else "") + ("?" + parts["query"] if with_query else "")
        url += "#" + parts["fragment"] if with_fragment else ""
        result = safe_host(url)
        split = urlsplit(result)
        assert (split.scheme, split.path, split.query, split.fragment) == (parts["scheme"].lower(), "", "", "")
        assert split.username is None and split.password is None
        assert split.hostname == parts["host"].strip("[]").lower() and split.port == (parts["port"] or None)
        assert "@" not in result and "?" not in result and "#" not in result

    @given(st.text(max_size=200))
    def test_never_raises_and_never_returns_more_than_a_host(self, text):
        result = safe_host(text)
        assert isinstance(result, str)
        assert result == "unknown" or not set(result.partition("://")[2]) & set("@?# /\\")

    @pytest.mark.parametrize("url, expected", [
        ("http://u:p@10.0.0.1:8000/v1?key=k#f", "http://10.0.0.1:8000"),
        ("https://Example.ORG/v1", "https://example.org"),
        ("http://[::1]:11434/v1", "http://[::1]:11434"),
        ("http://[bad", "unknown"), ("http://host:99999999/x", "unknown"), ("http://host:abc/x", "unknown"),
        ("", "unknown"), ("not a url", "unknown"), ("localhost:11434/v1", "unknown"), (None, "unknown"), ("http:///path", "unknown"),
    ])
    def test_examples(self, url, expected):
        assert safe_host(url) == expected


def _secret_env():
    word = st.text(string.ascii_letters + string.digits, min_size=8, max_size=24).map("S3CR3T".__add__)
    plain = [name for name in SECRET_VARS if name not in ("POSTGRES_URL", "GRANTS_LLM_BASE_URL", "OPENAI_BASE_URL")]
    return st.fixed_dictionaries({
        "plain": st.fixed_dictionaries({name: word for name in plain}),
        "db_password": word, "url_password": word, "url_key": word, "token": word,
    })


class TestNoSecretsOnThePage:
    @settings(**SLOW)
    @given(_secret_env())
    def test_nothing_secret_is_in_what_the_page_can_see(self, env):
        base_url = f"http://svc:{env['url_password']}@10.1.2.3:8000/v1/?key={env['url_key']}"
        values = dict(env["plain"], POSTGRES_URL=f"postgresql://app:{env['db_password']}@db:5432/grants",
                      GRANTS_LLM_BASE_URL=base_url, OPENAI_BASE_URL=base_url)
        with pytest.MonkeyPatch.context() as patch:
            for name, value in values.items():
                patch.setenv(name, value)
            patch.setenv("GRANTS_LLM_MODEL", "qwen3.6-35b")
            patch.setenv("GRANTS_SMTP_HOST", "smtp.example.org")
            db = FakeDb(_answers(token=env["token"]))
            patch.setattr(admin, "db_connection", db.connection)
            patch.setattr(admin, "_ensure_runs_table", lambda: None)
            patch.setattr(admin, "load_profile", lambda: Profile(summary="x"))
            outputs = [admin.admin_status(), admin.settings_summary(), admin.email_status()]
            url = base_url.rstrip("/") + "/models"
            for error in (requests.ConnectionError(f"Max retries exceeded with url: {url} ({base_url})"), RuntimeError(base_url), None):
                fake_get(patch, FakeReply(body={"data": [{"id": "qwen3.6-35b"}]}), error)
                outputs.append(admin.llm_check())
        written = json.dumps(outputs)
        for leaked in [*values.values(), env["db_password"], env["url_password"], env["url_key"], env["token"]]:
            assert leaked not in written


STATUS = {
    "runs": [], "health": {"scoring_on": False}, "recipients": [], "emails": [],
    "warnings": ["No daily run has been recorded yet."], "email": {}, "profile": {}, "settings": {},
}
PASSWORD = "lab-only-password"
REALM = 'Basic realm="GrantWatch admin"'
OFF_TEXT = "Set GRANTS_ADMIN_PASSWORD to 16 or more letters and digits on this server to turn it on."
ASK_TEXT = "Enter the admin password to see this page. Any user name works."


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GRANTS_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("GRANTS_SECRET_KEY", raising=False)  # the admin page does not depend on email sign-in
    monkeypatch.setattr(webapp, "admin_status", lambda: STATUS)
    monkeypatch.setattr(webapp, "llm_check", lambda: {"configured": False})
    monkeypatch.setattr(webapp, "run_log", lambda run_id: {"id": run_id, "step": "ingest", "log": "line 1"} if run_id == 5 else None)
    return TestClient(webapp.app, base_url="http://localhost")


PATHS = pytest.mark.parametrize("path", ["/admin/", "/admin/api", "/admin/api/llm", "/admin/api/runs/5/log"])


def _basic(raw: bytes) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def _is_page(reply) -> bool:
    return reply.headers["content-type"].startswith("text/html")


@PATHS
class TestAdminAccess:
    def test_off_without_a_password(self, client, monkeypatch, path):
        monkeypatch.delenv("GRANTS_ADMIN_PASSWORD", raising=False)
        for reply in (client.get(path), client.get(path, auth=("me", PASSWORD))):
            assert reply.status_code == 503 and reply.headers["Cache-Control"] == "no-store"
            assert OFF_TEXT in (reply.text if _is_page(reply) else reply.json()["detail"])

    @pytest.mark.parametrize("configured", ["", "   ", "REPLACE_ME", "REPLACE_ME_WITH_32_RANDOM_CHARACTERS", "replace-me-with-a-long-one", "a" * 15])
    def test_off_with_an_unusable_password(self, client, monkeypatch, path, configured):
        monkeypatch.setenv("GRANTS_ADMIN_PASSWORD", configured)
        assert client.get(path, auth=("me", configured)).status_code == 503

    def test_sixteen_characters_is_enough(self, client, monkeypatch, path):
        monkeypatch.setenv("GRANTS_ADMIN_PASSWORD", "a" * 16)
        assert client.get(path, auth=("me", "a" * 16)).status_code == 200
        assert client.get(path, auth=("me", "a" * 15)).status_code == 401

    def test_asks_for_credentials(self, client, path):
        reply = client.get(path)
        assert reply.status_code == 401
        assert reply.headers["WWW-Authenticate"] == REALM and reply.headers["Cache-Control"] == "no-store"
        assert ASK_TEXT in (reply.text if _is_page(reply) else reply.json()["detail"])

    def test_wrong_password(self, client, path):
        reply = client.get(path, auth=("me", "not-it"))
        assert reply.status_code == 401
        assert reply.headers["WWW-Authenticate"] == REALM

    def test_other_schemes_and_garbage_are_refused_the_same_way(self, client, path):
        assert client.get(path, headers={"Authorization": "Bearer " + PASSWORD}).status_code == 401
        for header in ("Basic !!!", "Basic", "Basic  ", "Digest abc"):
            garbage = client.get(path, headers={"Authorization": header})
            assert garbage.status_code == 401 and garbage.headers["WWW-Authenticate"] == REALM
            assert _is_page(garbage) == (path == "/admin/")
        non_ascii = client.get(path, headers=_basic("me:é".encode("utf-8")))
        assert non_ascii.status_code == 401 and _is_page(non_ascii) == (path == "/admin/")

    def test_malformed_credentials_do_not_hide_that_the_page_is_off(self, client, monkeypatch, path):
        monkeypatch.delenv("GRANTS_ADMIN_PASSWORD", raising=False)
        assert client.get(path, headers={"Authorization": "Basic !!!"}).status_code == 503

    def test_a_non_ascii_password_on_the_server_is_not_a_crash(self, client, monkeypatch, path):
        # Not a crash, but also never a way in: HTTPBasic decodes the header as ASCII, so docs say ASCII only.
        monkeypatch.setenv("GRANTS_ADMIN_PASSWORD", "pässword-long-enough")
        assert client.get(path, auth=("me", "password-long-enough")).status_code == 401
        assert client.get(path, headers=_basic("me:pässword-long-enough".encode("utf-8"))).status_code == 401

    def test_any_username_with_the_right_password(self, client, path):
        for username in ("me", "", "admin"):
            reply = client.get(path, auth=(username, PASSWORD))
            assert reply.status_code == 200, username
            assert reply.headers["Cache-Control"] == "no-store"


class TestAdminErrorPages:
    def test_the_page_answers_with_html_when_off(self, client, monkeypatch):
        monkeypatch.delenv("GRANTS_ADMIN_PASSWORD", raising=False)
        reply = client.get("/admin/")
        assert reply.status_code == 503 and _is_page(reply) and "WWW-Authenticate" not in reply.headers
        assert "<h1>The admin page is off</h1>" in reply.text and OFF_TEXT in reply.text

    def test_the_page_answers_with_html_and_a_challenge_without_credentials(self, client):
        reply = client.get("/admin/")
        assert reply.status_code == 401 and _is_page(reply) and reply.headers["WWW-Authenticate"] == REALM
        assert "<h1>Admin password needed</h1>" in reply.text and ASK_TEXT in reply.text

    def test_the_api_keeps_json_errors(self, client):
        for path in ("/admin/api", "/admin/api/llm", "/admin/api/runs/5/log"):
            reply = client.get(path)
            assert reply.status_code == 401 and reply.headers["content-type"] == "application/json"
            assert reply.json() == {"detail": ASK_TEXT}


class TestAdminRoutes:
    def test_the_bare_path_redirects_to_the_slash_path_without_a_password(self, client, monkeypatch):
        for password in (PASSWORD, None):
            if password is None:
                monkeypatch.delenv("GRANTS_ADMIN_PASSWORD", raising=False)
            reply = client.get("/admin", follow_redirects=False)
            assert reply.status_code == 307 and reply.headers["location"] == "/admin/"
            assert "WWW-Authenticate" not in reply.headers

    def test_following_the_redirect_ends_at_the_challenge(self, client):
        reply = client.get("/admin")
        assert reply.status_code == 401 and str(reply.url).endswith("/admin/")

    def test_the_old_data_route_is_gone(self, client):
        assert client.get("/api/admin", auth=("me", PASSWORD)).status_code == 404

    def test_the_dashboard_never_asks_for_the_admin_password(self, client):
        for path in ("/", "/api/session"):
            reply = client.get(path)
            assert reply.status_code == 200 and "WWW-Authenticate" not in reply.headers

    def test_the_data(self, client):
        reply = client.get("/admin/api", auth=("me", PASSWORD))
        assert reply.json() == STATUS

    def test_the_model_check_is_its_own_route(self, client, monkeypatch):
        monkeypatch.setattr(webapp, "llm_check", lambda: {"configured": True, "reachable": False, "error": "ConnectTimeout: x"})
        reply = client.get("/admin/api/llm", auth=("me", PASSWORD))
        assert reply.json() == {"configured": True, "reachable": False, "error": "ConnectTimeout: x"}

    def test_the_log_of_a_run(self, client):
        reply = client.get("/admin/api/runs/5/log", auth=("me", PASSWORD))
        assert reply.status_code == 200 and reply.json() == {"id": 5, "step": "ingest", "log": "line 1"}

    def test_an_unknown_run_is_a_404(self, client):
        reply = client.get("/admin/api/runs/404/log", auth=("me", PASSWORD))
        assert reply.status_code == 404 and reply.json() == {"detail": "No such run."}

    @pytest.mark.parametrize("run_id", ["abc", "0", "-1", "99999999999", "1.5"])
    def test_a_run_id_must_be_a_database_integer(self, client, run_id):
        assert client.get(f"/admin/api/runs/{run_id}/log", auth=("me", PASSWORD)).status_code == 422

    def test_the_password_is_asked_before_the_run_id_is_checked(self, client):
        assert client.get("/admin/api/runs/abc/log").status_code == 401

    def test_a_database_error_is_a_500_with_a_message(self, client, monkeypatch):
        def broken(*args):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(webapp, "admin_status", broken)
        monkeypatch.setattr(webapp, "run_log", broken)
        reply = client.get("/admin/api", auth=("me", PASSWORD))
        assert reply.status_code == 500
        assert reply.json()["detail"] == "Database query failed while loading the admin status."
        reply = client.get("/admin/api/runs/5/log", auth=("me", PASSWORD))
        assert reply.status_code == 500 and "connection refused" not in reply.text


class TestAdminPage:
    def test_the_page(self, client):
        reply = client.get("/admin/", auth=("me", PASSWORD))
        assert _is_page(reply) and "GrantWatch admin" in reply.text

    def test_the_page_asks_for_relative_urls_so_the_credentials_stay_under_admin(self):
        assert '"api"' in webapp.ADMIN_HTML and '"api/llm"' in webapp.ADMIN_HTML and '"api/runs/' in webapp.ADMIN_HTML
        assert '"/api' not in webapp.ADMIN_HTML and "/api/admin" not in webapp.ADMIN_HTML

    @pytest.mark.parametrize("sink", ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("])
    def test_the_page_never_puts_data_in_as_html(self, sink):
        assert sink not in webapp.ADMIN_HTML

    def test_the_page_clears_nodes_with_replaceChildren(self):
        assert "replaceChildren()" in webapp.ADMIN_HTML

    def test_the_email_row_counts_only_the_newest_notify_run(self):
        row = webapp.ADMIN_HTML.split("function emailRow(data) {")[1].split("\n  }\n")[0]
        assert ".find(" not in row and 'newest(data.runs, "notify")' in row  # never an older run's counts
        assert "stopped before sending" in row and "From the run of" in row

    def test_the_page_follows_the_dashboards_tokens_and_font(self):
        for line in webapp.INDEX_HTML.split("</style>")[0].split("* { box-sizing")[0].splitlines():
            if line.strip().startswith("--") or "color-scheme" in line:
                assert line.strip() in webapp.ADMIN_HTML, line
        assert "Public+Sans" in webapp.ADMIN_HTML

    def test_the_page_has_none_of_the_marks_the_design_rules_out(self):
        for banned in ("·", "→", "↗", "text-transform: uppercase"):
            assert banned not in webapp.ADMIN_HTML

    def test_the_dashboard_links_to_it(self, client):
        assert 'href="/admin/"' in client.get("/").text
