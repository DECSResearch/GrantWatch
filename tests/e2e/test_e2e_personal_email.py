"""Flow 4: `notify` emails a member from their own scores and votes.

Real Postgres and the scripted model server. The fixtures run each step once and keep what it did, so a
test reads the result of a step and does not depend on the tests around it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main
from grants.members import personal_profile, save_member_profile
from grants.sql_utils import ensure_schema
from harness import DASHBOARD_URL, MODEL, new_section, plain_text, read_email
from llm_utils.relevance import personal_key
from notifications.planner import OwnScore, Personal
from notifications import store as store_module
from notifications.store import PostgresStore
from web import app as webapp

ALICE, BOB, CAROL, DAN, ERIN = "alice@und.edu", "bob@und.edu", "carol@und.edu", "dan@und.edu", "erin@und.edu"
ALICE_TEXT = "I build battery models for grid-scale storage, mostly lithium and sodium chemistries."
ALICE_HAND_TYPED = "I build battery models for grid-scale storage,\nmostly lithium and sodium chemistries.  "  # ALICE_TEXT as typed into psql
BOB_TEXT = "I study wind-energy forecasting and the resilience of rural power grids to storms."


def key(text, model=MODEL):
    """The score key of a profile with this summary, made the way the scoring pass makes it."""
    return personal_key(personal_profile(webapp._profile(), text), model)


def add_score(world, email, opp_id, score, reason, score_key):
    world.query(
        "INSERT INTO member_scores (opp_id, email, score, reason, model, score_key) VALUES (%s, %s, %s, %s, %s, %s)",
        (opp_id, email, score, reason, MODEL, score_key),
    )


def vote(world, email, opp_id, verdict):
    world.query(
        "INSERT INTO grant_feedback (opp_id, email, verdict, title, agency, summary) VALUES (%s, %s, %s, %s, 'NSF', 'Funds things.')",
        (opp_id, email, verdict, f"{opp_id} as marked"),
    )


def emails_since(world, before):
    """The mail the outbox has gained since it held ``before`` files, by the address it was sent to."""
    return {message["To"]: message for message in (read_email(path) for path in world.emails()[before:])}


def notified(world, email):
    """The kinds of notice recorded for this address, oldest first."""
    sql = "SELECT n.opp_id, n.kind FROM notifications n JOIN recipients r ON r.id = n.recipient_id WHERE r.email = %s ORDER BY n.id"
    return [(row["opp_id"], row["kind"]) for row in world.query(sql, (email,))]


@pytest.fixture(scope="module")
def read(world):
    """Alice (typed by hand), Bob, Dan (a text too short to ever be scored) and Erin have profiles; Carol uses the team's.

    Rows are written straight into the tables, some for a key the member no longer has. The store is read, then read
    again under another model, then after Alice's profile text is changed behind the app's back.
    """
    world.reset()
    ensure_schema()
    for email in (ALICE, BOB, CAROL, DAN, ERIN):
        assert main.main(["recipients", "add", email]) == 0
    world.query("INSERT INTO member_profiles (email, summary) VALUES (%s, %s)", (ALICE, ALICE_HAND_TYPED))
    save_member_profile(BOB, BOB_TEXT)
    world.query("INSERT INTO member_profiles (email, summary) VALUES (%s, %s)", (DAN, "too short"))
    save_member_profile(ERIN, BOB_TEXT)
    add_score(world, ALICE, "E2E-BROAD", 5, "Alice fits", key(ALICE_TEXT))
    add_score(world, ALICE, "E2E-NOKW", 4, "scored by another model", key(ALICE_TEXT, "older-model"))
    add_score(world, ALICE, "E2E-TRUSTED", 3, "key made from the text as typed", key(ALICE_HAND_TYPED))
    add_score(world, BOB, "E2E-BROAD", 1, "Bob does not", key(BOB_TEXT))
    add_score(world, DAN, "E2E-BROAD", 5, "scored under another text", key("some other text that was long enough"))
    vote(world, ALICE, "E2E-BROAD", -1)
    vote(world, BOB, "E2E-TRUSTED", 1)
    vote(world, CAROL, "E2E-NOKW", -1)
    vote(world, DAN, "E2E-NOKW", 1)
    world.query("UPDATE recipients SET active = FALSE WHERE email = %s", (ERIN,))
    store = PostgresStore()
    first = store.personal([ALICE, BOB, CAROL, DAN])
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GRANTS_LLM_MODEL", "a-newer-model")
        swapped = store.personal([ALICE])
    world.query("UPDATE member_profiles SET summary = %s WHERE email = %s", (ALICE_TEXT + " I also work on solid-state electrolytes.", ALICE))
    edited = store.personal([ALICE])
    return SimpleNamespace(
        store=store, first=first, swapped=swapped, edited=edited, nobody=store.personal([]),
        recipients={r.email: r for r in store.recipients()},
    )


class TestTheStoreRead:
    def test_a_recipient_with_a_profile_of_their_own_is_marked_and_the_others_are_not(self, read):
        assert {email: r.has_profile for email, r in read.recipients.items()} == {ALICE: True, BOB: True, CAROL: False, DAN: True}

    def test_an_unsubscribed_member_is_not_a_recipient(self, read):
        assert ERIN not in read.recipients

    def test_every_score_stored_for_a_member_counts_whatever_key_it_was_made_under(self, read):
        assert read.first[ALICE].scores == {
            "E2E-BROAD": OwnScore(5, "Alice fits"), "E2E-NOKW": OwnScore(4, "scored by another model"),
            "E2E-TRUSTED": OwnScore(3, "key made from the text as typed"),
        }
        assert read.first[BOB].scores == {"E2E-BROAD": OwnScore(1, "Bob does not")}

    def test_a_member_is_read_with_their_own_votes_only(self, read):
        assert read.first[ALICE].votes == {"E2E-BROAD": -1}
        assert read.first[BOB].votes == {"E2E-TRUSTED": 1}

    def test_the_votes_are_read_by_the_function_that_also_gives_their_scorer_its_examples(self, read, monkeypatch):
        # Resolution 19 names fetch_member_votes for both, so what counts as a member's own vote is defined once.
        monkeypatch.setattr(store_module, "fetch_member_votes", lambda emails: [{"email": ALICE, "opp_id": "E2E-NOKW", "verdict": 1}])
        assert read.store.personal([ALICE])[ALICE].votes == {"E2E-NOKW": 1}

    def test_an_address_with_no_profile_is_left_out(self, read):
        assert CAROL not in read.first and set(read.first) == {ALICE, BOB, DAN}
        assert read.nobody == {}

    def test_the_rows_of_a_member_whose_text_cannot_be_cleaned_are_read_as_they_are(self, read):
        assert read.first[DAN] == Personal(scores={"E2E-BROAD": OwnScore(5, "scored under another text")}, votes={"E2E-NOKW": 1})

    def test_a_model_swap_leaves_the_scores_a_member_sees_as_they_were(self, read):
        assert read.swapped[ALICE] == read.first[ALICE]

    def test_a_profile_changed_behind_the_apps_back_keeps_its_old_scores_until_they_are_rescored(self, read):
        assert read.edited[ALICE] == read.first[ALICE]


# What the model scripts for a member's profile (the rest keep the team's scores; see harness.FakeLLM):
# Alice rates the grant the team calls too broad highly, two it likes not at all, and the DURIP posting of
# the Army above the other two. Bob rates a grant the team finds middling highly.
ALICE_SCORES = {"E2E-BROAD": 5, "E2E-NOKW": 0, "E2E-TRUSTED": 0, "DURIP-ARMY-26": 5}
BOB_SCORES = {"E2E-NOCODES": 5}
DURIP = "Defense University Research Instrumentation Program (DURIP)"
FORECAST = "Next-Generation Secure Computing Initiative"
TITLES = {
    "E2E-TRUSTED": "Foundations of Trusted Systems", "E2E-NOKW": "Dependable Control Software for Critical Infrastructure",
    "E2E-BULLETS": "Cybersecurity Innovation for Research Infrastructure", "E2E-ENTITY": "Cyber Research & Workforce Education",
    "E2E-PLACEHOLDER": "Long-term Secure Infrastructure Partnership", "E2E-BROAD": "Young Faculty Awards (YFA)",
    "E2E-NOCODES": "Rural Broadband Network Planning",
}


ALICE_LIST = sorted([
    ("5/5", TITLES["E2E-BULLETS"]), ("5/5", TITLES["E2E-PLACEHOLDER"]), ("5/5", DURIP),
    ("4/5", FORECAST), ("5/5", TITLES["E2E-BROAD"]),
])  # what her scores make of the fixture extract, when she marked E2E-ENTITY Not for us


def listed(message):
    """(score, title) of each new grant in the email, as the email lists them."""
    return sorted(new_section(plain_text(message))[1])


@pytest.fixture(scope="module")
def emailed(world):
    """Alice (her profile typed by hand), Bob and Carol (the team's); one ingest, one notify.

    Alice marked a grant Not for us and Carol another, and a score is stored for Alice on the grant the eligibility
    pre-check ruled out.
    """
    world.reset()
    ensure_schema()
    for email in (ALICE, BOB, CAROL):
        assert main.main(["recipients", "add", email]) == 0
    world.query("INSERT INTO member_profiles (email, summary) VALUES (%s, %s)", (ALICE, ALICE_HAND_TYPED))
    save_member_profile(BOB, BOB_TEXT)
    world.llm.profile_scores = {"battery": ALICE_SCORES, "wind-energy": BOB_SCORES}
    vote(world, ALICE, "E2E-ENTITY", -1)
    vote(world, CAROL, "E2E-BULLETS", -1)
    code = world.ingest()
    add_score(world, ALICE, "E2E-STATES", 5, "scored before the rule applied", key(ALICE_TEXT))
    status = world.notify()
    mails = emails_since(world, 0)
    return SimpleNamespace(
        code=code, status=status, mails=mails, texts={to: plain_text(m) for to, m in mails.items()},
        htmls={to: m.get_body(("html",)).get_content() for to, m in mails.items()},
        rows={email: notified(world, email) for email in (ALICE, BOB, CAROL)},
    )


class TestEachRecipientsEmail:
    def test_everyone_is_emailed_and_the_run_succeeds(self, emailed):
        assert (emailed.code, emailed.status) == (0, 0) and set(emailed.mails) == {ALICE, BOB, CAROL}

    def test_a_team_recipient_gets_the_teams_list(self, emailed):
        assert listed(emailed.mails[CAROL]) == sorted([
            ("4/5", TITLES["E2E-TRUSTED"]), ("5/5", TITLES["E2E-NOKW"]), ("5/5", TITLES["E2E-ENTITY"]),
            ("5/5", TITLES["E2E-PLACEHOLDER"]), ("4/5", DURIP), ("4/5", FORECAST),
        ])

    def test_alice_gets_her_own_scores_though_her_profile_was_typed_by_hand(self, emailed):
        assert listed(emailed.mails[ALICE]) == ALICE_LIST

    def test_bob_gets_his(self, emailed):
        assert listed(emailed.mails[BOB]) == sorted([
            ("4/5", TITLES["E2E-TRUSTED"]), ("5/5", TITLES["E2E-NOKW"]), ("5/5", TITLES["E2E-BULLETS"]), ("5/5", TITLES["E2E-ENTITY"]),
            ("5/5", TITLES["E2E-PLACEHOLDER"]), ("4/5", DURIP), ("4/5", FORECAST), ("5/5", TITLES["E2E-NOCODES"]),
        ])

    def test_a_grant_the_rules_ruled_out_is_not_announced_to_a_member_with_a_score_stored_for_it(self, emailed):
        assert "State and Local Cybersecurity Grant Program" not in emailed.texts[ALICE]

    def test_a_members_mark_is_theirs_alone_and_the_teams_is_not_theirs(self, emailed):
        entity, bullets = TITLES["E2E-ENTITY"], TITLES["E2E-BULLETS"]
        titles = {to: [title for _, title in listed(m)] for to, m in emailed.mails.items()}
        assert entity not in titles[ALICE] and entity in titles[BOB] and entity in titles[CAROL]  # Alice's Not for us hides it from her only
        assert bullets not in titles[CAROL] and bullets in titles[ALICE] and bullets in titles[BOB]  # Carol's, from the team's list only

    def test_a_program_is_led_by_the_posting_a_members_scores_prefer_and_every_posting_is_recorded(self, emailed):
        def also(to):
            (line,) = [line for line in emailed.texts[to].splitlines() if "Also posted by:" in line]
            return line

        assert "Army Research Office" not in also(ALICE) and "Air Force Office of Scientific Research" in also(ALICE)  # Alice's score leads with the Army's
        assert "Air Force Office of Scientific Research" not in also(CAROL) and "Army Research Office" in also(CAROL)  # the team's leads with the Air Force's
        for email in (ALICE, BOB, CAROL):
            durip = sorted(opp for opp, kind in emailed.rows[email] if kind == "new" and opp.startswith("DURIP"))
            assert durip == ["DURIP-AFOSR-26", "DURIP-ARMY-26", "DURIP-ONR-26"]

    def test_the_footer_says_whose_scores_they_are(self, emailed):
        own = "Scores are for your own research profile. Grants below 4/5 are left out unless tracked or you marked them Relevant."
        team = "Scores are relative to the research profile. Grants below 4/5 are left out unless tracked or marked Relevant by the team."
        invite = f"Want matches for your own research? Write your profile on the dashboard: {DASHBOARD_URL}."
        for email in (ALICE, BOB):
            assert own in emailed.texts[email] and own in emailed.htmls[email]
            assert team not in emailed.texts[email] and "Want matches" not in emailed.texts[email]
        assert team in emailed.texts[CAROL] and invite in emailed.texts[CAROL]
        assert f'<a href="{DASHBOARD_URL}"' in emailed.htmls[CAROL] and own not in emailed.texts[CAROL]


@pytest.fixture(scope="module")
def stale(world):
    """Alice (typed by hand) and Carol, scored by one ingest; then the model is swapped and notify runs.

    Every score of Alice's is then for a key she no longer has, as after a model swap, a new SCORER_VERSION or a
    change of institution before the personal step has run again. The mail must go out from those scores.
    """
    world.reset()
    ensure_schema()
    for email in (ALICE, CAROL):
        assert main.main(["recipients", "add", email]) == 0
    world.query("INSERT INTO member_profiles (email, summary) VALUES (%s, %s)", (ALICE, ALICE_HAND_TYPED))
    world.llm.profile_scores = {"battery": ALICE_SCORES}
    vote(world, ALICE, "E2E-ENTITY", -1)
    code = world.ingest()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GRANTS_LLM_MODEL", "a-newer-model")
        status = world.notify()
    mails = emails_since(world, 0)
    keys = {row["score_key"] for row in world.query("SELECT DISTINCT score_key FROM member_scores WHERE email = %s", (ALICE,))}
    return SimpleNamespace(
        code=code, status=status, mails=mails, texts={to: plain_text(m) for to, m in mails.items()}, keys=keys,
        rows=notified(world, ALICE),
    )


class TestAMemberWhoseScoresAreForAnOlderKey:
    """The design shows an old score until it is rescored, as the team's are (and the issue: the team's scores only while they have none)."""

    def test_the_scores_really_are_for_a_key_she_no_longer_has(self, stale):
        assert (stale.code, stale.status) == (0, 0) and set(stale.mails) == {ALICE, CAROL}
        assert stale.keys and key(ALICE_TEXT, "a-newer-model") not in stale.keys and key(ALICE_HAND_TYPED, "a-newer-model") not in stale.keys

    def test_she_still_gets_her_own_list_and_the_own_footer(self, stale):
        assert listed(stale.mails[ALICE]) == ALICE_LIST
        assert "Scores are for your own research profile." in stale.texts[ALICE]

    def test_she_is_not_told_her_matches_have_not_started(self, stale):
        assert "start after the next daily run" not in stale.texts[ALICE]

    def test_what_is_recorded_for_her_follows_her_scores_and_not_the_teams(self, stale):
        told = {opp for opp, kind in stale.rows if kind == "new"}
        assert "E2E-BROAD" in told and "E2E-NOKW" not in told  # she scored the one high and the other 0; the team the reverse
        assert "E2E-ENTITY" not in told  # her own Not for us


@pytest.fixture(scope="module")
def pending(world):
    """Carol and Dan; Dan writes a profile only after the ingest, so nothing is scored for it when the mail goes out."""
    world.reset()
    ensure_schema()
    for email in (CAROL, DAN):
        assert main.main(["recipients", "add", email]) == 0
    code = world.ingest()
    save_member_profile(DAN, BOB_TEXT)
    vote(world, DAN, "E2E-NOKW", -1)
    status = world.notify()
    mails = emails_since(world, 0)
    return SimpleNamespace(code=code, status=status, mails=mails, texts={to: plain_text(m) for to, m in mails.items()})


class TestAMemberWhoseProfileIsNotScoredYet:
    def test_they_get_the_teams_scores_with_their_own_vote_and_the_team_is_not_moved_by_it(self, pending):
        assert (pending.code, pending.status) == (0, 0) and set(pending.mails) == {CAROL, DAN}
        nokw = TITLES["E2E-NOKW"]
        assert nokw in [title for _, title in listed(pending.mails[CAROL])] and nokw not in [title for _, title in listed(pending.mails[DAN])]
        carol = [entry for entry in listed(pending.mails[CAROL]) if entry[1] != nokw]
        assert listed(pending.mails[DAN]) == carol  # the same scores, less the grant Dan said Not for us to

    def test_the_footer_says_their_own_matches_come_after_the_next_run(self, pending):
        text = pending.texts[DAN]
        assert "Your own matches start after the next daily run. Until then the scores are the team's." in text
        assert "unless tracked or marked Relevant by you." in text and "Want matches" not in text
        assert "start after the next daily run" not in pending.texts[CAROL] and "Want matches" in pending.texts[CAROL]


@pytest.fixture(scope="module")
def unread(world):
    """Alice and Carol, a tracked grant due soon, and a notify run in which Alice's own data cannot be read; then one in which it can."""
    world.reset()
    ensure_schema()
    for email in (ALICE, CAROL):
        assert main.main(["recipients", "add", email]) == 0
    save_member_profile(ALICE, ALICE_TEXT)
    world.llm.profile_scores = {"battery": ALICE_SCORES}
    assert world.ingest() == 0
    world.query("INSERT INTO grant_watchlist (opp_id) VALUES ('E2E-TRUSTED')")

    def failing(self, emails):
        raise RuntimeError("could not read member_scores")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(PostgresStore, "personal", failing)
        broken_status = world.notify()
    broken = emails_since(world, 0)
    broken_rows = {email: notified(world, email) for email in (ALICE, CAROL)}
    files = len(world.emails())
    fixed_status = world.notify()
    fixed = emails_since(world, files)
    return SimpleNamespace(
        broken=broken, broken_status=broken_status, broken_rows=broken_rows, fixed=fixed, fixed_status=fixed_status,
        rows={email: notified(world, email) for email in (ALICE, CAROL)},
    )


class TestWhenAMembersOwnDataCannotBeRead:
    def test_the_run_still_succeeds_and_both_recipients_are_emailed(self, unread):
        assert unread.broken_status == 0 and set(unread.broken) == {ALICE, CAROL}

    def test_the_member_gets_only_the_reminder_and_the_team_gets_everything(self, unread):
        alice, carol = plain_text(unread.broken[ALICE]), plain_text(unread.broken[CAROL])
        assert "Deadline reminders (1)" in alice and "New grants that fit" not in alice and "[unscored] Foundations of Trusted Systems" in alice
        assert "Your own matches could not be read this time" in alice
        assert "Deadline reminders (1)" in carol and "New grants that fit" in carol

    def test_nothing_is_recorded_for_the_members_new_grants_or_overview(self, unread):
        assert {kind for _, kind in unread.broken_rows[ALICE]} == {"reminder"}
        assert {"new", "overview"} <= {kind for _, kind in unread.broken_rows[CAROL]}

    def test_the_next_run_with_the_read_working_sends_the_member_what_they_missed_and_the_team_nothing(self, unread):
        assert unread.fixed_status == 0 and set(unread.fixed) == {ALICE}
        assert [title for _, title in listed(unread.fixed[ALICE])] and TITLES["E2E-BROAD"] in [title for _, title in listed(unread.fixed[ALICE])]
        assert {"new", "overview"} <= {kind for _, kind in unread.rows[ALICE]}
