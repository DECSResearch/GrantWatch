"""Flows 1 and 2: `ingest` scores the fixture extract through a model over HTTP into Postgres,
then `notify` writes the team's email to the outbox.

One database and one scripted model for the whole module. The fixtures run each step once
and keep what it did, so a test reads the result of a step and never depends on test order.
"""
from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harness import ADMIN_PASSWORD, ADMIN_USER, MODEL, SCORES, NOT_SENT, new_section, plain_text, read_email
from web import app as webapp

# The order notifications/planner.py gives: grants closing within 30 days first, soonest first (the
# first of them scores lower than the next); then the rest by score, then deadline. The forecast
# scores 4 and closes last, so the cap of 6 holds it back.
EMAILED = [
    ("4/5", "Foundations of Trusted Systems"),
    ("5/5", "Dependable Control Software for Critical Infrastructure"),
    ("5/5", "Cybersecurity Innovation for Research Infrastructure"),
    ("5/5", "Cyber Research & Workforce Education"),
    ("5/5", "Long-term Secure Infrastructure Partnership"),
    ("4/5", "Defense University Research Instrumentation Program (DURIP)"),
]
DURIP = ["DURIP-AFOSR-26", "DURIP-ARMY-26", "DURIP-ONR-26"]


@pytest.fixture(scope="module")
def ingested(world):
    world.mp.setenv("GRANTS_NOTIFY_RECIPIENTS", "lead@und.edu")
    world.mp.setenv("GRANTS_EMAIL_MAX_NEW", "6")
    code = world.ingest()
    return SimpleNamespace(
        code=code,
        requests=list(world.llm.requests),
        grants={row["opp_id"]: row for row in world.query("SELECT * FROM grants")},
        runs=world.query("SELECT * FROM job_runs WHERE step = 'ingest'"),
    )


@pytest.fixture(scope="module")
def dry_run(world, ingested):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = world.notify("--dry-run")
    return SimpleNamespace(
        code=code,
        out=out.getvalue(),
        notify_runs=world.query("SELECT * FROM job_runs WHERE step = 'notify'"),
        notifications=world.query("SELECT * FROM notifications"),
        emails=world.emails(),
    )


@pytest.fixture(scope="module")
def first_email(world, dry_run):
    code = world.notify()
    files = world.emails()
    message = read_email(files[-1])
    return SimpleNamespace(
        code=code,
        files=files,
        message=message,
        text=plain_text(message),
        html=message.get_body(("html",)).get_content(),
        rows=world.query("SELECT opp_id, kind FROM notifications ORDER BY id"),
        runs=world.query("SELECT * FROM job_runs WHERE step = 'notify' ORDER BY id"),
    )


@pytest.fixture(scope="module")
def later_emails(world, first_email):
    codes = [world.notify()]
    after_second = world.emails()
    codes.append(world.notify())
    return SimpleNamespace(
        codes=codes,
        after_second=after_second,
        after_third=world.emails(),
        second=read_email(after_second[-1]),
        runs=world.query("SELECT * FROM job_runs WHERE step = 'notify' ORDER BY id"),
    )


@pytest.fixture(scope="module")
def admin_after_a_vote(world, later_emails):
    """Every program has gone out as new; then one member marks one DURIP posting Not for us, and the admin page is read."""
    world.query("INSERT INTO grant_feedback (opp_id, email, verdict) VALUES ('DURIP-ARMY-26', 'lead@und.edu', -1)")
    client = TestClient(webapp.app, base_url="http://localhost")
    return SimpleNamespace(
        body=client.get("/admin/api", auth=(ADMIN_USER, ADMIN_PASSWORD)).json(),
        recorded=world.query("SELECT COUNT(DISTINCT opp_id) AS n FROM notifications WHERE kind = 'new'")[0]["n"],
    )


class TestIngest:
    def test_the_run_is_recorded_with_its_funnel(self, ingested):
        assert ingested.code == 0
        (run,) = ingested.runs
        assert run["ok"] is True and run["finished_at"] is not None
        assert {key: run["stats"][key] for key in (
            "extract", "open", "after_forecast", "after_agency", "ineligible", "sent_to_model",
            "scored", "cached", "failed", "deferred", "kept", "inserted", "updated",
        )} == {
            "extract": 15, "open": 14, "after_forecast": 14, "after_agency": 14, "ineligible": 1, "sent_to_model": 13,
            "scored": 13, "cached": 0, "failed": 0, "deferred": 0, "kept": 14, "inserted": 14, "updated": 0,
        }
        assert "13 newly scored" in run["detail"] and "scoring_abort" not in run["stats"]
        assert "Relevance scoring with fake-model: scored 13" in run["log"]

    def test_the_precheck_scores_the_states_only_grant_itself(self, ingested, world):
        row = ingested.grants["E2E-STATES"]
        assert row["relevance_score"] == 0 and row["relevance_model"] == "rule"
        assert row["relevance_reason"].startswith("Not open to public university")
        assert "E2E-STATES" not in world.llm.sent()  # never sent to the model, though its text matches the keywords
        assert row["description"]  # and still loaded, so tracking it would keep refreshing

    def test_every_other_open_grant_goes_to_the_model_once_and_is_scored(self, ingested, world):
        sent = world.llm.sent()
        assert sorted(sent) == sorted(SCORES) and len(sent) == 13
        assert not set(NOT_SENT) & set(sent)
        assert "E2E-CLOSED" not in ingested.grants  # closed grants are dropped before scoring and loading
        assert len(ingested.grants) == 14
        for number, score in SCORES.items():
            row = ingested.grants[number]
            assert (row["relevance_score"], row["relevance_model"]) == (score, MODEL), number
            assert row["relevance_reason"] and row["summary"].startswith(number)  # the model's summary replaced the fallback
        assert max(len(request["numbers"]) for request in ingested.requests) <= 3

    def test_keywords_do_not_decide_what_is_scored(self, ingested):
        quiet = ingested.grants["E2E-NOKW"]
        assert not quiet["matched_keywords"] and quiet["relevance_score"] == 5
        assert "trusted" in ingested.grants["E2E-TRUSTED"]["matched_keywords"]

    def test_a_grant_with_no_applicant_codes_is_kept_and_scored(self, ingested):
        assert ingested.grants["E2E-NOCODES"]["relevance_score"] == 3

    def test_the_universities_bullet_past_700_characters_reaches_the_model(self, ingested, world):
        user = world.llm.request_for("E2E-BULLETS")["user"]
        line = next(line for line in user.splitlines() if line.startswith("Eligibility: "))
        assert line.index("Institutions of Higher Education") > 700
        assert "[truncated]" not in line

    def test_the_model_gets_the_prompt_and_the_profile(self, ingested, world):
        request = world.llm.request_for("E2E-TRUSTED")
        assert "If any item covers institutions of higher" in request["system"]
        assert "Applicant type: public university" in request["system"]
        assert "Examples the team marked" not in request["user"]  # nobody has voted yet

    def test_titles_are_unescaped_at_ingest(self, ingested):
        assert ingested.grants["E2E-ENTITY"]["title"] == "Cyber Research & Workforce Education"


class TestNotify:
    def test_the_dry_run_prints_the_email_and_records_nothing(self, dry_run):
        assert dry_run.code == 0
        assert "To: lead@und.edu" in dry_run.out and "New grants that fit (6 of 7)" in dry_run.out
        assert dry_run.notify_runs == [] and dry_run.notifications == [] and dry_run.emails == []

    def test_urgent_grants_come_first_and_the_cap_says_how_many_are_held_back(self, first_email):
        assert first_email.code == 0 and len(first_email.files) == 1
        assert first_email.message["Subject"] == "GrantWatch: 6 new"
        count, entries = new_section(first_email.text)
        assert count == "6 of 7"  # programs, not postings: the three DURIP postings are one
        assert entries == EMAILED
        assert "will follow in the next emails" in first_email.text
        assert "Next-Generation Secure Computing Initiative" not in first_email.text

    def test_a_program_posted_by_three_services_is_one_row(self, first_email):
        assert first_email.text.count("Defense University Research Instrumentation Program") == 1
        (also,) = [line for line in first_email.text.splitlines() if "Also posted by:" in line]
        assert "Army Research Office" in also and "Office of Naval Research" in also
        assert "Air Force Office of Scientific Research" not in also  # that posting is the row itself
        recorded = {row["opp_id"] for row in first_email.rows if row["kind"] == "new"}
        assert set(DURIP) <= recorded  # every posting counts as announced

    def test_placeholder_dates_and_ceilings_are_not_shown(self, first_email):
        for text in (first_email.text, first_email.html):
            assert "2076" not in text and "999,999,999" not in text and "999999999" not in text
        assert "no firm deadline" in first_email.text

    def test_the_footnote_says_what_gets_a_low_score_listed(self, first_email):
        footnote = "Grants below 4/5 are left out unless tracked or marked Relevant by the team."
        assert footnote in first_email.text and footnote in first_email.html

    def test_entities_are_decoded_in_the_text_and_escaped_in_the_html(self, first_email):
        assert "Cyber Research & Workforce Education" in first_email.text
        assert "Cyber Research &amp; Workforce Education" in first_email.html

    def test_the_notify_run_is_recorded(self, first_email):
        (run,) = first_email.runs
        assert run["ok"] is True and run["finished_at"] is not None
        assert run["stats"] == {"sent": 1, "failed": 0, "quiet": 0, "recipients": 1}
        assert run["detail"].startswith("sent 1, failed 0")

    def test_the_grant_held_back_follows_in_the_next_email_and_then_nothing_is_left(self, later_emails):
        assert later_emails.codes == [0, 0]
        assert len(later_emails.after_second) == 2
        assert later_emails.second["Subject"] == "GrantWatch: 1 new"
        text = plain_text(later_emails.second)
        assert new_section(text) == ("1", [("4/5", "Next-Generation Secure Computing Initiative")])
        assert "days left, estimated)" in text  # a forecast's deadline is an estimate, and the email says so
        assert len(later_emails.after_third) == 2  # the third run had nothing new to say
        assert [run["stats"]["sent"] for run in later_emails.runs] == [1, 1, 0]
        assert later_emails.runs[-1]["stats"]["quiet"] == 1


class TestAdminFeedbackShare:
    def test_it_counts_programs_not_the_postings_each_was_recorded_under(self, admin_after_a_vote):
        assert admin_after_a_vote.recorded == 9  # seven programs; the DURIP triple is three rows of notifications
        assert admin_after_a_vote.body["feedback"] == {"votes": 1, "emailed": 7, "declined": 1, "days": 30}
