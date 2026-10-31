"""Flow 6: a member writes a profile on the dashboard, and the daily run, the list and the email follow.

One story, told through the routes a person uses (`/api/me/profile`, `/api/feedback`, `/api/grants`) with
`ingest` and `notify` run between the steps, over a real Postgres and the scripted model server. Ana
writes a profile of her own and Cara stays on the team's. The classes below run in the order written:
each fixture takes the database where the one before left it (day 0, her profile, day 1, the email,
an edit, a time stop, a model outage, the day after it, and last her profile removed). The tests read what
a fixture saw and do not depend on one another.

The per-step modules (test_e2e_personal, test_e2e_personal_email, test_e2e_web_profile) take the parts
apart with rows written straight into the tables; this one shows they fit together.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from grants.members import personal_profile
from harness import DASHBOARD_URL, MODEL, SCORES, closed_port, new_section, plain_text, read_email, synopsis
from llm_utils.relevance import personal_key, score_key
from web import app as webapp

ANA, CARA = "ana@und.edu", "cara@und.edu"
ANA_TYPED = "  I build battery models for grid-scale storage,\n\tmostly lithium and sodium chemistries.  "  # as a textarea sends it
ANA_TEXT = "I build battery models for grid-scale storage, mostly lithium and sodium chemistries."
ANA_EDIT = "I study grid-scale storage and, lately, the cyber security of the controllers that run it."
ANA_THIRD = "I model hurricane damage to coastal power networks and how fast they can be restored."
# What the scripted model makes of each text, by a word in it (the rest keep the team's scores, see harness.FakeLLM).
ANA_SCORES = {"lithium": {"E2E-BROAD": 5, "E2E-TRUSTED": 0, "E2E-NOCODES": 5}, "controllers": {"E2E-TRUSTED": 5, "E2E-BROAD": 0}, "hurricane": {"E2E-BROAD": 4}}
SENT = sorted(SCORES)  # the grants the team pass sends to the model: 13
NEW, LATE, LAST = "E2E-NEW", "E2E-LATE", "E2E-LAST"  # grants that arrive on day 1, after the outage and last
NOKW = "- Dependable Control Software for Critical Infrastructure (Department of Energy Office of Science): E2E-NOKW funds the group's research."
BULLETS = "- Cybersecurity Innovation for Research Infrastructure (National Science Foundation): E2E-BULLETS funds the group's research."
OWN = "Scores are for your own research profile. Grants below 4/5 are left out unless tracked or you marked them Relevant."
TEAM = "Scores are relative to the research profile. Grants below 4/5 are left out unless tracked or marked Relevant by the team."
INVITE = f"Want matches for your own research? Write your profile on the dashboard: {DASHBOARD_URL}."


def key_of(text):
    return personal_key(personal_profile(webapp._profile(), text), MODEL)


def member_scores(world, email=None):
    sql = "SELECT * FROM member_scores" + (" WHERE email = %s" if email else "") + " ORDER BY email, opp_id"
    return world.query(sql, (email,) if email else ())


def personal_run(world):
    return world.query("SELECT * FROM job_runs WHERE step = 'personal' ORDER BY id DESC LIMIT 1")[0]


def listing(client, **params):
    path = "/api/grants?" + "&".join(f"{k}={v}" for k, v in {"limit": 1000, **params}.items())
    return client.get(path).json()


def row(client, opp_id, **params):
    return {r["opp_id"]: r for r in listing(client, **params)["results"]}[opp_id]


def at_their_floor(client):
    """The list as the page asks for it: at the floor `/api/profile` gives the member."""
    return listing(client, min_score=client.get("/api/profile").json()["me"]["min_score"])


def examples(request):
    return [line for line in request["user"].splitlines() if line.startswith(("+ ", "- "))]


def mails_since(world, before):
    return {message["To"]: message for message in (read_email(path) for path in world.emails()[before:])}


def new_grants(message):
    """(score, title) of each grant under "New grants that fit", sorted."""
    return sorted(new_section(plain_text(message))[1])


def entries(titles, scores):
    """What an email lists for ``{opp_id: score}``, as `new_grants` gives it."""
    return sorted((f"{score}/5", titles[opp_id]) for opp_id, score in scores.items())


@pytest.fixture(scope="module")
def day_zero(world):
    """Day 0: the team's scores are loaded and nobody has a profile of their own. Nobody is emailed."""
    world.reset()
    world.llm.profile_scores = ANA_SCORES
    world.llm.scores[NEW] = world.llm.scores[LATE] = world.llm.scores[LAST] = 4
    assert world.ingest() == 0
    s = SimpleNamespace(
        requests=list(world.llm.requests), grants={r["opp_id"]: r for r in world.query("SELECT * FROM grants")},
        runs=[r["step"] for r in world.query("SELECT step FROM job_runs ORDER BY id")],
    )
    s.titles = {opp_id: r["title"] for opp_id, r in s.grants.items()}
    world.llm.requests.clear()
    return s


@pytest.fixture(scope="module")
def written(world, day_zero):
    """Both sign in. Ana and Cara each mark a grant Not for us; Ana then writes her profile. Nothing has been scored for it yet."""
    ana, cara = (TestClient(webapp.app, base_url="http://localhost") for _ in range(2))
    world.sign_in(ana, ANA)
    world.sign_in(cara, CARA)
    s = SimpleNamespace(ana=ana, cara=cara)
    s.before = ana.get("/api/me/profile").json()
    s.votes = [ana.post("/api/feedback", json={"opp_id": "E2E-NOKW", "verdict": -1}), cara.post("/api/feedback", json={"opp_id": "E2E-BULLETS", "verdict": -1})]
    s.cara_before = row(cara, "E2E-NOKW")  # Ana has no profile yet, so her mark is the team's
    s.put = ana.put("/api/me/profile", json={"summary": ANA_TYPED})
    s.read_reply = ana.get("/api/me/profile")
    s.read = s.read_reply.json()
    s.stored = world.query("SELECT summary FROM member_profiles")
    s.me = ana.get("/api/profile").json()["me"]
    s.ana_list = listing(ana)
    s.cara_after = row(cara, "E2E-NOKW")  # and now it is hers alone
    s.cara_list = listing(cara)
    return s


@pytest.fixture(scope="module")
def day_one(world, written):
    """Day 1 brings a grant the team has not scored. `ingest` scores it for the team, and every grant for Ana."""
    world.extra = synopsis(NEW, "A Brand New Network Authentication Program", oid=777001, close=40)
    code = world.ingest()
    s = SimpleNamespace(
        code=code, requests=list(world.llm.requests), run=personal_run(world), scores=member_scores(world),
        grants={r["opp_id"]: r for r in world.query("SELECT * FROM grants")}, runs=[r["step"] for r in world.query("SELECT step FROM job_runs ORDER BY id")],
    )
    ana, cara = written.ana, written.cara
    s.me = ana.get("/api/profile").json()
    s.ana_list = at_their_floor(ana)
    s.ana_all = listing(ana)
    s.cara_list = at_their_floor(cara)
    s.cara_all = listing(cara)
    s.cara_me = cara.get("/api/profile").json()["me"]
    s.detail = {"ana": ana.get("/api/grants/E2E-BROAD").json(), "cara": cara.get("/api/grants/E2E-BROAD").json()}
    return s


@pytest.fixture(scope="module")
def emailed(world, day_one):
    code = world.notify()
    mails = mails_since(world, 0)
    return SimpleNamespace(code=code, mails=mails, texts={to: plain_text(m) for to, m in mails.items()}, htmls={to: m.get_body(("html",)).get_content() for to, m in mails.items()})


@pytest.fixture(scope="module")
def edited(world, written, day_one, emailed):
    """Ana changes her text: her scores go at once, the next run rescores her only, and the email is for her alone."""
    ana, cara = written.ana, written.cara
    world.llm.requests.clear()
    s = SimpleNamespace(put=ana.put("/api/me/profile", json={"summary": ANA_EDIT}))
    s.after_put = member_scores(world, ANA)
    s.me_after_put = ana.get("/api/profile").json()["me"]
    s.list_after_put = listing(ana)["scores"]
    s.code = world.ingest()
    s.requests, s.run, s.scores = list(world.llm.requests), personal_run(world), member_scores(world)
    s.me = ana.get("/api/profile").json()["me"]
    s.list = listing(ana)
    s.cara_list = listing(cara)
    before = len(world.emails())
    s.status = world.notify()
    s.mails = mails_since(world, before)
    return s


@pytest.fixture(scope="module")
def stopped(world, written, edited):
    """A third text with a time stop of zero minutes: nothing is scored and all of it is pending. The next run, with the default, finishes it."""
    ana = written.ana
    ana.put("/api/me/profile", json={"summary": ANA_THIRD})
    world.llm.requests.clear()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GRANTS_PERSONAL_STOP_MINUTES", "0")
        stop = SimpleNamespace(code=world.ingest(), requests=list(world.llm.requests), run=personal_run(world), scores=member_scores(world, ANA))
    stop.me, stop.list = ana.get("/api/profile").json()["me"], listing(ana)["scores"]
    world.llm.requests.clear()
    done = SimpleNamespace(code=world.ingest(), requests=list(world.llm.requests), run=personal_run(world), scores=member_scores(world, ANA))
    done.me, done.list = ana.get("/api/profile").json()["me"], listing(ana)["scores"]
    return SimpleNamespace(stop=stop, done=done)


@pytest.fixture(scope="module")
def outage(world, written, stopped):
    """The model server cannot be reached on a day with a new grant, one grant is tracked, and both get their email."""
    assert written.cara.post("/api/watchlist", json={"opp_id": "E2E-TRUSTED"}).status_code == 201
    world.extra += synopsis(LATE, "A Late Network Authentication Program", oid=777002, close=45)
    world.llm.requests.clear()
    before = len(world.emails())
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GRANTS_LLM_BASE_URL", f"http://127.0.0.1:{closed_port()}/v1")
        s = SimpleNamespace(code=world.ingest(), status=world.notify())
    s.requests, s.run, s.scores = list(world.llm.requests), personal_run(world), member_scores(world, ANA)
    s.loaded = world.query("SELECT opp_id, relevance_score FROM grants WHERE opp_id = %s", (LATE,))
    s.mails = mails_since(world, before)
    s.texts = {to: plain_text(m) for to, m in s.mails.items()}
    s.late = {"ana": row(written.ana, LATE), "cara": row(written.cara, LATE)}
    return s


@pytest.fixture(scope="module")
def recovered(world, written, outage):
    """The model is back: the next run scores the grant for the team and for Ana, and the email announces it to both."""
    before = len(world.emails())
    s = SimpleNamespace(code=world.ingest(), requests=list(world.llm.requests), run=personal_run(world), scores=member_scores(world, ANA))
    s.status = world.notify()
    s.mails = mails_since(world, before)
    return s


@pytest.fixture(scope="module")
def removed(world, written, recovered):
    """Ana goes back to the team profile; a new grant arrives, is scored for the team only, and is emailed."""
    ana, cara = written.ana, written.cara
    s = SimpleNamespace(delete=ana.delete("/api/me/profile"))
    s.after = world.query("SELECT (SELECT COUNT(*) FROM member_profiles) AS profiles, (SELECT COUNT(*) FROM member_scores) AS scores")[0]
    s.me, s.list = ana.get("/api/profile").json()["me"], listing(ana)
    s.cara_nokw = row(cara, "E2E-NOKW")
    world.llm.requests.clear()
    s.idle_code, s.idle_requests = world.ingest(), list(world.llm.requests)  # nothing is new: nothing is asked
    world.extra += synopsis(LAST, "A Last Network Authentication Program", oid=777003, close=50)
    world.llm.requests.clear()
    before = len(world.emails())
    s.code, s.requests, s.run = world.ingest(), list(world.llm.requests), personal_run(world)
    s.status = world.notify()
    s.mails = mails_since(world, before)
    s.texts = {to: plain_text(m) for to, m in s.mails.items()}
    return s


class TestWritingTheProfile:
    def test_a_member_starts_on_the_team_profile(self, written):
        assert written.before == {"has_profile": False, "summary": None, "updated_at": None}

    def test_the_text_is_stored_cleaned_and_read_back(self, written):
        assert written.put.status_code == 200 and written.read["summary"] == ANA_TEXT and written.read["updated_at"]
        assert [r["summary"] for r in written.stored] == [ANA_TEXT]

    def test_what_a_member_is_told_is_not_kept_by_a_cache(self, written):
        for reply in (written.put, written.read_reply):
            assert reply.headers["cache-control"] == "no-store" and "cookie" in reply.headers["vary"].lower()

    def test_with_nothing_scored_for_it_yet_they_see_the_teams_scores(self, written, day_zero):
        assert written.me["has_profile"] is True and written.me["using"] == "team" and written.ana_list["scores"] == "team"
        shown = {r["opp_id"]: r["relevance_score"] for r in written.ana_list["results"]}
        assert shown and shown == {opp: day_zero.grants[opp]["relevance_score"] for opp in shown}

    def test_their_not_for_us_counts_for_the_team_only_until_they_have_a_profile(self, written):
        assert [reply.status_code for reply in written.votes] == [200, 200]
        assert (written.cara_before["votes_down"], written.cara_before["team_verdict"], written.cara_before["hidden"]) == (1, -1, True)
        assert (written.cara_after["votes_down"], written.cara_after["team_verdict"], written.cara_after["hidden"]) == (0, 0, False)

    def test_the_teammate_is_untouched(self, written):
        assert written.cara_list["scores"] == "team"


class TestTheDailyRun:
    def test_the_run_is_recorded_and_scores_every_grant_for_her_and_no_one_else(self, day_one, day_zero):
        assert day_one.code == 0 and day_one.runs == day_zero.runs + ["ingest", "personal"]
        assert day_one.run["ok"] is True
        assert day_one.run["stats"] == {"personal": {"people": 1, "scored": 14, "failed": 0, "pending": 0, "stopped": None}}
        assert [(r["email"], r["opp_id"]) for r in day_one.scores] == [(ANA, opp) for opp in sorted(SENT + [NEW])]
        assert {r["score_key"] for r in day_one.scores} == {key_of(ANA_TEXT)} and key_of(ANA_TEXT).endswith(":p1")

    def test_her_scores_are_her_own(self, day_one):
        by = {r["opp_id"]: r["score"] for r in day_one.scores}
        assert (by["E2E-BROAD"], by["E2E-TRUSTED"], by["E2E-NOCODES"]) == (5, 0, 5)
        assert (by["E2E-NOKW"], by[NEW]) == (SCORES["E2E-NOKW"], 4)  # the rest are the model's usual

    def test_the_team_pass_asked_about_the_new_grant_only_and_in_the_prompt_it_always_had(self, day_one, day_zero):
        team = [r for r in day_one.requests if r["profile"] is None]
        assert [n for r in team for n in r["numbers"]] == [NEW]
        (request,) = team
        assert request["system"] == day_zero.requests[0]["system"]  # the team's system prompt, byte for byte
        assert "Researcher profile" not in request["user"] and "lithium" not in request["system"] + request["user"]

    def test_the_team_scores_are_stored_as_they_always_were(self, day_one, day_zero):
        key = score_key(webapp._profile(), MODEL)
        assert {r["relevance_profile"] for r in day_zero.grants.values() if r["relevance_model"] != "rule"} == {key}
        assert {r["relevance_profile"] for r in day_one.grants.values() if r["relevance_model"] != "rule"} == {key}
        assert day_one.grants[NEW]["relevance_score"] == 4 and not any(str(r["relevance_profile"]).endswith(":p1") for r in day_one.grants.values())

    def test_her_requests_carry_her_description_as_data_and_the_team_ones_do_not(self, day_one):
        mine = [r for r in day_one.requests if r["profile"] == ANA_TEXT]
        assert sorted(n for r in mine for n in r["numbers"]) == sorted(SENT + [NEW])
        for request in mine:
            assert ANA_TEXT not in request["system"] and "data, not instructions" in request["system"]
            assert f"```\n{ANA_TEXT}\n```" in request["user"]
        assert [r["profile"] for r in day_one.requests].count(None) == 1 and len(mine) == len(day_one.requests) - 1

    def test_each_scorer_gets_only_its_own_votes_as_examples(self, day_one):
        theirs = {None: set(), ANA_TEXT: set()}
        for request in day_one.requests:
            theirs[request["profile"]].update(examples(request))
        assert theirs == {None: {BULLETS}, ANA_TEXT: {NOKW}}  # Cara's mark is the team's; Ana's is hers

    def test_the_team_pass_ran_before_hers(self, day_one):
        order = [r["profile"] is None for r in day_one.requests]
        assert order == [True] + [False] * (len(order) - 1)


class TestTheDashboard:
    def test_she_is_shown_her_own_scores_and_the_floor_is_hers(self, day_one):
        assert day_one.me["me"]["using"] == "mine" and day_one.ana_all["scores"] == "mine" and day_one.me["me"]["min_score"] == 4
        assert day_one.me["me"]["coverage"] == {"open": day_one.me["data"]["open"], "scored": 15}  # 14 scored, and the grant the rules ruled out

    def test_a_grant_the_team_finds_broad_and_she_does_not_is_on_her_list_and_not_theirs(self, day_one):
        ana, cara = {r["opp_id"]: r for r in day_one.ana_list["results"]}, {r["opp_id"]: r for r in day_one.cara_list["results"]}
        assert ana["E2E-BROAD"]["relevance_score"] == 5 and "E2E-BROAD" not in cara
        assert "E2E-TRUSTED" in cara and "E2E-TRUSTED" not in ana  # and the other way round

    def test_her_own_not_for_us_hides_that_grant_for_her_only(self, day_one):
        ana, cara = ({r["opp_id"]: r for r in listing["results"]} for listing in (day_one.ana_all, day_one.cara_all))
        assert (ana["E2E-NOKW"]["my_verdict"], ana["E2E-NOKW"]["hidden"], ana["E2E-NOKW"]["votes_down"]) == (-1, True, None)
        assert (cara["E2E-NOKW"]["my_verdict"], cara["E2E-NOKW"]["hidden"], cara["E2E-NOKW"]["votes_down"]) == (None, False, 0)
        assert (cara["E2E-BULLETS"]["hidden"], cara["E2E-BULLETS"]["votes_down"]) == (True, 1) and ana["E2E-BULLETS"]["hidden"] is False

    def test_the_teammate_still_sees_exactly_the_teams_scores(self, day_one):
        assert day_one.cara_all["scores"] == "team" and day_one.cara_me["using"] == "team" and day_one.cara_me["has_profile"] is False
        shown = {r["opp_id"]: r["relevance_score"] for r in day_one.cara_all["results"]}
        assert shown and shown == {opp: day_one.grants[opp]["relevance_score"] for opp in shown}

    def test_the_detail_of_a_grant_has_the_score_of_the_list(self, day_one):
        assert (day_one.detail["ana"]["relevance_score"], day_one.detail["cara"]["relevance_score"]) == (5, 2)


class TestTheEmail:
    def test_both_are_emailed_and_the_run_succeeds(self, emailed):
        assert emailed.code == 0 and set(emailed.mails) == {ANA, CARA}

    def test_ana_gets_her_matches(self, emailed, day_zero):
        titles = day_zero.titles | {NEW: "A Brand New Network Authentication Program"}
        mine = {"E2E-BULLETS": 5, "E2E-ENTITY": 5, "E2E-PLACEHOLDER": 5, "DURIP-AFOSR-26": 4, "E2E-FORECAST": 4, "E2E-BROAD": 5, "E2E-NOCODES": 5, NEW: 4}
        assert new_grants(emailed.mails[ANA]) == entries(titles, mine)  # not E2E-TRUSTED (she scores it 0) and not E2E-NOKW (her Not for us)

    def test_her_footer_is_for_her_profile(self, emailed):
        assert OWN in emailed.texts[ANA] and OWN in emailed.htmls[ANA]
        assert TEAM not in emailed.texts[ANA] and "Want matches" not in emailed.texts[ANA] and "start after the next daily run" not in emailed.texts[ANA]

    def test_cara_gets_the_team_list_less_the_grant_she_declined(self, emailed, day_zero):
        titles = day_zero.titles | {NEW: "A Brand New Network Authentication Program"}
        team = {"E2E-TRUSTED": 4, "E2E-NOKW": 5, "E2E-ENTITY": 5, "E2E-PLACEHOLDER": 5, "DURIP-AFOSR-26": 4, "E2E-FORECAST": 4, NEW: 4}
        assert new_grants(emailed.mails[CARA]) == entries(titles, team)  # Ana's Not for us on E2E-NOKW is not the team's

    def test_her_footer_is_the_teams_and_invites_her_to_write_her_own(self, emailed):
        assert TEAM in emailed.texts[CARA] and INVITE in emailed.texts[CARA]
        assert f'<a href="{DASHBOARD_URL}"' in emailed.htmls[CARA] and OWN not in emailed.texts[CARA]


class TestAnEdit:
    def test_saving_a_changed_text_takes_her_scores_away_at_once(self, edited):
        assert edited.put.status_code == 200 and edited.after_put == []
        assert edited.me_after_put["using"] == "team" and edited.list_after_put == "team"

    def test_the_next_run_rescores_her_only_and_leaves_the_team_alone(self, edited):
        assert edited.code == 0 and edited.requests and all(r["profile"] == ANA_EDIT for r in edited.requests)
        assert sorted(n for r in edited.requests for n in r["numbers"]) == sorted(SENT + [NEW])
        assert edited.run["stats"] == {"personal": {"people": 1, "scored": 14, "failed": 0, "pending": 0, "stopped": None}}
        assert {r["score_key"] for r in edited.scores} == {key_of(ANA_EDIT)} and {r["email"] for r in edited.scores} == {ANA}

    def test_she_is_back_on_her_own_scores_and_they_are_the_new_ones(self, edited):
        rows = {r["opp_id"]: r for r in edited.list["results"]}
        assert edited.me["using"] == "mine" and (rows["E2E-TRUSTED"]["relevance_score"], rows["E2E-BROAD"]["relevance_score"]) == (5, 0)
        assert edited.cara_list["scores"] == "team"

    def test_the_email_after_it_is_hers_alone_and_announces_what_her_new_text_makes_of_a_grant_she_had_not_been_told_about(self, edited, day_zero):
        assert edited.status == 0 and set(edited.mails) == {ANA}
        assert new_grants(edited.mails[ANA]) == entries(day_zero.titles, {"E2E-TRUSTED": 5})


class TestATimeStop:
    def test_with_no_time_left_nothing_is_scored_and_it_is_not_a_failure(self, stopped):
        stop = stopped.stop
        assert stop.code == 0 and stop.requests == [] and stop.scores == []
        assert stop.run["ok"] is True and stop.run["stats"] == {"personal": {"people": 1, "scored": 0, "failed": 0, "pending": 14, "stopped": "time limit"}}
        assert "Stopped: time limit." in stop.run["detail"]

    def test_she_sees_the_teams_scores_while_it_is_pending_and_the_dashboard_counts_nothing_done(self, stopped):
        assert stopped.stop.me["using"] == "team" and stopped.stop.list == "team"
        assert stopped.stop.me["coverage"]["scored"] == 1  # only the grant the rules ruled out

    def test_the_next_run_finishes_it(self, stopped):
        done = stopped.done
        assert done.code == 0 and done.requests and all(r["profile"] == ANA_THIRD for r in done.requests)
        assert done.run["stats"] == {"personal": {"people": 1, "scored": 14, "failed": 0, "pending": 0, "stopped": None}}
        assert len(done.scores) == 14 and {r["score_key"] for r in done.scores} == {key_of(ANA_THIRD)}
        assert done.me["using"] == "mine" and done.list == "mine" and done.me["coverage"]["scored"] == done.me["coverage"]["open"]


class TestAModelOutage:
    def test_the_grants_are_loaded_and_the_run_is_not_a_failure(self, outage):
        assert outage.code == 0 and outage.requests == []
        assert outage.loaded == [{"opp_id": LATE, "relevance_score": None}]

    def test_the_personal_step_is_skipped_and_says_why(self, outage):
        assert outage.run["ok"] is True and outage.run["detail"].startswith("Skipped: the team's scoring stopped: cannot reach http://127.0.0.1:")
        stats = outage.run["stats"]["personal"]
        assert stats["stopped"].startswith("skipped: the team's scoring stopped") and stats["scored"] == 0
        assert LATE not in {r["opp_id"] for r in outage.scores}

    def test_the_dashboard_says_the_new_grant_is_not_scored_yet_for_each(self, outage):
        assert outage.late["cara"]["relevance_score"] is None
        assert (outage.late["ana"]["relevance_score"], outage.late["ana"]["relevance_reason"]) == (None, "Not scored for your profile yet")

    def test_the_email_still_goes_to_both_with_the_tracked_grants_reminder_and_nothing_wrong(self, outage):
        assert outage.status == 0 and set(outage.mails) == {ANA, CARA}
        for text in outage.texts.values():
            assert "Deadline reminders (1)" in text and "Foundations of Trusted Systems" in text
            assert "New grants that fit" not in text and "A Late Network Authentication Program" not in text


class TestTheDayAfterAnOutage:
    def test_the_grant_is_scored_for_the_team_and_for_ana_without_asking_about_the_rest(self, recovered):
        assert recovered.code == 0 and recovered.run["stats"] == {"personal": {"people": 1, "scored": 1, "failed": 0, "pending": 0, "stopped": None}}
        assert {n for r in recovered.requests for n in r["numbers"]} == {LATE}
        assert sorted(r["profile"] is None for r in recovered.requests) == [False, True]
        assert LATE in {r["opp_id"] for r in recovered.scores}

    def test_both_are_told_about_it_for_the_first_time(self, recovered):
        assert recovered.status == 0 and set(recovered.mails) == {ANA, CARA}
        for message in recovered.mails.values():
            assert new_grants(message) == [("4/5", "A Late Network Authentication Program")]


class TestRemovingTheProfile:
    def test_it_takes_her_profile_and_scores_and_puts_her_on_the_teams_view(self, removed):
        assert removed.delete.status_code == 200 and removed.delete.json()["removed"] is True
        assert removed.after == {"profiles": 0, "scores": 0}
        assert removed.me["has_profile"] is False and removed.me["using"] == "team" and removed.list["scores"] == "team"

    def test_her_not_for_us_counts_for_the_team_again(self, removed):
        nokw = removed.cara_nokw
        assert (nokw["votes_down"], nokw["team_verdict"], nokw["hidden"]) == (1, -1, True)

    def test_nothing_is_asked_for_her_afterwards_and_her_vote_teaches_the_teams_scorer_again(self, removed):
        assert removed.idle_code == 0 and removed.idle_requests == []
        assert removed.code == 0 and [n for r in removed.requests for n in r["numbers"]] == [LAST]
        (request,) = removed.requests
        assert request["profile"] is None and set(examples(request)) == {BULLETS, NOKW}
        assert removed.run["stats"]["personal"]["people"] == 0

    def test_her_email_is_the_teams_with_the_invitation(self, removed):
        assert removed.status == 0 and set(removed.mails) == {ANA, CARA}
        for text in removed.texts.values():
            assert TEAM in text and INVITE in text and OWN not in text
        assert new_grants(removed.mails[ANA]) == [("4/5", "A Last Network Authentication Program")]
