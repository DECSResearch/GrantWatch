# Admin status page

Status: v2.1. v1 (run log, admin data, password) is built on branch
`admin-status`. v2 adds what the admin asked for next: the research profile,
a live model-server check, per-run logs, email status, settings, the data
source with its filter funnel, and the page design.

## Problem

GrantWatch runs as a daily CronJob on the lab k3s cluster. Without
`kubectl logs` nobody can tell whether last night's run worked, whether the
model server answered, where the grants came from, or who was emailed. The
dashboard has one status line.

## Goal

One read-only page, `/admin`, that answers in five seconds: did last night
work, and if not, where did it break? No editing; recipients are still
managed with `python main.py recipients`.

## Part A: built in v1

### Run log

- **Table.** `job_runs (id SERIAL, step TEXT, started_at TIMESTAMP DEFAULT NOW(),
  finished_at TIMESTAMP, ok BOOLEAN, detail TEXT)` in
  `grants/sql_utils/schema.sql`.
- **Recorder.** `grants/runs.py: recorded(step)` yields a `Run` with `.ok`
  (default True) and `.detail`.
  - It inserts the row on entry (`_start`) and finishes it on exit
    (`_finish`). These are the test seams.
  - If the body raises, the row is marked failed with
    `"<Type>: <message>"`, and the exception is re-raised.
  - Database errors are logged as warnings and swallowed, so recording
    never changes a job's outcome.
- **What gets recorded.** `main.py` records `ingest` inside `ingest()`,
  after `ensure_schema()`. It records `notify` through `notify_step()`,
  which both the `notify` command and the default command use; the CronJob
  runs the default. Dry runs, `test-email` and `recipients` are not
  recorded.
- **Killed runs.** A killed run keeps `finished_at` NULL. After two hours it
  shows as "did not finish". The job's `activeDeadlineSeconds` is 90
  minutes.
- **Numbers.** These come out through `onlyTheGoodStuff.last_stats` (reset
  on every call) and `run_notify.last_summary`; return values are
  unchanged. `main.ingest_detail(ok, stats)` builds the one-sentence detail.

### Admin data

- **Main function.** `grants/admin.py: admin_status()`.
  - Ages are computed in SQL (`EXTRACT(EPOCH FROM NOW() - started_at)`).
  - Timestamps are cast `::timestamptz`, so the JSON carries an offset and
    the page shows local time.
  - A missing `job_runs` table is created on first load
    (`to_regclass` check, then `ensure_schema()`).
- **`run_state(finished_at, ok, age_seconds)`** returns one of `ok`,
  `failed`, `running` or `did not finish`.
- **`warnings_for(health, runs, recipient_count, scoring_on)`** returns
  plain sentences:
  - no ingest recorded;
  - no successful ingest in 30 hours (24 would false-alarm during the next
    run);
  - the newest ingest or notify failed or did not finish;
  - scoring is on and open grants are unscored;
  - there are no active recipients.

### Access

- **Password.** HTTP Basic with `GRANTS_ADMIN_PASSWORD`, any username,
  `compare_digest` on UTF-8 bytes.
  - Not set: 503. Wrong or missing: 401 with `WWW-Authenticate`.
  - ASCII only, because FastAPI decodes the header as ASCII.
- **Why not the email sign-in.** It sends a magic link, and UND mail drops
  the lab Gmail, so the page would be unreachable.
- **Responses.** `/admin/` and `/admin/api*` send `Cache-Control: no-store`.

## Part B: v2 additions

### B1. Per-run logs and stats

- **Columns.** Add to `job_runs`:
  `ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS log TEXT;` and
  `... ADD COLUMN IF NOT EXISTS stats JSONB;`.
- **Capturing the log.** While the body runs, `recorded()` attaches a
  `logging.Handler` to the `grantwatch` logger (`logs/status_logger.py`),
  with that module's formatter (`%(asctime)s | %(levelname)s | %(message)s`).
  - It keeps the last 500 lines (`collections.deque(maxlen=500)`), each cut
    to 500 characters.
  - On an exception it appends `traceback.format_exc()`, cut to 5,000
    characters.
  - The handler is removed in `finally`, so `ingest` then `notify` in one
    process never share or leak a handler.
  - `_finish(run_id, step, ok, detail, log, stats)` writes `log`, cut to
    the last 100,000 characters, and `stats` via `psycopg2.extras.Json`.
  - Capturing never raises into the job.
- **Stats.** `Run` gains `.stats: Optional[dict]` (JSON-safe values only).
  `ingest()` sets `run.stats = dict(onlyTheGoodStuff.last_stats)`.
  `notify_step()` sets `run.stats = run_notify.last_counts`.

### B2. Source and filter funnel

`onlyTheGoodStuff.last_stats` gains these keys, set as the pipeline reaches
each stage:

| key | value |
|---|---|
| `source` | `"extract"` or `"export"` (`GRANTS_DATA_SOURCE`) |
| `source_file` | base name of the file the run read: `gen_extract.last_extract_path` or the export path. Set in `_load_source_records` |
| `extract` | records after `normalize_records` (exists) |
| `open` | after `drop_closed_grants(date_filter_json_data(...))` |
| `after_forecast` | after the forecast filter, or equal to `open` when forecasts are kept |
| `after_agency` | after `_drop_excluded_agencies` |
| `matched` | after `filter_grants_by_keywords` |
| `kept` | final count (exists) |
| `scoring_abort` | the scorer's abort reason, or absent |

Scoring abort:
- Add `abort_reason: Optional[str] = None` to `ScoringOutcome` in
  `llm_utils/relevance.py`, set from `scorer.abort_reason`, and copy it
  into `last_stats` in `_score_relevance`.
- `ingest_detail` appends `" Scoring stopped: <reason>."`.
- `warnings_for` adds "Scoring stopped during the newest ingest: <reason>"
  when the newest ingest's `stats.scoring_abort` is set. The run still
  counts as ok, because the data was loaded.

### B3. Notify counts

- `run_notify.last_counts = {"sent", "failed", "quiet", "recipients"}` on
  the normal path.
- It is reset to `None` at the start of each call. Early exits leave it
  `None`.

### B4. Model server check

`grants/admin.py: llm_check(timeout: float = 5.0) -> dict` reads
`LLMSettings.from_env()`.

- **Not set up.** If `enabled` is false (no model), return
  `{"configured": False}`.
- **Otherwise.** `requests.get(base_url.rstrip("/") + "/models")`, with
  `Authorization: Bearer <api_key>` when a key is set. This works for vLLM
  (`/v1/models`) and Open WebUI (`/api/models`).
- **Return.**
  `{"configured": True, "reachable": bool, "status": int|None,
  "latency_ms": int|None, "host": str, "model": str,
  "model_listed": bool|None, "models": [first 10 ids], "error": str|None}`.
  - `reachable` is true exactly when the status is 200 and the body is
    JSON.
  - Model ids are read from `data[].id`, falling back to
    `models[].id|name`.
  - `host` comes from `_safe_host(url)`: `scheme://hostname[:port]`, with
    no user info, path or query.
  - `error` is a short message (exception type plus first 200 characters).
  - It never raises, and never includes the key.
- **Route.** `GET /admin/api/llm`, behind the password. It is separate from
  `/admin/api` so a slow model server never slows the page; the page calls
  it after the main data loads, and again on "Check now".

### B5. Email status

`admin_status()["email"]`:

- `service` is `get_transport().name`, or None. `get_transport` makes no
  network call.
- `sender` is `sender_address()`.
- `error` is the `ValueError` text when email is misconfigured.
- `last_sent` is `MAX(notifications.sent_at)::timestamptz`.

### B6. Team and profile

`admin_status()["profile"]` comes from `load_profile()`: `configured`,
`summary` (full), `institution`, `keywords`, `keyword_threshold`,
`min_score`, `reminder_days`, `overview_day`, `include_forecasted`,
`agencies_exclude` and `horizons`.

Recipients add `min_score`, the person's own threshold. They never include
`unsubscribe_token`. Use the admin module's own SQL, not
`store.list_recipients()`.

### B7. Settings

`admin_status()["settings"]` holds no secrets:

- `data_source` (`GRANTS_DATA_SOURCE`, default `extract`)
- `lookback_days` (`GRANTS_GOV_LOOKBACK_DAYS`, or None)
- `llm` with:
  - `host` (from `_safe_host`)
  - `model`, `batch_size`, `workers`, `max_per_run`, `timeout`
  - `max_tokens` (0 means automatic)
  - `thinking`: `extra_body.chat_template_kwargs.enable_thinking` when
    present, else None
- `email_max_new` (`GRANTS_EMAIL_MAX_NEW`, default 25)
- `dashboard_url`

It never includes API keys, passwords, tokens, `POSTGRES_URL` or
`GRANTS_SECRET_KEY`.

### B8. Runs for the strip, and logs on demand

- **Runs in `admin_status()`.** `runs` becomes every run from the last 15
  days, capped at 200 rows. Each row adds `id`, `stats` and
  `has_log` (`log IS NOT NULL`); it never carries the log text.
- **Log route.** `GET /admin/api/runs/{run_id}/log` (int id, behind the
  password) returns `{"id", "step", "log"}`, or 404.

### B9. Errors on /admin itself

`/admin` answers 401 and 503 with a `_page()` HTML body. Browsers show that
body when the person cancels the password prompt. The 401 keeps
`WWW-Authenticate`. `/admin/api*` keep JSON errors.

## Page design

**Subject and audience.** One person keeps GrantWatch running for a
research team, and checks this page in the morning. The page should look
like the dashboard: same tokens, same type, light and dark modes. Its one
bold element is the run strip.

**Tokens.** Copy `:root` and the dark-mode block from `index.html`
verbatim: paper, well, ink, mist, fog, spruce, spruce-soft, signal, ochre.
Text is Public Sans, from the same Google Fonts link. Logs only use
`ui-monospace, SFMono-Regular, Menlo, monospace` at 13px. Type scale:
- page title: 22px, weight 600
- verdict: 20px, weight 600
- section heads: 17px, weight 600
- body: 15–16px
- small text: 13px

Use tabular numbers wherever numbers line up. Do not use all-caps labels,
eyebrows or middle-dot separators, and do not append arrows to links.

**Layout.** One left-aligned column, `max-width: 1040px`, with
`padding: 36px 20px 72px` (16px side gutters under 640px), like `.page` in
`index.html`.

```
GrantWatch admin                            Dashboard  Refresh  Updated 10:32 AM
All clear. The daily run finished 3 hours ago and emailed 2 people.   <- verdict

  Ingest  ■ ■ ■ ■ ■ ■ ■ ■ ■ ■ ■ ▨ ■ ■
  Notify  ■ ■ ■ ■ ■ ■ ■ ■ ■ ■ ■ □ ■ ■
          Oct 17                Oct 30        Worked  Failed  Did not finish  No run

[warnings, one per line, 3px signal left border, only when there are any]

How the daily run is doing
  Grants.gov    Working        Daily XML extract, GrantsDBExtract20261030v2.xml
                               83,388 in the extract, 2,140 still open, 1,903 without
                               forecasts, 1,880 after agency exclusions, 399 matched keywords
  Database      Working        1,204 open grants, 1,164 scored, 40 not yet, 6 tracked
  Model server  Not answering  http://10.226.31.254:18100, qwen3.6-35b     [Check now]
                               ConnectTimeout after 5 s
  Email         Working        SMTP from c2sr.und@gmail.com, last sent Oct 30, 7:52 AM;
                               last run sent 2, failed 0

Recent runs                                         [ ] Only problems
  Oct 30, 7:30 AM   Ingest   41 min   Worked   399 kept, 12 newly scored   [Show log]
    (log opens here: monospace, warnings ochre bar, errors signal bar,
     [ ] Only warnings and errors)

Team
  Research profile: full summary paragraph
  Keywords: a, b, c (one sentence)
  Emails grants scoring 4 or higher. Reminders 30, 14, 7 and 2 days before.
  Weekly overview on Monday. Forecasts kept. No agencies excluded.
  Person              Status        Own threshold   Last email
  Harish Kolla        Gets email    Team default    Oct 30
  harish.kolla@und.edu

Emails sent
  Oct 30   harish.kolla@und.edu   3 new grants, 1 reminder, weekly overview

Settings      (two-column definition list)
```

**Run strip.**
- **Grid.** Two rows (Ingest, Notify) by 14 days, with today on the right.
  The page buckets runs by the local date of `started_at` and uses each
  day's newest run per step.
- **Cells.** Each cell is a `<button>`, 18×18 px, 4px gap, 2px radius.
  Under 400px they shrink to 16×16 with a 3px gap.
- **States.** Each state differs by shape as well as color:
  - worked: solid `--spruce`
  - failed: solid `--signal`, with a 2px inset paper notch (a centered bar)
  - did not finish: `--ochre` diagonal hatch (`repeating-linear-gradient`)
  - running: `--ochre` 2px outline
  - no run: 1px `--fog` outline, transparent
- **Labels and keys.** Each cell has an `aria-label` and a `title`, such as
  "Oct 29, ingest: failed". Clicking a cell scrolls to that run in Recent
  runs and opens its log. Day labels sit under the first and last column,
  and a one-line key sits under the strip.

**Steps ("How the daily run is doing").**
- **Rows.** Four rows in data order: Grants.gov, Database, Model server,
  Email. Each row has a name (140px), a state word (130px) and details
  (1fr), plus a button on the Model server row.
- **State words.** Working (spruce); Not answering, Failed or Not set up
  (signal, or mist for Not set up); Checking… (mist).
- **Under 640px.** The name and state go on one line, with the details
  below.

**Recent runs.**
- **Columns.** Rows show Started (local "Oct 30, 7:30 AM"), Step, Took
  ("41 min"; "still running" or "never finished" when unfinished), Result
  (the state word, colored), What happened (detail) and a Show log button
  with `aria-expanded`.
- **Logs.** The button fetches the log once and renders it in a `<pre>`,
  one line per `<span>`. Lines containing `| WARNING |` get an ochre left
  bar; `| ERROR |`, `| CRITICAL |` and traceback lines get a signal bar.
  An "Only warnings and errors" checkbox filters the lines. When there is
  no log, it says "No log was saved for this run."
- **Filter.** An "Only problems" checkbox hides runs that worked.

**States.**
- Loading: "Loading…".
- 401: "Enter the admin password to see this page.", with a "Try again"
  button that reloads.
- 503: the server's message.
- 500: "Could not load the admin data: <detail>".
- Fresh database: an empty strip and the warnings explain the state.
- Model check failure: the error shows in the Model server row.

**Quality floor.**
- All data goes in through `textContent`, never `innerHTML` with data.
- Visible `:focus-visible`.
- No motion except what answers a click; respect `prefers-reduced-motion`.
- No horizontal page scroll at 375px; wide tables scroll inside their own
  wrapper.
- Light and dark modes.

## Alternatives considered

- **Email sign-in.** Rejected: UND mail drops the sign-in links.
- **An open page on the lab network.** Rejected: it lists the team's
  addresses.
- **`kubectl logs` alone.** Today's state. Logs vanish with the Job, and
  reading them needs cluster access.
- **Reading the log file from the web pod.** Impossible: the job writes it
  inside its own short-lived pod. Hence the logs live in `job_runs.log`.
- **JSONB stats.** v1 rejected them because one sentence was enough. The
  funnel and notify counts need numbers per stage, so v2 adds `stats JSONB`.
- **Per-person research interests.** Out of scope. Matching is team-wide;
  per-person interests would mean scoring every grant once per person.

## Tests

Examples in `tests/test_runs.py` and `tests/test_admin.py`, plus Hypothesis
properties (style of `tests/test_planner_properties.py`):

- **Run log (built).** `recorded()` never changes a job's outcome under any
  combination of database failures. `ingest_detail` never raises for any
  stats dict.
- **Log capture.**
  - For any sequence of log calls (levels; messages with Unicode, newlines
    and lengths up to 2,000) inside `recorded()`, the saved log holds the
    last ≤500 lines in order, each ≤500 characters, ≤100,000 in total.
  - The number of handlers on the `grantwatch` logger is the same after
    exit as before, including when the body raises.
  - A raised body's log ends with its traceback.
- **No secrets.** For any random secret strings placed in
  `GRANTS_LLM_API_KEY`, `POSTGRES_PASSWORD`, `POSTGRES_URL`,
  `GRANTS_SECRET_KEY`, `GRANTS_ADMIN_PASSWORD` and `GRANTS_SMTP_PASSWORD`,
  none of them appears anywhere in `json.dumps` of the settings,
  `llm_check()` (fake HTTP) or `email_status`. Generate the strings long
  enough (8+ characters) that they cannot appear by accident.
- **`_safe_host`.** For any URL with user info, path, query or fragment,
  the result has none of them, and it never raises.
- **`llm_check`.** With a fake `requests.get` returning any status, any
  body (JSON or not) or any exception, it never raises, `reachable` is
  true exactly when the status is 200 and the body is JSON, and
  `model_listed` is None unless reachable.
- **Funnel.** An example run of `onlyTheGoodStuff` with stubbed source,
  scorer, summarizer and database checks that `extract ≥ open ≥
  after_forecast ≥ after_agency ≥ matched ≥ kept` and that `source` and
  `source_file` are set.
- **Web.** The routes are password-gated (503/401/200). `/admin` sends HTML
  error bodies. The log route returns 404 for an unknown id. No test opens
  a real database.

## Review resolutions (v2.1)

Two fresh reviewers checked v1 and v2. Their findings are settled below.
**Where this section and the text above disagree, this section wins.**

### Routes and password

- **Routes under `/admin/`.** Under RFC 7617 a password challenged at
  `/admin` covers all of `/`, so the browser would send it with every
  dashboard request. The routes are:
  - `GET /admin/`: the page
  - `GET /admin`: a 307 redirect to `/admin/`, with no password needed
  - `GET /admin/api`: the data
  - `GET /admin/api/llm`
  - `GET /admin/api/runs/{run_id}/log`

  Remove `/api/admin`. The page fetches the relative URLs `api`, `api/llm`
  and `api/runs/<id>/log`. The dashboard footer links to `/admin/`.
- **Password strength.** `GRANTS_ADMIN_PASSWORD` counts as unset (503)
  when it is blank, starts with `REPLACE` (case-insensitive), or is shorter
  than 16 characters. The 503 text says it needs 16 or more ASCII
  characters. Update the README, `.env.example`, `ansible/secrets.example.yml`
  and `k8s/secret.example.yaml` comments to match.
- **HTML errors on the page.**
  - Add `status_code: int = 200` and `headers: Optional[dict] = None` to
    `_page()` in `src/web/app.py`.
  - A shared `_admin_problem(credentials) -> Optional[Tuple[int, str]]`
    returns None when access is allowed.
  - `require_admin` (the API dependency) raises `HTTPException` from it,
    with `WWW-Authenticate` on 401.
  - `admin_page` takes `Depends(_admin_basic)` and, on a problem, returns
    `_page()` with that status. The 401 also sends `WWW-Authenticate`.
  - 401 body: title "Admin password needed", text "Enter the admin
    password to see this page. Any user name works."
  - 503 body: title "The admin page is off", text "Set
    GRANTS_ADMIN_PASSWORD to 16 or more letters and digits on this server
    to turn it on."

### Run log

- **`run_state(finished_at, ok, age_seconds, superseded=False)`.**
  - An unfinished row reads "did not finish" when `superseded` is true (a
    newer row of the same step exists) or when it is older than
    `RUNNING_SECONDS = 95 * 60`.
  - Comment that the cutoff follows `activeDeadlineSeconds: 5400` in
    `k8s/cronjob-daily.yaml`.
  - The runs SQL adds `EXISTS (SELECT 1 FROM job_runs n WHERE n.step =
    r.step AND n.started_at > r.started_at) AS superseded`.
- **`_finish(run_id, step, ok, detail, log, stats)`.**
  - It writes `UPDATE job_runs SET finished_at = NOW(), ok = %s, detail =
    %s, log = %s, stats = %s::jsonb WHERE id = %s`, with stats as
    `json.dumps(stats, default=str)` or None.
  - If that UPDATE fails, it retries once with only `finished_at`, `ok`
    and `detail`, so the outcome is never lost.
  - Before writing, it strips `\x00` from detail, log and stats text.
- **Redaction.**
  - `grants/runs.py: secret_values() -> List[str]` collects the non-empty
    values, 8 characters or longer, of `GRANTS_LLM_API_KEY`,
    `OPENAI_API_KEY`, `POSTGRES_PASSWORD`, `POSTGRES_URL`,
    `GRANTS_SECRET_KEY`, `GRANTS_ADMIN_PASSWORD`, `GRANTS_SMTP_PASSWORD`
    and `GMAIL_TOKEN_JSON`, plus `GRANTS_LLM_BASE_URL` and
    `OPENAI_BASE_URL` (the full strings).
  - `redact(text)` replaces each secret value with `***`; for a base URL
    it uses `safe_host(url)` instead.
  - It applies to every captured log line, the detail and the stats string
    values.
- **Log capture limits.**
  - A line is one physical line after splitting each formatted record on
    `\n`. Keep the last 500 lines, each at most 500 characters.
  - On an exception, append `traceback.format_exc()` after those lines,
    keeping its last 5,000 characters.
  - The total keeps its last 100,000 characters.
  - Attach the handler with `logging.getLogger("grantwatch")`, and export
    `FORMATTER` from `logs/status_logger.py` (with its datefmt) for the
    handler to reuse.
- **Killed runs have no log.** A deadline or OOM kill never reaches
  `_finish`. That is a known limit, and the page says "This run was
  stopped before it could save its log."
- **Exception detail.** When the body raises and had set `run.detail`, that
  detail is kept. Otherwise the detail is `"<Type>: <message>"` (as built).
- **Notify counts.**
  - `run_notify.last_counts` is a dict:
    `{"sent": int, "failed": int, "quiet": int, "recipients": len(recipients)}`.
  - It is also initialised to None at module level next to
    `last_summary`.
  - `notify_step` reads it with `getattr(run_notify, "last_counts", None)`.

### Source and scoring

- **Source keys.**
  - `source` is the source actually used: `extract`, or `export` (unknown
    values fall back to `export`).
  - `source_file` is `Path(...).name` of `gen_extract.last_extract_path`
    for `extract`, or of `latest_file_path` for `export` (from
    `gen_grants.last_download_path` or the cached fallback).
  - Both are set inside `_load_source_records`.
- **Safe host for the scoring abort.**
  - Move the URL helper to `llm_utils/relevance.py` as
    `safe_host(url) -> str`. It returns `scheme://hostname[:port]` and
    never raises; on garbage it returns "unknown".
  - At `relevance.py`, "cannot reach …" uses `safe_host(settings.base_url)`.
  - `grants/admin.py` imports it from there.

### Admin data

- **New columns on existing databases.** `_ensure_runs_table()` also runs
  `ensure_schema()` when `information_schema.columns` lacks
  `job_runs.stats`.
- **Data freshness.**
  - `health` gains
    `data_age_seconds = EXTRACT(EPOCH FROM NOW() - MAX(last_seen_at))`.
  - `warnings_for` warns "Grants.gov data was last updated N ago." when
    it is over 30 hours.
  - It also warns "The newest ingest kept 0 grants. <detail>" when the
    newest ok ingest has `stats.kept == 0`.
  - It warns "Scoring stopped during the newest ingest: <reason>" when
    the newest ingest has `stats.scoring_abort` set.
- **Email.**
  - `last_sent` comes from
    `(SELECT MAX(sent_at)::timestamptz FROM notifications) AS last_sent`
    in `_HEALTH_SQL`, and moves into the `email` block.
  - Emails are grouped by the team's day:
    `(n.sent_at::timestamptz AT TIME ZONE %(tz)s)::date`, where tz is
    `GRANTS_TIMEZONE`, default `America/Chicago`.
- **Function names in `grants/admin.py`.** `email_status()`,
  `settings_summary()`, `profile_summary()`, `run_log(run_id) ->
  Optional[dict]` and `llm_check(timeout=5.0)`. `src/web/app.py` imports
  `admin_status`, `llm_check` and `run_log` by name, so tests can
  monkeypatch them on the app module.
- **Profile mapping.**
  - `configured` comes from `is_configured`, `min_score` from
    `min_score_to_notify`, and `horizons` from `deadline_horizons`.
  - The rest share their names.
- **Settings.**
  - `dashboard_url` comes from `GRANTS_DASHBOARD_URL`.
  - `lookback_days` is an int when it parses, else None.
- **`llm_check`.**
  - It always sends `Authorization: Bearer <api_key>` (the key defaults to
    `ollama`).
  - Its return gains `timeout_s`.
  - `error` is `f"{type(exc).__name__}: {redact(str(exc))}"`, cut to 200
    characters. `redact` also replaces the full request URL with
    `safe_host`, and strips any `user:pass@` and `?query` with a regex.

### Page

- **Verdict.**
  - No warnings: "All clear." followed by "The daily run finished N ago
    and emailed M people." This uses the newest notify's `finished_at`
    and `stats.sent`, or the newest ingest when there is no notify yet.
  - Otherwise: "1 thing needs attention." or "N things need attention."
- **State words in "How the daily run is doing".**

  | Row | Rule |
  |---|---|
  | Grants.gov | From the newest ingest. ok with kept > 0 is Working (spruce). ok with kept 0 is "Nothing loaded" (ochre). failed or did not finish is Failed (signal). running is Running (mist). None is "No run yet" (mist). |
  | Database | Working whenever `/admin/api` answered. |
  | Model server | Not configured: "Not set up" (mist). While checking: "Checking…". Reachable with the model listed: Working. Reachable without it: "Model missing" (signal), with "qwen3.6-35b is not among: a, b, c". Unreachable: "Not answering" (signal), with the error. |
  | Email | `email.error`: "Misconfigured" (signal). No service: "Not set up" (mist). Newest notify failed, did not finish, or has `stats.failed > 0`: "Failing" (signal). Else Working. |

- **Funnel.**
  - It comes from the newest ingest whose stats have `extract`, labelled
    "from the run of Oct 30, 7:30 AM".
  - With forecasts kept, leave out the forecast step and say "forecasts
    kept". Otherwise show "N without forecasts".
  - Source labels: `extract` is "Daily XML extract"; `export` is "Search
    export".
- **Logs.**
  - Everything from a line starting "Traceback (most recent call last):"
    to the end gets the signal bar.
  - Clicking a strip cell unticks "Only problems", scrolls to that run and
    opens its log.
  - "No run" cells are `disabled` buttons.
  - The key includes Running.
  - Recent runs lists every run returned, which is at most 200 over 15
    days.
- **Layout.**
  - A centered container (`margin: 0 auto`, like `.page`) with
    left-aligned text.
  - The failed cell's notch is a centered horizontal bar, 10×2 px, in
    `--paper`.
  - No `innerHTML` anywhere; clear nodes with `replaceChildren()`.
  - `tests/test_admin.py` already checks this.

### Tests to update

These existing tests must change:
- `TestRecorded` compares UPDATE params: compare `params[:2]` and
  `params[-1]`.
- `TestPipelineStats` gets the new funnel keys. Its fixture stubs
  `pipeline.gen_extract` (with `.last_extract_path`) and
  `pipeline.process_extract_xml` with `GRANTS_DATA_SOURCE=extract`, so
  `source` and `source_file` are set.
- `TestSteps` fakes leave `last_counts` alone, so code reads it with
  `getattr`.
- `TestAdminStatus` stubs `load_profile` with `Profile(summary="x")`. It
  extends FakeDb for the new and changed SQL, and reads run fields with
  `.get`.
- `TestAdminAccess` keeps JSON checks for `/admin/api` and expects
  `text/html` plus the status for `/admin/`.

The no-secrets property covers:
- every env var in `secret_values()`, including a `GRANTS_LLM_BASE_URL`
  with `user:pass@` and a `?key=` query, `OPENAI_API_KEY` and
  `GMAIL_TOKEN_JSON`
- unsubscribe tokens in the recipients fixture

It asserts over `json.dumps(admin_status())` (fake DB), `llm_check()`
(fake HTTP, including exceptions whose text contains the URL), a log
captured by `recorded()`, and `ingest_detail` with `scoring_abort` set.
