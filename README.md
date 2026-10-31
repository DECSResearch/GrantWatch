# GrantWatch

GrantWatch watches Grants.gov for funding a research group can actually
pursue and makes sure no deadline slips. A daily pipeline scores every open
opportunity against a written research profile, each team member is emailed
about every grant that fits (once), tracked grants get deadline reminders, a
dashboard shows what closes when, and an MCP server lets Claude answer
questions about the same data.

## How it fits together

1. `config/research_profile.yml` describes the research program, the
   applicant type's Grants.gov codes, the keyword pre-filter, agencies to skip,
   the score below which a grant is not worth an email, and the deadline
   horizons.
2. `python main.py` runs two steps. **Ingest** downloads the daily Grants.gov
   extract (about 83,000 records), keeps the ones still open (not past their
   close date and not archived), drops excluded agencies, scores what is
   unscored with an OpenAI-compatible model (every open grant goes to the
   model; the keyword pre-filter applies only when no model is set up),
   writes a CSV and upserts Postgres. **Notify** then emails each recipient
   whatever they have not been told yet (see [Email](#email)). Notify runs
   even when the download fails, and the run exits non-zero if either step
   failed.
3. The dashboard (`src/web/app.py`, deployed on Vercel) reads Postgres: a
   90-day timeline, grants grouped by deadline, relevance scores with the
   model's reason, and a Track button for grants the group intends to pursue.
   Team members sign in with an emailed link to track grants and to write
   their own research profile; anyone else sees the public list of open
   Grants.gov grants.
4. `mcp_server/server.py` exposes the same queries to Claude Code or Claude
   Desktop.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in POSTGRES_URL (or the POSTGRES_* parts)
```

Then edit `config/research_profile.yml`: replace the `summary` with two to
five sentences about the group's research. Scoring stays off until that is
done, and everything else still works without it.

### Relevance scoring with Ollama or vLLM

Scoring talks to any server that speaks the OpenAI chat-completions protocol.
Set these in `.env`:

| Server | `GRANTS_LLM_BASE_URL` | `GRANTS_LLM_MODEL` | `GRANTS_LLM_API_KEY` |
|--------|-----------------------|--------------------|----------------------|
| Ollama | `http://localhost:11434/v1` | the model you pulled, e.g. the name shown by `ollama list` | `ollama` (ignored) |
| vLLM   | `http://host:8000/v1` | the served model name | the value passed to `--api-key`, if any |
| Open WebUI | `http://host:port/api` | the model id it lists, e.g. `qwen3.6-35b` | an API key from Settings > Account |

Reasoning models such as Qwen 3.x think before answering, and with the
default token budget they can run out before writing any JSON; scoring then
stops with a message saying so. Either raise `GRANTS_LLM_MAX_TOKENS` (8192 is
enough) or turn thinking off, which is about nine times faster with nearly
the same scores:

```bash
GRANTS_LLM_EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}
```

The scorer asks for a 0 to 5 fit score, a one-sentence summary, and a
one-sentence reason per grant, batched `GRANTS_LLM_BATCH_SIZE` at a time with
`GRANTS_LLM_WORKERS` concurrent requests. It requests JSON-schema output,
falls back to plain JSON mode and then to free text if the server rejects
those, and tolerates reasoning tags and code fences. Scores are cached per
profile in the database and in `grants_data/cache/`, so each run only scores
grants it has not seen; the first run is capped by
`GRANTS_RELEVANCE_MAX_PER_RUN` (soonest deadlines first) and catches up on
later runs. Changing the profile summary or the model rescores everything, and so does
a release that changes the scoring prompt (`SCORER_VERSION`).

Keywords do not decide what the model sees: the keyword filter dropped
relevant grants whose descriptions are short, so with a model every open
grant is scored and `MATCHED_KEYWORDS` is only a hint on the dashboard. The
one rule applied first is a fact from Grants.gov: set `applicant_codes` in
the profile (for example `["06"]` for a public university) and a grant whose
codes are known and leave that type out is scored 0 ("Not open to ...") with
no model call, but is still loaded. Grants that list unrestricted (99) or
"others" (25), or no codes, always go to the model.

### A member's own profile

A member who has written a research description of their own (40 to 2,000
characters, one row in `member_profiles`) is scored against the team profile
with that summary in place of the team's: the institution, applicant type,
agency exclusions and horizons stay the team's. Their scores are in
`member_scores`; the team's stay on `grants` and are never touched. Their
description goes to the model in the user message as a fenced block of data,
and their own votes are their scorer's examples. Removing the member, or just
their profile, deletes their rows; unsubscribing keeps them.

A recorded `personal` step does the scoring, between `ingest` and `notify`.
Ingest leaves the grants it sent to the model, and the members with a profile
take turns scoring them: up to 60 grants a turn, soonest close date first. The
step starts no turn once `GRANTS_PERSONAL_STOP_MINUTES` (default 65) have
passed since the process started, which leaves time for the email inside the
cronjob's 90 minutes; what is left is scored on the next run. A model outage
stops everyone, and any other trouble stops only the member it happened to. It
is skipped when ingest failed, when there is no model or team profile, and when
the team's own scoring stopped. It can never change ingest's exit code or stop
`notify`. Its numbers (`people`, `scored`, `failed`, `pending`, `stopped`) are
the run's stats under `personal`.

Changing the text of a profile deletes that member's scores, and a change to
the team's institution, applicant type or model, or a new `SCORER_VERSION`, makes
them stale. Either way the next run rescores them, and a new profile is scored
by `python main.py ingest` without waiting for the next daily run (nothing is
rescored for the team: its scores are cached).

The email follows the member's profile too. Their own score and reason replace
the team's, and only their own votes count for them (the team's marks do not),
and the footer says the scores are for their own research profile. A grant not
scored for them yet is not announced to them, whatever they marked, until it
is, and a grant the eligibility rules ruled out stays at 0. A program is
announced to them only through a posting their profile has scored; a ruled-out
posting never stands in for one it has not. A score made for an older version
of their profile (another model, a new `SCORER_VERSION`, a changed institution)
still counts until the next run rescores it, as the team's scores do. While
nothing is scored for their profile they get the team's scores with their own
votes, and the email says their own matches start after the next daily run.
`notify` reads every member's scores and votes once per run. If that read
fails, each member gets only reminders, deadline changes and withdrawn notices
that run, with no scores shown and nothing recorded for new grants or the
overview, so the next run still has them; team recipients are not affected.
A team recipient's email ends with a line inviting them to write a profile on
the dashboard (when `GRANTS_DASHBOARD_URL` is set).

### Running it

```bash
python main.py recipients add you@und.edu     # once per team member
python main.py notify --dry-run               # print what would be sent
python main.py                                # ingest, score members' own profiles, then email
```

`python main.py ingest` (which also scores the members' own profiles) and
`python main.py notify` run one step each.

## Email

Each run sends a recipient one email, and only when there is something to
say. It can hold:

- **New grants that fit**: every open grant scored at or above
  `min_score_to_notify` (or the recipient's own `--min-score`), announced
  once (by their own scores, for a member with a profile of their own). A grant scored days after it was first loaded is still announced. At
  most `GRANTS_EMAIL_MAX_NEW` per email, best first; the rest follow in the
  next ones.
- **Deadline reminders** for tracked grants, once as each `reminder_days`
  mark passes (30, 14, 7 and 2 days by default).
- **Deadlines that moved** for tracked grants, and **tracked grants no longer
  open** when one is archived early or drops out of the extract.
- **The overview** of everything relevant by deadline horizon, on
  `overview_day` (Monday by default).

What each person has been told is kept in the `notifications` table and
written only after the email went out, so a failed send is retried on the
next run, a rerun sends nothing twice, and two schedulers can both run (a
lease in `notify_lock` keeps them from sending at the same moment). Emails
are trimmed to stay under Gmail's ~102 KB clipping limit, carry a one-click
unsubscribe link when `GRANTS_DASHBOARD_URL` is set, and go to each recipient
separately. Until scoring is set up, every grant that matches at least
`keyword_threshold` keywords (and never fewer than one) is announced, most
hits first, so the profile's `keywords` are what decide what you hear about;
name your research fields there.

Recipients live in the `recipients` table. `python main.py recipients
add|remove|list` manages them, and addresses in `GRANTS_NOTIFY_RECIPIENTS`
are added on every run without undoing anyone's unsubscribe.

Pick how mail is sent in `.env` (see `.env.example`):

| Transport | Settings | Notes |
|-----------|----------|-------|
| SMTP | `GRANTS_SMTP_HOST`, `GRANTS_SMTP_USER`, `GRANTS_SMTP_PASSWORD`, `GRANTS_EMAIL_FROM` | Gmail with an app password, Microsoft 365, AWS SES, Postmark. |
| Gmail API | `GMAIL_TOKEN_FILE` (from `scripts/generate_gmail_token.py`) or `GMAIL_TOKEN_JSON` | Publish the OAuth app; tokens from an app in Testing mode expire after 7 days. |
| Outbox | `GRANTS_EMAIL_OUTBOX=outbox` | Writes `.eml` files instead of sending; for trying it out. |

The property tests in `tests/test_planner_properties.py` state these rules
as invariants and check them over simulated weeks of daily runs with late
scores, moving deadlines and failed sends. `tests/test_personal_email.py` does
the same for a member's own scores: one member's plan never depends on
another's scores or votes, and a grant with no score of theirs is never
announced to them.

## Dashboard

```bash
PYTHONPATH=. uvicorn web.app:app --app-dir src --reload   # http://localhost:8000
```

API: `GET /api/grants` (`q`, `min_score`, `closing_within`,
`include_forecasted`, `watched_only`, `limit`), `GET /api/grants/{id}` and
`GET /api/profile` for anyone, and for signed-in team members
`GET /api/watchlist`, `POST /api/watchlist` and `DELETE /api/watchlist/{id}`,
`POST /api/feedback` (`{"opp_id": ..., "verdict": 1}` for Relevant or `-1` for
Not for us) and `DELETE /api/feedback/{id}`, and `GET`, `PUT` and `DELETE`
`/api/me/profile` for their own research profile. Sign-in:
`POST /api/login` emails a link to a recipient, `GET /auth` sets the session,
`GET /api/session`, `POST /api/logout`. `GET|POST /unsubscribe?token=` serves
the links in the emails (a GET only asks; mail scanners fetch links).

`closing_within=N` includes a grant closing exactly N days out. `/api/grants`
reports `total` (how many postings matched, before `limit`) beside
`count` (the rows returned, a program posted by several services as one), and
`/api/profile` reports `data.fit_count`, the open grants scoring at least the
team threshold. The page opens on any closing date. When `limit` cuts the list
short it says "Showing N grants from the first 500 of M postings", where N is
the rows drawn and M is `total`.

Changing the tracked list, voting and writing a profile need
`GRANTS_SECRET_KEY` on the server and a sign-in. Without a signed-in member,
including every request to a server that has no key, the API gives only the
public view described below, so a server put on a public address without
`GRANTS_SECRET_KEY` shows nothing but Grants.gov's facts.

When the sign-in email does not arrive (a university mail filter may hold it),
an admin prints a link that works for one hour with
`python main.py sign-in-link EMAIL`. It is for an active recipient only, needs
the web server's `GRANTS_SECRET_KEY`, and points at `GRANTS_DASHBOARD_URL`
(which must be the address people open), else at `http://localhost:8000`.
Anyone who has the link can sign in as that person, so send it to them only.

### The public list and a member's own scores

**A visitor** (no signed-in member) gets the open Grants.gov grants and
nothing GrantWatch holds about the team: no scores or reasons, matched keywords,
tracking, votes, thresholds or profile text. `GET /api/grants` has its own SQL
that selects Grants.gov columns only, and a row is cut down by an allow list
(`grants.display.public_row`) before the grouping of one program's postings
reads it and again after, so which posting leads cannot depend on a score or on
tracking (it is the lowest opportunity number). The summary is the Grants.gov
description cut as the fallback summary is; the model's summary is written
from the team's profile and is never sent. `q` searches the title, agency,
opportunity number and description. Rows are ordered by close date, then post
date, title and opportunity number, never by score, so `limit` cannot show
where the team ranks a grant. `min_score`, `watched_only` and `include_closed`
are ignored. `GET /api/grants/{id}` adds the full description, and
`GET /api/profile` is `{"configured", "data": {"open", "data_as_of"}}` only.
Without a model, or with the team profile unconfigured, the database holds
the keyword matches (a pick made for the team), so a visitor gets no list, no
detail and `"configured": false`.

**A member with no profile of their own** gets the team's scores and votes as
before. **A member with one** (see `PUT /api/me/profile`) gets their own scores
from `member_scores`, once any is stored for them under any key (an old score
stands until the daily run replaces it, as in their email), and the team's
before that; `GET /api/grants` says which in `scores` (`"mine"` or `"team"`).
A grant their profile has not scored has no score, the reason "Not scored for
your profile yet" and fails a score floor unless it is tracked or they marked
it Relevant; a grant the eligibility pre-check ruled out is 0 with the rule's
reason. Their own votes decide for them alone: `votes_up`, `votes_down` and
`team_verdict` are null, their Not for us hides a grant for them, and their
Relevant marks pass their floor. `GET /api/profile` adds `me`: `has_profile`,
`updated_at`, `min_score` (their default floor: `recipients.min_score`, else the
team's; `/api/grants` applies a floor only when asked), `using`, and `coverage`,
`{"open", "scored"}`, the open grants with a score under the key of their
profile as it is now (made from the cleaned text, as the scoring pass makes it)
plus the ones the pre-check ruled out. `data.fit_count` is taken at that floor
by the scores they are shown.

`PUT /api/me/profile` takes `{"summary"}` as JSON (any other content type is
refused with a 415, so a cross-site form cannot send it): cleaned, 40 to 2,000
characters, else a 400 with the reason. A changed text deletes the member's
scores in the same transaction, so they see the team's until the next daily
run. `DELETE` returns them to the team profile, scores deleted and votes back
with the team's. Every `/api` answer is `Cache-Control: no-store` with
`Vary: Cookie`, and the web app applies the database schema once when it
starts (best effort, logged) so these tables exist before the first run.

### The dashboard page

One page serves everyone and asks `/api/session` and `/api/profile` who is
looking. **A visitor** sees "Open Grants.gov opportunities as of <date>: <n>.",
a line saying this is a public list and team members sign in (with "Link not
arriving? Ask the GrantWatch admin for one."), the deadline strip, and rows of
two columns (when it closes, the grant with its Grants.gov description). The
filters are Search, Closing within, Stage and Show forecasted. There is no
score, reason, keyword, button, scoring-off note or Admin link, and the page's
markup starts as this page, so a member's controls are never drawn and hidden
again. With no model there is no list: only the sign-in prompt, and no grants
are asked for. Where `GRANTS_SECRET_KEY` is not set nobody can sign in, so the
page is the visitor's without the sign-in prompt.

**A member** sees what they saw before, with their own floor (`me.min_score`,
which `data.fit_count` and the status line are counted at) and, once they have
a profile of their own, their own scores or, until the daily run has scored
them, the team's with the note "Your own matches start after the next daily
run. Until then you see the team's." Their vote buttons show no team counts,
and their own Not for us hides a grant for them. "My profile" next to "Signed
in as ..." opens a panel, built by the page's script for members only: a
textarea for what they work on (the same 40 to 2,000 characters
`PUT /api/me/profile` checks, and its message is shown as it comes), Save, "Use
the team profile instead" and how many open grants are scored for their
profile. Save and the switch ask first when they would delete scores. Signing
out reloads the page.

What depends on who is looking is decided by pure functions in one block of
`src/web/templates/index.html` (`viewOf`, `rowHtml`, `planSave` and others).
`tests/test_dashboard_view.py` runs that block under node (skipped when node is
not installed), with Hypothesis for the rules.

### Team feedback

A team member can mark a grant Relevant or Not for us (one vote each, and a
new vote replaces the old one; the votes are in the `grant_feedback` table).
A program posted by several services is one grant to vote on: a person's vote
sits on one of its postings, and voting or taking the vote back through any
posting replaces or removes it on the others.
A member who has a research profile of their own (a row in
`member_profiles`) votes for themselves only: their votes are left out of the
team's counts, verdicts, hidden grants and scorer examples, and are the examples
for their own scoring instead. Deleting the profile returns them to the team.
For a signed-in member `GET /api/grants` adds `my_verdict`, `my_vote_opp_id`
(the posting that holds the viewer's vote, else the row's own, which is where a
new vote goes), `votes_up`, `votes_down`, `team_verdict` (1, -1, or 0 for none
or a tie) and `hidden` (the team said Not for us and nobody tracks it) to each
row. The votes work on the team's side of the pipeline directly, without
rescoring anything:

- a grant the team marked Not for us is left out of the email's new grants and
  weekly overview, unless it is tracked; one marked Relevant counts as meeting
  the score threshold; a tie is no verdict. A program posted by several
  services is judged on its open postings' votes together. The dashboard's
  score bar (`min_score`) lets a grant or program the team marked Relevant
  through, like a tracked grant, so new grants the email lists because of the
  team's mark also show on the default dashboard;
- up to 10 marked grants (5 of each, the most recent first) go to the model
  with every scoring request as examples, so grants scored from then on lean
  toward what the team wants. They are not part of the score cache key, so
  voting never rescores old grants.

## Admin page

`/admin/` answers "did last night's run work, and if not, where did it
break?". It is read-only and shows:

- a verdict, a strip of the last 14 days of `ingest` and `notify` runs, and a
  sentence for anything that looks wrong;
- how the daily run is doing: the Grants.gov file and its filter funnel, with a
  "Personal profiles" line under it (people, scored, failed and pending in the
  newest personal step, and whether it stopped at its time limit), the
  database, a live check of the model server (with a "Check now" button), and
  email;
- the last 15 days of runs, each with its numbers and the log lines it wrote;
- the team (research profile, thresholds, recipients, how many votes there
  are and what share of the grants emailed in the last 30 days someone marked
  Not for us, counting a program posted by several services once), the emails
  sent per day, and the settings that shape a run. Each person in the table
  shows whether they use the team's profile or one of their own (with the day
  they saved it, never its text) and how many open grants are scored for them.
  A line says "Sign-in is off" when the server has no `GRANTS_SECRET_KEY`. No
  keys or passwords.

It asks for HTTP Basic credentials: each person has a named account in
`GRANTS_ADMIN_USERS`, one `name:scrypt$...` entry per person (comma or newline
separated), and only the password hash is stored. The page shows "Signed in as
NAME", and each visit is logged with the name. With no valid account the page
answers 503. It does not use the email sign-in, so it opens even where the
team's mail cannot receive sign-in links. Log lines are saved with each run
with the known secrets removed.

Make an entry with `python main.py admin-user NAME`. It asks for a password
twice (16 or more ASCII characters), prints the line, and needs no database.
To remove a person, delete their line and redeploy. Basic auth has no
sign-out: close the browser.

```bash
python main.py admin-user alice
GRANTS_ADMIN_USERS='alice:scrypt$16384$8$1$...' PYTHONPATH=. uvicorn web.app:app --app-dir src   # http://localhost:8000/admin/
```

Keep the value in single quotes, or the shell expands the `$` signs. On the lab
cluster it is at `http://<lab host>:30810/admin/`, and the lines go in
`admin_users` in `ansible/secrets.yml`. That NodePort is plain HTTP, so the
password crosses the lab network unencrypted: use a password you use nowhere
else. Runs are recorded in the `job_runs` table; the
first page load after an upgrade adds the tables and columns it needs. A run killed by the
job's deadline cannot save its log, and shows as "did not finish". Emails are
counted by day in `GRANTS_TIMEZONE` (default `America/Chicago`).

## MCP server

Register it once from the repository root, then ask Claude things like
"what closes this month that fits my grid work" or "track the second one":

```bash
pip install -r mcp_server/requirements.txt
claude mcp add grantwatch -e POSTGRES_URL="$POSTGRES_URL" -- \
  "$PWD/.venv/bin/python" "$PWD/mcp_server/server.py"
```

For Claude Desktop, add the same command to `claude_desktop_config.json`
under `mcpServers`. Tools: `search_grants`, `upcoming_deadlines`,
`grant_details`, `track_grant`, `untrack_grant`, `tracked_grants`,
`research_profile`.

## Scheduled runs

Run it daily on the machine that runs the model. The job starts at 07:30,
after the extract is published, and catches up after sleep:

```bash
scripts/install_launchd.sh
launchctl start com.grantwatch.daily     # run once now; output in logs/daily.log
```

On Linux, add `30 7 * * * /path/to/GrantWatch/scripts/run_daily.sh` to
`crontab -e`. `main.py` exits non-zero when ingest or any email failed, and
rerunning it the same day never sends anything twice.

## Running on the lab cluster

GrantWatch can run on the lab's k3s cluster: Postgres, the dashboard on port
30810, and the daily job as a CronJob, deployed with Ansible the same way as
`c2sr-testbed-llm-orchestrator`. See [k8s/README.md](k8s/README.md).

## Deploying the web app

Vercel is not linked to this GitHub repository, so pushing does not deploy.
Set `POSTGRES_URL`, `GRANTS_SECRET_KEY`, `GRANTS_DASHBOARD_URL` and one email
transport (`GRANTS_SMTP_*` or `GMAIL_TOKEN_JSON`, plus `GRANTS_EMAIL_FROM`)
in the Vercel project's environment variables so sign-in links and
unsubscribe pages work. After changing `src/web/` or `api/`, run:

```bash
npx vercel --prod
```

# Grants.gov Document Checker

Temporary document validation pipeline where applicants upload opportunity-specific files. Uploads land in an encrypted S3 bucket, a Lambda function validates them, and a DynamoDB entry tracks checklist status surfaced through the FastAPI backend and Next.js UI.

## Architecture
- **Frontend:** Next.js App Router (`app/page.tsx`) talks to the FastAPI backend via REST.
- **Backend:** FastAPI (`src/web/app.py`) exposes `/start-submission`, `/upload-url`, `/status/{id}`, and `/manifest` endpoints, reading manifests from YAML and storing state in DynamoDB.
- **Storage:** S3 bucket `grant-doc-checker-temp-<env>` with default SSE, blocked public access, lifecycle rule deleting objects after 2 days, and CORS for browser uploads.
- **Processing:** S3 ObjectCreated events flow through EventBridge to the `ValidateDoc` Lambda (`aws/lambda/validate_doc.py`) that runs pdfplumber/Textract checks and updates DynamoDB.
- **State:** DynamoDB table `submissions` keeps per-file findings, overall status, TTL attribute for automatic cleanup.

## FastAPI Endpoints
| Method | Path | Description |
| --- | --- | --- |
| POST | `/start-submission` | Creates a new submission, returns `submission_id`. Optional body `{ "opportunity_id": "opp-001" }`. |
| POST | `/upload-url` | Body `{ filename, contentType, submission_id?, requirement_id, opportunity_id? }`. Returns presigned PUT URL + object key. |
| GET | `/status/{submission_id}` | Returns overall status and per-file messages. |
| GET | `/manifest?opportunity_id=opp-001` | Returns requirement list derived from YAML in `config/doc_manifests`. |
| GET | `/manifest/index` | Lists available opportunity IDs and labels. |

## Environment Variables
Configure these (see `.env.example`):
- `DOC_CHECKER_BUCKET`, `DOC_CHECKER_TABLE`: AWS resource names.
- `DOC_CHECKER_ALLOWED_ORIGINS`: comma-separated origins for CORS (e.g. `http://localhost:3000`).
- `DOC_CHECKER_PRESIGN_SECONDS`, `DOC_CHECKER_TTL_DAYS`, `DOC_CHECKER_DEFAULT_MAX_MB`, `DOC_CHECKER_DEFAULT_MAX_PAGES`.
- `DOC_CHECKER_ENABLE_TEXTRACT`: `true` to invoke Textract when PDF text is empty.
- `DOC_CHECKER_MANIFEST_PATH`: directory containing manifest YAML files (`config/doc_manifests` by default).
- AWS credentials/region (standard `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`).
- Frontend uses `NEXT_PUBLIC_DOC_CHECKER_API` to find the backend.

## Local Development
1. **Python backend**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows PowerShell
   pip install -r requirements.txt
   uvicorn src.web.app:app --reload
   ```
2. **Next.js frontend**
   ```bash
   npm install
   npm run dev
   ```
   Visit `http://localhost:3000`. The UI will call the backend at `NEXT_PUBLIC_DOC_CHECKER_API` (defaults to `http://localhost:8000`).
3. **Tests**
   ```bash
   pip install -r requirements-dev.txt
   pytest tests/ -q
   ```
   The dashboard page's own rules (what a visitor and a member see) are
   tested under node, so the tests need `node` on the path; they are skipped
   without it.

   `tests/e2e/` runs the real flow (ingest through a model over HTTP, the email,
   the dashboard and admin APIs, votes, a member's own profile from the first
   save to their email, what a visitor is shown, and the failure paths) against
   a real Postgres and a fake model server. It is skipped unless `GRANTS_TEST_POSTGRES_URL`
   is set to a database you can create schemas in, for example
   `GRANTS_TEST_POSTGRES_URL=postgresql://you@localhost:5432/grantwatch_e2e_tests pytest tests/e2e -q`.
   Each test module makes its own schema and drops it afterwards. CI runs it
   against a `postgres:16` service.

## AWS Deployment
1. **Package the Lambda**
   ```powershell
   cd GrantWatch
   python -m venv lambda-env
   lambda-env\Scripts\activate
   pip install -r aws/lambda/requirements.txt -t aws/dist
   Copy-Item aws/lambda/validate_doc.py aws/dist/validate_doc.py
   Copy-Item -Recurse doc_checker aws/dist/doc_checker
   cd aws/dist
   Compress-Archive -Path * -DestinationPath ../validate_doc.zip -Force
   ```
2. **Apply Terraform**
   ```bash
   cd GrantWatch/infrastructure
   terraform init
   terraform apply -var "environment=dev" -var "lambda_package_path=../aws/dist/validate_doc.zip"
   ```
   Terraform provisions the S3 bucket with SSE + 2-day lifecycle, DynamoDB table with TTL, IAM roles/policies for backend and Lambda, EventBridge rule, and Lambda wiring.
3. **Configure FastAPI runtime** with the Terraform outputs (bucket, table) and AWS credentials.

## Validation Flow
1. Start the backend + frontend locally.
2. Pick an opportunity (e.g. `opp-001`) and upload documents. Files stream directly to S3 via presigned PUT URLs.
3. The Lambda runs automatically, updating DynamoDB with validation results (filename regex, size, content type, pages, required sections, optional Textract fallback).
4. Click **Run Checks** to refresh the UI; ✅ indicates pass, ❌/warnings include Lambda messages. Submissions and objects expire automatically after 48 hours.

## Notes
- The manifest loader (`doc_checker/manifest.py`) reads all YAML files within `config/doc_manifests`, so you can add or version requirements without code changes.
- `aws/lambda/validate_doc.py` reuses the shared `doc_checker` package; ensure it is bundled with the Lambda artifact.
- Bucket CORS (configured via Terraform) allows PUT/GET/HEAD from the UI origins for presigned uploads.
- DynamoDB TTL field (`ttl`) plus bucket lifecycle keeps the environment self-cleaning, satisfying the temporary requirement.
