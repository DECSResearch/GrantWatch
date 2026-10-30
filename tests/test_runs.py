"""The run log: recorded() never changes a job's outcome, saves the log and numbers, and keeps secrets out;
the steps describe themselves."""
from __future__ import annotations

import json
import logging
import string
import traceback
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from openai import APIConnectionError

import main
from grants import runs
from grants.data.loader import LoadSummary
from grants.profile import Profile
from grants_data import pipeline
from llm_utils.relevance import LLMSettings, ScoringOutcome, safe_host, score_records
from logs.status_logger import FORMATTER, logger
from notifications.notify import run_notify
from notifications.planner import NotifySettings, Recipient
from support import CaptureTransport, MemoryStore
from test_digest import _grant

ME = Recipient(id=1, email="me@und.edu", unsubscribe_token="t1")
QUIET = NotifySettings(overview_day="never")
SECRET_VARS = [
    "GRANTS_LLM_API_KEY", "OPENAI_API_KEY", "POSTGRES_PASSWORD", "POSTGRES_URL", "GRANTS_SECRET_KEY",
    "GRANTS_SMTP_PASSWORD", "GMAIL_TOKEN_JSON", "GRANTS_LLM_BASE_URL", "OPENAI_BASE_URL",
]
LOG_TEXT = st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00")


def not_a_url(text: str) -> bool:
    """True when the redactor, which only acts on URLs, has nothing to change in ``text``."""
    return "://" not in text and "url: /" not in text
# Tests that count on the planner's example budget share one reduced size, as test_planner_properties does.
SLOW = dict(max_examples=max(20, settings.default.max_examples // 5), deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


class FakeDb:
    """Stands in for runs.db_connection and remembers every statement."""

    def __init__(self) -> None:
        self.executed = []
        self.connects = 0
        self.fail_from = None  # the connection number (1-based) from which connecting raises
        self.fail_on = set()  # connection numbers that raise
        self.reject = None  # a statement containing this text raises when it runs

    @contextmanager
    def connection(self):
        self.connects += 1
        if (self.fail_from is not None and self.connects >= self.fail_from) or self.connects in self.fail_on:
            raise RuntimeError("database is down")
        db = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=()):
                if db.reject and db.reject in sql:
                    raise RuntimeError("column does not exist")
                db.executed.append((sql.split()[0], params))

            def fetchone(self):
                return (7,)

        class Conn:
            def cursor(self):
                return Cursor()

        yield Conn()


@pytest.fixture
def db(monkeypatch):
    for name in SECRET_VARS:
        monkeypatch.delenv(name, raising=False)
    fake = FakeDb()
    monkeypatch.setattr(runs, "db_connection", fake.connection)
    return fake


def ended(db):
    """The last UPDATE as (kind, (ok, detail), run id): the middle columns vary with what was saved."""
    kind, params = db.executed[-1]
    return kind, params[:2], params[-1]


def saved(db):
    """(log, stats) of the last full UPDATE."""
    return db.executed[-1][1][2:4]


class TestRecorded:
    def test_records_a_normal_run(self, db):
        with runs.recorded("ingest") as run:
            assert run.ok is True and run.detail is None and run.stats is None
            run.detail = "5 new"
        assert db.executed[0] == ("INSERT", ("ingest",))
        assert ended(db) == ("UPDATE", (True, "5 new"), 7)

    def test_records_a_failure_the_caller_set(self, db):
        with runs.recorded("notify") as run:
            run.ok = False
            run.detail = "sent 0, failed 2"
        assert ended(db) == ("UPDATE", (False, "sent 0, failed 2"), 7)

    def test_records_an_exception_and_reraises_it(self, db):
        with pytest.raises(ValueError, match="boom"):
            with runs.recorded("ingest"):
                raise ValueError("boom")
        assert ended(db) == ("UPDATE", (False, "ValueError: boom"), 7)

    def test_an_exception_that_cannot_print_is_still_recorded_as_failed(self, db):
        class Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("no text")

        with pytest.raises(Unprintable):
            with runs.recorded("ingest"):
                raise Unprintable()
        kind, (ok, detail), run_id = ended(db)
        assert (kind, ok, run_id) == ("UPDATE", False, 7)
        assert detail.startswith("Unprintable: ")
        assert saved(db)[0].endswith("Unprintable: <exception str() failed>")

    def test_the_callers_detail_beats_the_exception_text(self, db):
        with pytest.raises(RuntimeError):
            with runs.recorded("ingest") as run:
                run.detail = "83,388 in the extract"
                raise RuntimeError("late failure")
        assert ended(db) == ("UPDATE", (False, "83,388 in the extract"), 7)

    def test_a_killed_process_is_recorded_too(self, db):
        with pytest.raises(KeyboardInterrupt):
            with runs.recorded("ingest"):
                raise KeyboardInterrupt()
        assert db.executed[-1][1][0] is False

    def test_long_detail_is_clipped(self, db):
        with runs.recorded("ingest") as run:
            run.detail = "x" * 5000
        assert len(db.executed[-1][1][1]) == 1000

    def test_database_down_changes_nothing(self, db):
        db.fail_from = 1
        with runs.recorded("ingest") as run:
            run.detail = "fine"
        assert db.connects == 1  # no update is attempted without a row to update
        assert db.executed == []

    def test_database_down_does_not_hide_the_jobs_own_error(self, db):
        db.fail_from = 1
        with pytest.raises(ValueError, match="boom"):
            with runs.recorded("ingest"):
                raise ValueError("boom")

    def test_update_failing_is_swallowed(self, db):
        db.fail_from = 2
        with runs.recorded("ingest"):
            pass
        assert [kind for kind, _ in db.executed] == ["INSERT"]

    def test_the_log_and_the_numbers_are_saved(self, db):
        with runs.recorded("ingest") as run:
            logger("info", "downloaded the extract")
            logger("warning", "two lines\nof warning")
            run.stats = {"extract": 83388, "kept": 399, "source": "extract"}
        log, stats = saved(db)
        assert log.splitlines()[0].endswith("| INFO | downloaded the extract")
        assert log.splitlines()[1].endswith("| WARNING | two lines")
        assert log.splitlines()[2] == "of warning"
        assert json.loads(stats) == {"extract": 83388, "kept": 399, "source": "extract"}

    def test_a_step_with_no_log_lines_or_numbers_saves_nulls(self, db):
        with runs.recorded("notify"):
            pass
        assert saved(db) == (None, None)

    def test_lines_logged_outside_the_step_are_not_kept(self, db):
        logger("info", "before")
        with runs.recorded("ingest"):
            logger("info", "during")
        logger("info", "after")
        assert [line.split(" | ", 2)[-1] for line in saved(db)[0].splitlines()] == ["during"]

    def test_a_failure_ends_the_log_with_its_traceback(self, db):
        with pytest.raises(ValueError):
            with runs.recorded("ingest"):
                logger("error", "about to fail")
                raise ValueError("boom")
        log = saved(db)[0]
        assert "| ERROR | about to fail\nTraceback (most recent call last):" in log
        assert log.endswith("ValueError: boom")

    def test_a_huge_traceback_keeps_its_last_5000_characters(self, db):
        with pytest.raises(ValueError):
            with runs.recorded("ingest"):
                raise ValueError("a" * 20000 + "the end")
        log = saved(db)[0]
        assert len(log) == 5000 and log.endswith("the end")

    def test_each_line_is_cut_to_500_characters_and_the_log_to_its_last_500_lines(self, db):
        with runs.recorded("ingest"):
            logger("info", "x" * 3000)
            for number in range(600):
                logger("info", f"line {number}")
        lines = saved(db)[0].splitlines()
        assert len(lines) == 500 and lines[-1].endswith("line 599") and lines[0].endswith("line 100")
        with runs.recorded("ingest"):
            logger("info", "x" * 3000)
        assert len(saved(db)[0]) == 500

    def test_the_log_is_cut_to_its_last_100000_characters(self, db):
        with runs.recorded("ingest"):
            for number in range(400):
                logger("info", f"{number:03d} " + "y" * 440)  # under 500 characters with the timestamp and level
        log = saved(db)[0]
        assert len(log) == 100_000 and log.endswith("399 " + "y" * 440)

    def test_nul_is_stripped_before_writing(self, db):
        with runs.recorded("ingest") as run:
            logger("info", "a\x00b")
            run.detail = "de\x00tail"
            run.stats = {"re\x00ason": "x\x00y", "nested": ["p\x00q"]}
        log, stats = saved(db)
        assert "\x00" not in log and "ab" in log
        assert ended(db)[1] == (True, "detail")
        assert json.loads(stats) == {"reason": "xy", "nested": ["pq"]}
        assert "\\u0000" not in stats

    def test_stats_that_will_not_serialise_still_leave_the_outcome(self, db):
        with runs.recorded("ingest") as run:
            run.detail = "kept"
            run.stats = {(1, 2): "tuple keys cannot be JSON"}
        assert [kind for kind, _ in db.executed] == ["INSERT", "UPDATE"]
        assert ended(db) == ("UPDATE", (True, "kept"), 7)
        assert len(db.executed[-1][1]) == 3

    def test_when_the_log_columns_are_missing_the_outcome_still_goes_in(self, db):
        db.reject = "stats"
        with runs.recorded("notify") as run:
            run.detail = "sent 1"
            run.stats = {"sent": 1}
        assert [(kind, len(params)) for kind, params in db.executed] == [("INSERT", 1), ("UPDATE", 3)]
        assert ended(db) == ("UPDATE", (True, "sent 1"), 7)

    def test_secrets_are_removed_from_the_log_the_detail_and_the_numbers(self, db, monkeypatch):
        monkeypatch.setenv("GRANTS_LLM_API_KEY", "sk-live-0123456789")
        monkeypatch.setenv("GRANTS_LLM_BASE_URL", "http://user:hunter2hunter2@10.1.2.3:8000/v1?key=abcdef123456")
        with pytest.raises(RuntimeError):
            with runs.recorded("ingest") as run:
                logger("error", "auth failed with sk-live-0123456789 at http://user:hunter2hunter2@10.1.2.3:8000/v1?key=abcdef123456")
                run.stats = {"scoring_abort": "bad key sk-live-0123456789"}
                raise RuntimeError("could not use sk-live-0123456789")
        log, stats = saved(db)
        everything = log + stats + db.executed[-1][1][1]
        for leaked in ("sk-live-0123456789", "hunter2hunter2", "abcdef123456", "/v1"):
            assert leaked not in everything
        assert "auth failed with *** at http://10.1.2.3:8000" in log

    def test_short_values_are_not_treated_as_secrets(self, db, monkeypatch):
        monkeypatch.setenv("GRANTS_LLM_API_KEY", "ollama")
        with runs.recorded("ingest"):
            logger("info", "ollama is fine")
        assert "ollama is fine" in saved(db)[0]

    def test_urls_lose_their_user_info_and_query_even_when_unknown(self):
        assert runs.redact("GET https://u:p@host.example/a?token=zz&x=1 failed") == "GET https://host.example/a failed"
        assert runs.redact("with url: /v1/models?key=zz (Caused by x)") == "with url: /v1/models (Caused by x)"
        assert runs.redact("Is this ok? yes: a@b") == "Is this ok? yes: a@b"

    def test_a_request_url_given_to_redact_shrinks_to_its_host(self):
        assert runs.redact("down: http://gpu:9/v1/models", urls=["http://gpu:9/v1/models"]) == "down: http://gpu:9"


class TestHandlers:
    def test_a_handler_is_added_for_the_step_and_removed_after_it(self, db):
        channel = logging.getLogger("grantwatch")
        before = list(channel.handlers)
        with runs.recorded("ingest"):
            assert len(channel.handlers) == len(before) + 1
            with runs.recorded("notify"):
                assert len(channel.handlers) == len(before) + 2
        assert channel.handlers == before

    def test_two_steps_in_one_process_do_not_share_lines(self, db):
        with runs.recorded("ingest"):
            logger("info", "from ingest")
        with runs.recorded("notify"):
            logger("info", "from notify")
        assert "from ingest" not in saved(db)[0] and "from notify" in saved(db)[0]

    def test_a_handler_that_cannot_format_does_not_reach_the_job(self, db, monkeypatch):
        monkeypatch.setattr(runs, "redact", lambda *args, **kwargs: 1 / 0)
        with runs.recorded("ingest"):
            logger("info", "still fine")


class TestNeverChangesTheOutcome:
    @given(
        failing=st.sets(st.integers(1, 3)),
        outcome=st.sampled_from(["return", "not ok", "raise"]),
    )
    def test_any_body_with_any_database_failure(self, failing, outcome):
        db = FakeDb()
        db.fail_on = failing
        raised = None
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(runs, "db_connection", db.connection)
            try:
                with runs.recorded("ingest") as run:
                    run.ok = outcome != "not ok"
                    run.stats = {"kept": 1}
                    if outcome == "raise":
                        raise ValueError("boom")
            except Exception as exc:
                raised = exc
        assert (type(raised) is ValueError) == (outcome == "raise")
        # Connection 1 is the INSERT, 2 the full UPDATE, 3 the retry with only the outcome.
        expected = [] if 1 in failing else ["INSERT"] + (["UPDATE"] if not {2, 3} <= failing else [])
        assert [kind for kind, _ in db.executed] == expected
        if len(expected) == 2:
            kind, params = db.executed[1]
            assert params[0] == (outcome == "return")
            assert len(params) == (5 if 2 not in failing else 3)


def _log_calls():
    levels = st.sampled_from(["info", "warning", "error", "critical"])  # the logger drops debug
    message = st.lists(st.text(LOG_TEXT, max_size=700), min_size=1, max_size=4).map(lambda parts: "\n".join(parts)[:2000]).filter(not_a_url)
    return st.lists(st.tuples(levels, message), max_size=40)


class Collect(logging.Handler):
    """Formats every record the way the run log does, to compare with."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(FORMATTER)
        self.texts = []

    def emit(self, record) -> None:
        self.texts.append(self.format(record))


class TestLogCapture:
    @settings(**SLOW)
    @given(calls=_log_calls(), raises=st.booleans(), error_text=st.text(LOG_TEXT, max_size=2000).filter(not_a_url))
    def test_the_saved_log_is_the_last_lines_in_order(self, calls, raises, error_text):
        db = FakeDb()
        channel = logging.getLogger("grantwatch")
        collect = Collect()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(runs, "db_connection", db.connection)
            for name in SECRET_VARS:
                patch.delenv(name, raising=False)
            before = list(channel.handlers)
            channel.addHandler(collect)
            try:
                try:
                    with runs.recorded("ingest"):
                        for level, message in calls:
                            logger(level, message)
                        if raises:
                            raise ValueError(error_text)
                except ValueError:
                    assert raises
            finally:
                channel.removeHandler(collect)
            assert channel.handlers == before
        log = db.executed[-1][1][2] or ""
        lines = [line[:500] for text in collect.texts for line in text.split("\n")][-500:]
        assert len(log) <= 100_000 and all(len(line) <= 500 for line in log.split("\n")[: len(lines)])
        if not raises:
            assert log == "\n".join(lines)[-100_000:]
            return
        marker = "Traceback (most recent call last):"
        assert marker in log
        assert (("\n".join(lines) + "\n") if lines else "").endswith(log[: log.index(marker)])
        assert log.endswith("".join(traceback.format_exception_only(ValueError, ValueError(error_text))).rstrip("\n"))


class TestIngestDetail:
    @given(st.booleans(), st.dictionaries(
        st.sampled_from(["extract", "kept", "scored", "cached", "failed", "deferred", "inserted", "updated", "reason", "scoring_abort", "other"]),
        st.one_of(st.none(), st.integers(), st.text(max_size=50)),
    ))
    def test_any_stats_give_a_sentence(self, ok, stats):
        assert isinstance(main.ingest_detail(ok, stats), str)

    FULL = dict(extract=83388, kept=399, scored=12, cached=300, failed=3, deferred=40, inserted=5, updated=394)

    def test_the_whole_story(self):
        assert main.ingest_detail(True, self.FULL) == (
            "83,388 in the extract, 399 kept, 12 newly scored (3 failed, 40 left for later), "
            "5 new and 394 updated in the database"
        )

    def test_nothing_new_to_score_leaves_the_scoring_part_out(self):
        stats = dict(self.FULL, scored=0, failed=0, deferred=0)
        assert main.ingest_detail(True, stats) == "83,388 in the extract, 399 kept, 5 new and 394 updated in the database"

    def test_scoring_trouble_shows_even_when_nothing_was_scored(self):
        stats = dict(self.FULL, scored=0, failed=4, deferred=0)
        assert "0 newly scored (4 failed)" in main.ingest_detail(True, stats)

    def test_failure_is_the_reason(self):
        assert main.ingest_detail(False, {"reason": "Summarizing descriptions failed."}) == "Summarizing descriptions failed."
        assert main.ingest_detail(False, {}) == "The pipeline failed; check the logs."

    def test_an_empty_result_says_why(self):
        stats = {"extract": 83388, "kept": 0, "reason": "No grants matched the profile keywords."}
        assert main.ingest_detail(True, stats) == "83,388 in the extract, 0 kept. No grants matched the profile keywords."

    def test_missing_keys_are_fine(self):
        assert main.ingest_detail(True, {}) == "The pipeline finished"

    def test_a_stopped_scorer_is_named(self):
        stats = dict(self.FULL, scoring_abort="cannot reach http://10.0.0.5:18100: Connection error.")
        assert main.ingest_detail(True, stats).endswith(
            "394 updated in the database. Scoring stopped: cannot reach http://10.0.0.5:18100: Connection error."
        )

    def test_a_stopped_scorer_is_named_on_a_failed_run_too(self):
        stats = {"reason": "Summarizing descriptions failed.", "scoring_abort": "authentication failed"}
        assert main.ingest_detail(False, stats) == "Summarizing descriptions failed. Scoring stopped: authentication failed."


def _no_secrets_env():
    """A strategy for environments full of secrets: names -> values, plus the pieces inside the URLs."""
    word = st.text(string.ascii_letters + string.digits, min_size=8, max_size=24).map("S3CR3T".__add__)
    return st.fixed_dictionaries({
        "plain": st.fixed_dictionaries({name: word for name in SECRET_VARS if name not in ("POSTGRES_URL", "GRANTS_LLM_BASE_URL", "OPENAI_BASE_URL")}),
        "db_password": word, "url_password": word, "url_key": word,
    })


class TestNoSecretsInTheRunLog:
    @settings(**SLOW)
    @given(_no_secrets_env())
    def test_nothing_secret_is_saved_or_described(self, env):
        db = FakeDb()
        base_url = f"http://svc:{env['url_password']}@10.1.2.3:8000/v1?key={env['url_key']}"
        values = dict(env["plain"], POSTGRES_URL=f"postgresql://app:{env['db_password']}@db:5432/grants",
                      GRANTS_LLM_BASE_URL=base_url, OPENAI_BASE_URL=base_url)
        shown = list(values.values())  # logged whole; the pieces inside the URLs only ever appear within them
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(runs, "db_connection", db.connection)
            for name, value in values.items():
                patch.setenv(name, value)
            try:
                with runs.recorded("ingest") as run:
                    for value in shown:
                        logger("error", f"failed with {value} and {values['POSTGRES_URL']}")
                    run.detail = f"detail {shown[0]} {base_url}"
                    run.stats = {"scoring_abort": f"cannot reach {base_url}: {shown[1]}", "list": [shown[2]]}
                    raise RuntimeError(f"{shown[3]} {base_url}/chat/completions")
            except RuntimeError:
                pass
        written = json.dumps(db.executed[-1][1])
        for leaked in [*shown, env["db_password"], env["url_password"], env["url_key"]]:
            assert leaked not in written

    @settings(**SLOW)
    @given(_no_secrets_env())
    def test_a_stopped_scorer_and_its_description_name_only_the_host(self, env):
        base_url = f"http://svc:{env['url_password']}@10.1.2.3:8000/v1?key={env['url_key']}"

        class Down:
            def __init__(self):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            def create(self, **kwargs):
                raise APIConnectionError(request=httpx2.Request("POST", base_url))

        outcome = score_records(
            [{"OPPORTUNITY_NUMBER": "A-1", "OPPORTUNITY_TITLE": "t"}], Profile(summary="Grid resilience."),
            settings=LLMSettings(base_url=base_url, model="m"), client=Down(),
        )
        assert outcome.abort_reason == f"cannot reach {safe_host(base_url)}: Connection error."
        detail = main.ingest_detail(True, {"kept": 1, "scoring_abort": outcome.abort_reason})
        for leaked in (env["url_password"], env["url_key"], base_url):
            assert leaked not in outcome.abort_reason and leaked not in detail


class TestPipelineStats:
    EXTRACT_FILE = "GrantsDBExtract20261030v2.xml"

    @pytest.fixture
    def stubbed(self, monkeypatch):
        """The pipeline with every outside dependency replaced: no network, no database."""

        def fake_extract():
            return True

        fake_extract.last_extract_path = Path("/data") / self.EXTRACT_FILE
        monkeypatch.setenv("GRANTS_DATA_SOURCE", "extract")
        monkeypatch.setattr(pipeline, "gen_extract", fake_extract)
        monkeypatch.setattr(pipeline, "process_extract_xml", lambda path: [{"id": 1}, {"id": 2}, {"id": 3}])
        monkeypatch.setattr(pipeline, "load_profile", lambda: Profile(summary="Grid resilience."))
        monkeypatch.setattr(pipeline, "normalize_records", lambda records: records)
        monkeypatch.setattr(pipeline, "date_filter_json_data", lambda records: records)
        monkeypatch.setattr(pipeline, "drop_closed_grants", lambda records: records)
        monkeypatch.setattr(pipeline, "keyword_extractor", lambda profile: ([], 1, True))
        monkeypatch.setattr(pipeline, "filter_grants_by_keywords", lambda records, *args: records[:2])
        monkeypatch.setattr(pipeline, "load_cached", lambda fingerprint: {})
        monkeypatch.setattr(pipeline, "fetch_relevance", lambda fingerprint: {})
        monkeypatch.setattr(pipeline, "save_cached", lambda *args: None)
        monkeypatch.setattr(
            pipeline, "score_records",
            lambda records, profile, already_scored, settings: ScoringOutcome(records=records, enabled=True, scored=1, cached=0, failed=1, deferred=0),
        )
        monkeypatch.setattr(pipeline, "description_summarizer", lambda records: records)
        monkeypatch.setattr(pipeline, "_write_csv", lambda records: Path("grants.csv"))
        monkeypatch.setattr(pipeline, "load_grants_from_records", lambda records: LoadSummary(inserted=2, updated=0, new_ids={"1", "2"}))

    def test_a_clean_run_fills_the_stats(self, stubbed):
        success, kept = pipeline.onlyTheGoodStuff()
        assert success and len(kept) == 2
        assert pipeline.onlyTheGoodStuff.last_stats == dict(
            source="extract", source_file=self.EXTRACT_FILE,
            extract=3, open=3, after_forecast=3, after_agency=3, matched=2,
            scored=1, cached=0, failed=1, deferred=0, inserted=2, updated=0, kept=2,
        )

    def test_the_funnel_only_narrows(self, stubbed, monkeypatch):
        records = [
            {"id": n, "AGENCY_CODE": "XX-1" if n == 4 else "NSF", "OPPORTUNITY_STATUS": "Forecasted" if n == 3 else "Posted"}
            for n in range(1, 8)
        ]
        monkeypatch.setattr(pipeline, "process_extract_xml", lambda path: records)
        monkeypatch.setattr(pipeline, "drop_closed_grants", lambda rows: rows[:6])
        monkeypatch.setattr(pipeline, "keyword_extractor", lambda profile: ([], 1, False))
        monkeypatch.setattr(pipeline, "load_profile", lambda: Profile(summary="Grid resilience.", agencies_exclude=["XX"]))
        assert pipeline.onlyTheGoodStuff()[0] is True
        stats = pipeline.onlyTheGoodStuff.last_stats
        funnel = [stats[key] for key in ("extract", "open", "after_forecast", "after_agency", "matched", "kept")]
        assert funnel == [7, 6, 5, 4, 2, 2]
        assert stats["source"] == "extract" and stats["source_file"] == self.EXTRACT_FILE

    def test_an_empty_stage_stops_the_funnel_there(self, stubbed, monkeypatch):
        monkeypatch.setattr(pipeline, "filter_grants_by_keywords", lambda records, *args: [])
        assert pipeline.onlyTheGoodStuff() == (True, [])
        stats = pipeline.onlyTheGoodStuff.last_stats
        assert (stats["after_agency"], stats["matched"], stats["kept"]) == (3, 0, 0)
        assert "scored" not in stats and stats["reason"] == "No grants matched the profile keywords."

    def test_the_search_export_names_its_file(self, stubbed, monkeypatch):
        def fake_export():
            return True

        fake_export.last_download_path = Path("/data/grants_export_20261030.json")
        monkeypatch.setenv("GRANTS_DATA_SOURCE", "export")
        monkeypatch.setattr(pipeline, "gen_grants", fake_export)
        monkeypatch.setattr(pipeline, "process_json_data", lambda path: [{"id": 1}])
        pipeline.onlyTheGoodStuff()
        assert pipeline.onlyTheGoodStuff.last_stats["source"] == "export"
        assert pipeline.onlyTheGoodStuff.last_stats["source_file"] == "grants_export_20261030.json"

    def test_an_unknown_source_falls_back_to_the_export(self, stubbed, monkeypatch):
        def fake_export():
            return False

        monkeypatch.setenv("GRANTS_DATA_SOURCE", "carrier-pigeon")
        monkeypatch.setattr(pipeline, "gen_grants", fake_export)
        monkeypatch.setattr(pipeline, "get_latest_file_path", lambda: Path("/cache/old_export.json"))
        monkeypatch.setattr(pipeline, "process_json_data", lambda path: [{"id": 1}])
        pipeline.onlyTheGoodStuff()
        assert pipeline.onlyTheGoodStuff.last_stats["source"] == "export"
        assert pipeline.onlyTheGoodStuff.last_stats["source_file"] == "old_export.json"

    def test_a_stopped_scorer_is_carried_into_the_stats(self, stubbed, monkeypatch):
        monkeypatch.setattr(
            pipeline, "score_records",
            lambda records, profile, already_scored, settings: ScoringOutcome(
                records=records, enabled=True, failed=2, abort_reason="cannot reach http://10.0.0.5:18100: Connection error."
            ),
        )
        assert pipeline.onlyTheGoodStuff()[0] is True
        assert pipeline.onlyTheGoodStuff.last_stats["scoring_abort"] == "cannot reach http://10.0.0.5:18100: Connection error."

    def test_no_abort_leaves_the_key_out(self, stubbed):
        pipeline.onlyTheGoodStuff()
        assert "scoring_abort" not in pipeline.onlyTheGoodStuff.last_stats

    def test_stats_are_reset_on_every_call(self, stubbed, monkeypatch):
        pipeline.onlyTheGoodStuff()
        monkeypatch.setattr(pipeline, "process_extract_xml", lambda path: [])
        assert pipeline.onlyTheGoodStuff() == (False, [])
        assert pipeline.onlyTheGoodStuff.last_stats == {
            "source": "extract", "source_file": self.EXTRACT_FILE,
            "extract": 0, "kept": 0, "reason": "Could not download or parse the Grants.gov extract.",
        }

    def test_a_failed_database_load_says_so(self, stubbed, monkeypatch):
        def refuse(records):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(pipeline, "load_grants_from_records", refuse)
        success, _ = pipeline.onlyTheGoodStuff()
        assert not success
        assert pipeline.onlyTheGoodStuff.last_stats["reason"] == "Database load failed: connection refused"

    def test_failed_summaries_say_so(self, stubbed, monkeypatch):
        monkeypatch.setattr(pipeline, "description_summarizer", lambda records: [])
        assert pipeline.onlyTheGoodStuff()[0] is False
        assert pipeline.onlyTheGoodStuff.last_stats["reason"] == "Summarizing descriptions failed."


class TestNotifySummary:
    def _run(self, store, transport=None, **kwargs):
        return run_notify(store=store, transport=transport or CaptureTransport(), today=date(2026, 9, 14), settings=QUIET, **kwargs)

    def test_normal_end(self):
        store = MemoryStore([ME], [_grant("A", 40, score=5)])
        assert self._run(store) == 0
        assert run_notify.last_summary == "sent 1, failed 0, nothing new for 0 of 1 recipients"

    def test_nothing_new(self):
        assert self._run(MemoryStore([ME], [])) == 0
        assert run_notify.last_summary == "sent 0, failed 0, nothing new for 1 of 1 recipients"

    def test_failed_send_counts(self):
        out = CaptureTransport()
        out.failing.add("me@und.edu")
        assert self._run(MemoryStore([ME], [_grant("A", 40, score=5)]), out) == 1
        assert run_notify.last_summary == "sent 0, failed 1, nothing new for 0 of 1 recipients"

    def test_no_recipients(self):
        assert self._run(MemoryStore()) == 0
        assert "no recipients yet" in run_notify.last_summary

    def test_lock_skip(self):
        store = MemoryStore([ME], [_grant("A", 40, score=5)])
        store.locked = True
        assert self._run(store) == 0
        assert "another run is sending" in run_notify.last_summary

    def test_early_error(self):
        store = MemoryStore([ME])

        def broken():
            raise RuntimeError("no database")

        store.ensure_schema = broken
        assert self._run(store) == 1
        assert run_notify.last_summary == "cannot read recipients from the database: no database"

    def test_every_run_replaces_the_last_summary(self):
        run_notify.last_summary = "stale"
        self._run(MemoryStore([ME], []))
        assert run_notify.last_summary != "stale"

    def test_counts_on_the_normal_path(self):
        assert self._run(MemoryStore([ME], [_grant("A", 40, score=5)])) == 0
        assert run_notify.last_counts == {"sent": 1, "failed": 0, "quiet": 0, "recipients": 1}
        assert self._run(MemoryStore([ME], [])) == 0
        assert run_notify.last_counts == {"sent": 0, "failed": 0, "quiet": 1, "recipients": 1}

    def test_failed_sends_are_counted(self):
        out = CaptureTransport()
        out.failing.add("me@und.edu")
        assert self._run(MemoryStore([ME], [_grant("A", 40, score=5)]), out) == 1
        assert run_notify.last_counts == {"sent": 0, "failed": 1, "quiet": 0, "recipients": 1}

    def test_early_exits_have_no_counts(self):
        run_notify.last_counts = {"sent": 9, "failed": 9, "quiet": 9, "recipients": 9}
        assert self._run(MemoryStore()) == 0  # no recipients
        assert run_notify.last_counts is None


class TestSteps:
    @pytest.fixture
    def log(self, monkeypatch):
        entries = []

        @contextmanager
        def fake(step):
            run = runs.Run()
            try:
                yield run
            finally:
                entries.append((step, run.ok, run.detail, run.stats))

        monkeypatch.setattr(main, "recorded", fake)
        monkeypatch.setattr(main, "ensure_schema", lambda: None)
        return entries

    def test_ingest_is_logged_with_its_sentence(self, log, monkeypatch):
        def fake_ingest():
            stats = {"extract": 10, "kept": 2, "inserted": 2, "updated": 0}
            monkeypatch.setattr(main.onlyTheGoodStuff, "last_stats", stats)
            return 0

        monkeypatch.setattr(main, "_ingest", fake_ingest)
        assert main.ingest() == 0
        stats = {"extract": 10, "kept": 2, "inserted": 2, "updated": 0}
        assert log == [("ingest", True, "10 in the extract, 2 kept, 2 new and 0 updated in the database", stats)]

    def test_a_failed_ingest_is_logged_as_failed(self, log, monkeypatch):
        def fake_ingest():
            monkeypatch.setattr(main.onlyTheGoodStuff, "last_stats", {"reason": "Summarizing descriptions failed."})
            return 1

        monkeypatch.setattr(main, "_ingest", fake_ingest)
        assert main.ingest() == 1
        assert log == [("ingest", False, "Summarizing descriptions failed.", {"reason": "Summarizing descriptions failed."})]

    def test_notify_is_logged_with_its_summary(self, log, monkeypatch):
        def fake_notify(dry_run=False):
            fake_notify.last_summary = "sent 1, failed 0, nothing new for 0 of 1 recipients"
            fake_notify.last_counts = {"sent": 1, "failed": 0, "quiet": 0, "recipients": 1}
            return 0

        monkeypatch.setattr(main, "run_notify", fake_notify)
        assert main.main(["notify"]) == 0
        assert log == [(
            "notify", True, "sent 1, failed 0, nothing new for 0 of 1 recipients",
            {"sent": 1, "failed": 0, "quiet": 0, "recipients": 1},
        )]

    def test_a_notify_that_leaves_no_counts_is_still_logged(self, log, monkeypatch):
        def bare_notify(dry_run=False):
            bare_notify.last_summary = "no recipients yet"
            return 0

        monkeypatch.setattr(main, "run_notify", bare_notify)
        assert main.main(["notify"]) == 0
        assert log == [("notify", True, "no recipients yet", None)]

    def test_a_dry_run_is_not_logged(self, log, monkeypatch):
        monkeypatch.setattr(main, "run_notify", lambda dry_run=False: 0)
        assert main.main(["notify", "--dry-run"]) == 0
        assert log == []

    @pytest.fixture
    def order(self, monkeypatch):
        """Events in the order they happen: the schema check and the start of each recorded step."""
        events = []

        @contextmanager
        def fake(step):
            events.append(f"record {step}")
            yield runs.Run()

        monkeypatch.setattr(main, "recorded", fake)
        monkeypatch.setattr(main, "ensure_schema", lambda: events.append("schema"))

        def notify(dry_run=False):
            events.append("notify")
            return notify.code

        notify.code, notify.last_summary = 0, None
        monkeypatch.setattr(main, "run_notify", notify)
        return events

    def test_notify_ensures_the_schema_before_it_records_the_run(self, order):
        assert main.main(["notify"]) == 0
        assert order == ["schema", "record notify", "notify"]

    def test_notify_that_cannot_check_the_schema_still_runs_and_is_recorded(self, order, monkeypatch, capsys):
        def down():
            raise RuntimeError("database is down")

        monkeypatch.setattr(main, "ensure_schema", down)
        main.run_notify.code = 1
        assert main.main(["notify"]) == 1
        assert order == ["record notify", "notify"]
        assert "could not verify database schema: database is down" in capsys.readouterr().out

    def test_a_dry_run_neither_checks_the_schema_nor_is_recorded(self, order):
        assert main.main(["notify", "--dry-run"]) == 0
        assert order == ["notify"]

    def test_the_default_command_logs_both_steps(self, log, monkeypatch):
        monkeypatch.setattr(main, "_ingest", lambda: 0)

        def failing_notify(dry_run=False):
            failing_notify.last_summary = "sent 0, failed 1, nothing new for 0 of 1 recipients"
            return 1

        monkeypatch.setattr(main, "run_notify", failing_notify)
        assert main.main([]) == 1
        assert [(step, ok) for step, ok, *_ in log] == [("ingest", True), ("notify", False)]
