# GrantWatch

GrantWatch watches Grants.gov for funding a research group can actually
pursue and makes sure no deadline slips. A daily pipeline scores every open
opportunity against a written research profile, each team member is emailed
about every grant that fits (once), tracked grants get deadline reminders, a
dashboard shows what closes when, and an MCP server lets Claude answer
questions about the same data.

## How it fits together

1. `config/research_profile.yml` describes the research program, the keyword
   pre-filter, agencies to skip, the score below which a grant is not worth an
   email, and the deadline horizons.
2. `python main.py` runs two steps. **Ingest** downloads the daily Grants.gov
   extract (about 83,000 records), keeps the ones still open (not past their
   close date and not archived), drops excluded agencies, applies the keyword
   pre-filter, scores what is unscored with an OpenAI-compatible model, writes
   a CSV and upserts Postgres. **Notify** then emails each recipient whatever
   they have not been told yet (see [Email](#email)). Notify runs even when
   the download fails, and the run exits non-zero if either step failed.
3. The dashboard (`src/web/app.py`, deployed on Vercel) reads Postgres: a
   90-day timeline, grants grouped by deadline, relevance scores with the
   model's reason, and a Track button for grants the group intends to pursue.
   Team members sign in with an emailed link to track grants.
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
later runs. Changing the profile summary rescores everything.

### Running it

```bash
python main.py recipients add you@und.edu     # once per team member
python main.py notify --dry-run               # print what would be sent
python main.py                                # ingest, then email
```

`python main.py ingest` and `python main.py notify` run one step each.

## Email

Each run sends a recipient one email, and only when there is something to
say. It can hold:

- **New grants that fit**: every open grant scored at or above
  `min_score_to_notify` (or the recipient's own `--min-score`), announced
  once. A grant scored days after it was first loaded is still announced. At
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
separately. Until scoring is set up, every new keyword match is announced,
most keyword hits first, so the profile's `keywords` are what decide what
you hear about; name your research fields there.

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
scores, moving deadlines and failed sends.

## Dashboard

```bash
uvicorn web.app:app --app-dir src --reload   # http://localhost:8000
```

API: `GET /api/grants` (`q`, `min_score`, `closing_within`,
`include_forecasted`, `watched_only`, `limit`), `GET /api/grants/{id}`,
`GET /api/watchlist`, `GET /api/profile`, and for signed-in team members
`POST /api/watchlist` and `DELETE /api/watchlist/{id}`. Sign-in:
`POST /api/login` emails a link to a recipient, `GET /auth` sets the session,
`GET /api/session`, `POST /api/logout`. `GET|POST /unsubscribe?token=` serves
the links in the emails (a GET only asks; mail scanners fetch links).

Reading is open to anyone with the URL; changing the tracked list needs
`GRANTS_SECRET_KEY` on the server and a sign-in. Without the key the
dashboard is read-only.

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
