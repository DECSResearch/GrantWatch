"""The personal step in main.py: where it runs, what it records, and that it can never fail anything else."""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import main
from grants import personal, runs
from grants.personal import Totals
from test_personal import TEAM, record


class TestMainStep:
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
        monkeypatch.setenv("GRANTS_LLM_MODEL", "model-one")
        monkeypatch.setattr(main.onlyTheGoodStuff, "last_profile", TEAM)
        monkeypatch.setattr(main.onlyTheGoodStuff, "last_stats", {})
        monkeypatch.setattr(main.onlyTheGoodStuff, "last_candidates", [record(1)])
        return entries

    def test_the_totals_go_in_the_runs_stats_under_personal(self, log, monkeypatch):
        monkeypatch.setattr(personal, "run", lambda candidates, team, settings, started: Totals(people=2, scored=9, failed=1, pending=5, stopped="time limit"))
        main.personal_step(0.0, ingested=True)
        assert log == [("personal", True, "2 members: 9 newly scored (1 failed), 5 still pending. Stopped: time limit.",
                        {"personal": {"people": 2, "scored": 9, "failed": 1, "pending": 5, "stopped": "time limit"}})]

    def test_a_skipped_step_is_recorded_with_its_reason(self, log, monkeypatch):
        monkeypatch.setattr(personal, "run", lambda *a: pytest.fail("skipped"))
        main.personal_step(0.0, ingested=False)
        monkeypatch.delenv("GRANTS_LLM_MODEL")
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        main.personal_step(0.0, ingested=True)
        assert [(e[1], e[2], e[3]["personal"]["stopped"]) for e in log] == [
            (True, "Skipped: ingest did not finish.", "skipped: ingest did not finish"),
            (True, "Skipped: no model is configured (GRANTS_LLM_MODEL).", "skipped: no model is configured (GRANTS_LLM_MODEL)"),
        ]
        assert log[0][3]["personal"] == {"people": 0, "scored": 0, "failed": 0, "pending": 0, "stopped": "skipped: ingest did not finish"}

    def test_it_runs_with_the_candidates_and_profile_ingest_left_and_the_process_start(self, log, monkeypatch):
        seen = {}
        monkeypatch.setattr(personal, "run", lambda candidates, team, settings, started: seen.update(candidates=candidates, team=team, started=started, model=settings.model) or Totals())
        main.personal_step(123.0, ingested=True)
        assert seen == {"candidates": [record(1)], "team": TEAM, "started": 123.0, "model": "model-one"}

    @given(st.sampled_from([RuntimeError("boom"), ValueError("bad"), OSError("disk"), KeyError("k"), ZeroDivisionError()]))
    @settings(max_examples=5, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_whatever_goes_wrong_in_it_stays_in_it(self, log, error):
        with pytest.MonkeyPatch.context() as mp:
            def run(*a):
                raise error

            mp.setattr(personal, "run", run)
            assert main.personal_step(0.0, ingested=True) is None  # no exception reaches the caller
        assert log[-1][0] == "personal"

    def test_ingests_exit_code_is_the_exit_code_whatever_the_personal_step_does(self, log, monkeypatch):
        monkeypatch.setattr(main, "_ingest", lambda: 0)
        monkeypatch.setattr(personal, "run", lambda *a: (_ for _ in ()).throw(RuntimeError("model gone")))
        assert main.main(["ingest"]) == 0
        monkeypatch.setattr(main, "_ingest", lambda: 1)
        assert main.main(["ingest"]) == 1

    def test_notify_still_runs_after_a_personal_step_that_raised(self, log, monkeypatch):
        sent = []
        monkeypatch.setattr(main, "_ingest", lambda: 0)
        monkeypatch.setattr(personal, "run", lambda *a: (_ for _ in ()).throw(RuntimeError("model gone")))
        monkeypatch.setattr(main, "run_notify", lambda dry_run=False: sent.append("notify") or 0)
        main.run_notify.last_summary = None
        assert main.main([]) == 0 and sent == ["notify"]


class TestOrder:
    @pytest.fixture
    def events(self, monkeypatch):
        events = []

        @contextmanager
        def fake(step):
            events.append(f"record {step}")
            yield runs.Run()

        monkeypatch.setattr(main, "recorded", fake)
        monkeypatch.setattr(main, "ensure_schema", lambda: None)
        monkeypatch.setattr(main, "_ingest", lambda: events.append("ingest") or 0)
        monkeypatch.setattr(main, "personal_step", lambda started, ingested: events.append(f"personal {ingested}"))

        def notify(dry_run=False):
            events.append("notify")
            return 0

        notify.last_summary = None
        monkeypatch.setattr(main, "run_notify", notify)
        return events

    def test_the_default_command_goes_ingest_personal_notify(self, events):
        assert main.main([]) == 0
        assert events == ["record ingest", "ingest", "personal True", "record notify", "notify"]

    def test_the_ingest_command_scores_the_members_too_so_a_new_profile_can_be_scored_now(self, events):
        assert main.main(["ingest"]) == 0
        assert events == ["record ingest", "ingest", "personal True"]

    def test_a_failed_ingest_still_reaches_the_personal_step_which_skips_and_then_notify(self, events, monkeypatch):
        monkeypatch.setattr(main, "_ingest", lambda: events.append("ingest") or 1)
        assert main.main([]) == 1
        assert events == ["record ingest", "ingest", "personal False", "record notify", "notify"]

    def test_notify_alone_does_not_touch_the_members(self, events):
        assert main.main(["notify"]) == 0
        assert events == ["record notify", "notify"]

    def test_the_candidates_of_an_earlier_run_are_cleared_before_ingest(self, events, monkeypatch):
        monkeypatch.setattr(main.onlyTheGoodStuff, "last_candidates", [record(1)])
        seen = {}
        monkeypatch.setattr(main, "_ingest", lambda: seen.update(candidates=main.onlyTheGoodStuff.last_candidates) or 0)
        main.main(["ingest"])
        assert seen == {"candidates": None}
