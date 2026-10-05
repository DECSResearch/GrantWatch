# GrantWatch

GrantWatch watches Grants.gov for funding a research group can actually
pursue and makes sure no deadline slips. A daily pipeline scores every new
opportunity against a written research profile, a deadline-first digest goes
out by email, a dashboard shows what closes when, and an MCP server lets
Claude answer questions about the same data.

## How it fits together

1. `config/research_profile.yml` describes the research program, the keyword
   pre-filter, agencies to skip, the score below which a grant is not worth an
   email, and the deadline horizons.
2. `python main.py` downloads the daily Grants.gov extract (about 83,000
   records), keeps opportunities posted in the last 90 days, drops excluded
   agencies, applies the keyword pre-filter, scores what is new with an
   OpenAI-compatible model, writes a CSV, upserts Postgres, and prints and
   emails the digest.
3. The dashboard (`src/web/app.py`, deployed on Vercel) reads Postgres: a
   90-day timeline, grants grouped by deadline, relevance scores with the
   model's reason, and a Track button for grants the group intends to pursue.
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
GRANTS_DATA_SOURCE=extract python main.py
```

The digest is printed to the console, and emailed when
`GMAIL_NOTIFY_RECIPIENTS` and `GMAIL_TOKEN_FILE` are set (create the token
with `scripts/generate_gmail_token.py`). It leads with grants that are new
and score at least `min_score_to_notify`, then lists every relevant or
tracked grant closing inside each horizon with days left. Forecasted
opportunities are marked as estimated.

### Dashboard

```bash
uvicorn web.app:app --app-dir src --reload   # http://localhost:8000
```

API: `GET /api/grants` (`q`, `min_score`, `closing_within`,
`include_forecasted`, `watched_only`, `limit`), `GET /api/grants/{id}`,
`GET|POST /api/watchlist`, `DELETE /api/watchlist/{id}`, `GET /api/profile`,
plus the category subscription endpoints.

### MCP server

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

Pick one of the two.

**On this Mac (recommended when the model runs locally).** The job runs at
07:30, after the extract is published, and catches up after sleep:

```bash
scripts/install_launchd.sh
launchctl start com.grantwatch.daily     # run once now; output in logs/daily.log
```

On Linux, add `30 7 * * * /path/to/GrantWatch/scripts/run_daily.sh` to
`crontab -e`.

**On GitHub Actions.** `.github/workflows/pipeline.yml` runs daily at 12:00
UTC and on demand. A model on a laptop is not reachable from a runner, so
either point `GRANTS_LLM_BASE_URL` at a server GitHub can reach or leave
scoring to local runs; unscored grants still load and the digest still sends
tracked deadlines. Configure under **Settings > Secrets and variables > Actions**:

| Kind     | Name                      | Purpose                                                        |
|----------|---------------------------|----------------------------------------------------------------|
| Secret   | `POSTGRES_URL`            | Required. Production Postgres DSN; the run fails without it.   |
| Secret   | `GMAIL_TOKEN_JSON`        | Optional. Contents of the Gmail OAuth `token.json`.            |
| Secret   | `GRANTS_LLM_BASE_URL`     | Optional. OpenAI-compatible endpoint reachable from GitHub.    |
| Secret   | `GRANTS_LLM_API_KEY`      | Optional. Key for that endpoint.                               |
| Variable | `GRANTS_LLM_MODEL`        | Optional. Model name; scoring is off when empty.               |
| Variable | `GMAIL_NOTIFY_RECIPIENTS` | Optional. Comma-separated digest recipients.                   |
| Variable | `GMAIL_SENDER_EMAIL`      | Optional. Defaults to the first recipient.                     |
| Variable | `GRANTS_KEYWORDS`         | Optional. Overrides the profile's keyword list.                |
| Variable | `GRANTS_MIN_SCORE`        | Optional. Overrides the profile's notification threshold.      |
| Variable | `GRANTS_INCLUDE_FORECAST` | Optional. `false` drops forecasted opportunities.              |
| Variable | `GRANTS_GOV_LOOKBACK_DAYS`| Optional. Defaults to 90.                                      |

Each run uploads the CSV and `logs/grantwatch.log` as a workflow artifact,
and `main.py` exits non-zero when the pipeline fails so the run shows up red.

## Deploying the web app

Vercel is not linked to this GitHub repository, so pushing does not deploy.
After changing `src/web/` or `api/`, run:

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
