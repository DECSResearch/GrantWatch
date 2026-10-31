# Match quality, feedback, UX and end-to-end tests

Status: v1.1 (reviewed; the resolutions at the end win over earlier text). Issues: #34–#43. Branch: `match-quality`.

## Problem

GrantWatch had never been checked against what the team actually works on.
On 2026-10-30 we checked it, using every open grant in that day's Grants.gov
extract (1,464) and a draft C2SR profile written from the C2SR site and
hkolla.dev.

**Reference labels.** Every grant that a screen, the keywords or Qwen picked
up was blind-judged on the scorer's own rubric and text (476 grants). Opus
settled the 79 cases where the judge and Qwen disagreed about emailing. Seven
grants are truly a 4 or 5: PESOSE, CICI, FAA aviation research (GNSS), AFRL
Foundations of Trusted Systems, the DoW Cyber Service Academy scholarships,
and two more NSF programs.

Findings:

- **The keyword gate drops real candidates (#41).**
  - The draft keywords match 242 grants, mostly via "workforce" and "security".
  - A screen found 282 grants plausibly relevant, 183 of which match no keyword.
  - The gate drops 1 of the 7 strong grants.
  - Many Defense and NSF calls have little description text to match.
- **The scorer over-rates broad calls and misreads eligibility (#42).**
  - Measured with every open grant scored, as shipped: precision@4 is 14%
    (5 of 35 emailed grants are truly 4 or higher), recall@4 is 71% (5 of 7),
    and scores are within ±1 of the reference on 88% of grants.
  - Almost every false hit is a broad open call, such as YFA, office-wide
    BAAs, MURI, EPSCoR and DURIP.
  - CICI scored 0: Qwen read "Non-profit, non-academic organizations" as the
    only eligible type and missed the next bullet, "Institutions of Higher
    Education".
- **The UX hides or garbles what matters (#35–#40).**
  - The dashboard opens on 9 of the 35 grants that fit.
  - The email lists the grants closing in 7 days 15th to 17th.
  - DURIP and MURI postings are triplicated.
  - Grants.gov placeholder dates and amounts ("2076", "$999,999,999") are
    shown as facts.
  - Track fails when sign-in is off.
  - There are accessibility gaps.
- **No end-to-end test exists (#43).** Every test mocks the database and the
  model.

## Goal

The email and the default dashboard view show the grants the team would
actually want, with deadlines first, and every number can be trusted. The
scorer improves from the team's own feedback (#34). One test suite proves the
whole flow works against a real database.

## A. Matching

### A1. Fact-based pre-check: who may apply (#41)

- **Parse.** `grants_data/parse_extract.py` reads every `EligibleApplicants`
  code into `ELIGIBLE_APPLICANTS` (a sorted list of two-digit strings). The
  JSON export source has no codes and leaves the field empty.
- **Store.** Add `ALTER TABLE grants ADD COLUMN IF NOT EXISTS
  eligible_applicants TEXT`, holding a comma-joined list, and load it in
  `grants/data/loader.py`.
- **Profile.** Add an optional `applicant_codes` list, for example `["06"]`
  for a public university or `["20"]` for a private one. It is documented in
  `config/research_profile.yml` with the code table. Empty, the default,
  turns the pre-check off, so existing setups don't change.
- **Filter.** In `onlyTheGoodStuff`, after the agency exclusion, drop a grant
  only when its codes are known and none of them is in `applicant_codes`,
  `99` (unrestricted) or `25` (others, see text).
  - `stats["after_eligibility"]` records the count, and the admin funnel
    shows it as "N open to your applicant type".
  - On the Oct 30 data this drops 81 grants, none judged 3 or higher.
- **Tell the scorer.** `_record_block` adds a line `Who may apply: <types in
  words>` before the description, using the code table (a module constant).
  Round 6 measures whether this helps (below).

### A2. Score every open grant when a model is configured (#41)

- **Ignore keywords.** When `LLMSettings.enabled` and the profile is
  configured, the pipeline skips `filter_grants_by_keywords`. It still
  computes `MATCHED_KEYWORDS` for display, with threshold 0 semantics.
- **Without a model.** Behaviour is unchanged: the keyword filter decides.
- **Cost.**
  - Scores are cached per profile fingerprint, so after the first run only
    new or changed grants are scored.
  - The first run is held to `GRANTS_RELEVANCE_MAX_PER_RUN` (default 400)
    and fills in over a few days. Each day's soonest deadlines go first, so
    the most urgent are covered first.
  - The admin page already warns about unscored grants (A2 changes its
    wording, see #40).
- **Stats.** `stats["matched"]` becomes "sent to the model". The admin page
  says "Keywords do not filter; every open grant is scored" in this mode
  (#40).

### A3. What the scorer sees, and the prompt (#42)

**Eligibility truncation bug.** `_record_block` cuts the eligibility text at
700 characters.
- NSF lists who may submit as bullets, and for PESOSE the "Institutions of
  Higher Education" bullet starts at character 822. Qwen sees only
  for-profits, non-academic nonprofits and federal agencies, and correctly
  concludes universities can't apply, so it scores 0.
- 12 open grants lose the universities bullet this way. Two of them are
  reference 4–5 grants (25-515, 26-503).
- **Fix:** send the eligibility text up to the same limit as the description
  (`GRANTS_LLM_MAX_CHARS`, 3000). Across 3 grants per request this adds at
  most about 2,000 tokens, well inside the model's context.

**Noise.** Two identical runs (rounds 1 and 2, temperature 0) give the same
score on only 69% of grants and flip 21 email decisions. Two consequences:
- Recall over 7 positives moves by 14 points per grant, so rounds are judged
  on precision, the ±1 agreement and the mean error too, not on recall alone.
- A change that makes scores steadier (for example one grant per request)
  is worth something in itself.

The tuning rounds decide the prompt and settings. A change is adopted only
if it improves precision@4 without lowering recall@4.

| Round | Change | Precision@4 | Recall@4 | Within ±1 | MAE |
|---|---|---|---|---|---|
| 1 | shipped prompt, batch 3 (all open grants) | 14% (5/35) | 71% | 88% | 0.64 |
| 2 | round 1 repeated (noise) | 13% (4/30) | 57% | 88% | 0.63 |
| 3 | batch 1 | 18% (3/17) | 43% | 93% | 0.52 |
| 4 | eligibility rule, full eligibility text | 15% (5/33) | 71% | 86% | 0.67 |
| 5 | round 4 plus broad-program calibration | **45% (5/11)** | 71% | 94% | 0.52 |
| 6 | round 5, "Who may apply" line, batch 1 | 80% (4/5) | 57% | 95% | 0.42 |
| 7 | round 5 repeated (noise) | 42% (5/12) | 71% | 94% | 0.51 |
| 8 | round 5 with reason before score (eligibility at 2,000) | 18% (6/34) | 86% | 91% | 0.52 |

**Adopted: round 5.** Eligibility rule, broad-program calibration, eligibility
text up to 2,000 characters, 3 grants per request, and score before reason
(unchanged).
- **Stability.** Round 7 repeats round 5 and confirms it. The new prompt is
  also much steadier: 92% of scores are identical between two runs, against
  69% for the shipped prompt, with 3 email flips instead of 21.
- **Round 6 rejected (recall falls).** With batch 1, the model writes the
  score before reasoning, and on CICI it wrote 0 and then argued itself to a
  2 in the reason text. The "Who may apply: Others" line gives nothing for
  NSF's code-25 grants. So no "Who may apply" line.
- **Round 8 rejected (precision falls).** Reasoning first raises recall but
  talks the model into high scores for broad calls again, and is less stable
  (59% identical to round 5).

The exact adopted wording is in A3's prompt text below (resolutions).

- **Round 4.** It fixes the eligibility misreads: CICI 0 → 3 and PESOSE 0 → 5. It does not touch the broad calls.
- **Round 5.** It cuts emailed grants from 35 to 11 without losing a strong one, and all 11 are a 3 or higher by the reference. The two misses (the DoW Cyber Service Academy and FAA aviation research) are 3-versus-4 calls.
- **Caveat.** The reference adjudicator and the round-5 prompt share the same reading of the rubric for broad calls (2–3 unless the text names the group's topics). Part of the gain is agreement with that reading. Whether it matches the team's taste is what the feedback loop (A4) measures.
| 4 | eligibility rule only | TBD | | |
| 5 | eligibility and broad-program calibration | TBD | | |
| 6 | best of the above, plus "Who may apply" | TBD | | |

### A4. Feedback: Relevant / Not for us (#34)

- **Table.**

  ```sql
  CREATE TABLE IF NOT EXISTS grant_feedback (
      opp_id TEXT NOT NULL,
      email TEXT NOT NULL,
      verdict SMALLINT NOT NULL CHECK (verdict IN (-1, 1)),
      title TEXT, agency TEXT, summary TEXT,
      created_at TIMESTAMP NOT NULL DEFAULT NOW(),
      PRIMARY KEY (opp_id, email)
  );
  ```

  `title`, `agency` and `summary` are copied in when the vote is cast, so an
  example survives after the grant leaves the extract.
- **API.** `POST /api/feedback {opp_id, verdict}` stores or replaces a vote,
  and `DELETE /api/feedback/{opp_id}` clears it. Both use `require_member`,
  as Track does. `GET /api/grants` adds `my_verdict` and `votes_up` /
  `votes_down` per grant.
- **Dashboard.** Two small buttons next to Track: "Relevant" and "Not for
  us", with `aria-pressed`. They are hidden when sign-in is off (#38).
- **Examples in the prompt.** `feedback_examples(limit=10)` returns, per grant:
  - the net verdict (the sign of the sum; ties are dropped);
  - the most recent grants first;
  - up to `limit // 2` of each verdict, filling from the other side if one
    is short;
  - each entry as `+ Title (Agency): summary` or `- …`, cut to 200
    characters.

  `RelevanceScorer` appends an "Examples the team has marked" section to the
  system prompt after the profile. It holds at most 10 entries, about 600
  tokens.
- **Rescoring.** `Profile.fingerprint()` gains a `feedback_version`: the
  number of votes divided by 10, rounded down, so it changes once per 10 new
  votes. Old scores are recomputed after every 10 votes, never on every
  click, and only by the next ingest, within the per-run cap.
- **Admin.** A "Feedback" line under the Team section: total votes, and the
  share of grants emailed in the last 30 days that someone marked Not for us.

## B. UX (#35–#40)

- **Dashboard (#35).**
  - The closing window defaults to any date.
  - The filter uses `<=`.
  - Clear resets the closing window to any date and the score floor to the
    team threshold.
  - The status line adds "N score 4 or higher".
- **Email (#36).**
  - New grants are sorted with those closing within 30 days first, then by
    score.
  - The heading reads "(25 of 35)" when some are held back.
  - `overview` is recorded only if a section rendered.
  - The summary shows as a muted line with the reason under it.
  - The HTML is wrapped in a minimal document with `color-scheme: light`.
- **Data hygiene (#37).**
  - Unescape HTML entities in the title, agency and description at ingest.
  - A close date more than 5 years out shows as "no firm deadline", and the
    day count is left out past 365 days.
  - Hide ceilings of 999,999,999 or more.
  - Duplicates, handled by a function in `grants/display.py` (new):
    - **Same program from several services:** grants with the same close
      date and the same trailing parenthesized acronym collapse into one
      row with "Also posted by: …" and each posting's link.
    - **Superseded solicitations:** a grant whose normalized title matches
      a newer posting is hidden, with "1 older posting hidden" shown.
    - The dashboard and the email both use this function.
- **Sign-in off (#38).** Track, the "Only tracked" filter and the feedback
  buttons are hidden, and a muted note says tracking is off on this server.
  The email footer's tracking line is left out when no secret key is set.
- **Accessibility and phone (#39).** As listed in the issue:
  - the strip becomes a group with focus moved after jumping, larger hit
    areas and same-day offsets;
  - the first axis label is dropped on narrow screens;
  - buttons get `aria-describedby`;
  - a `--line` token is used for control borders;
  - the search field goes full width with a shorter placeholder;
  - a visible Grants.gov link is added.
- **Admin copy (#40).**
  - The unscored warning reads the newest ingest stats: failed, deferred or
    unreachable.
  - The keyword lines change in score-everything mode.
  - The overview label follows #36.

## C. End-to-end tests (#43)

`tests/e2e/` is skipped unless `GRANTS_TEST_POSTGRES_URL` is set.

- **Fixture.** `tests/e2e/fixtures/extract.xml` holds about 12 grants, built
  to cover these cases:
  - a strong fit;
  - a broad call;
  - states-only (the pre-check drops it);
  - "others, see text";
  - a placeholder date and a placeholder ceiling;
  - a DURIP-style triple;
  - a superseded pair;
  - an entity in the title;
  - a closed grant;
  - a forecast.

  `gen_extract` is monkeypatched to return this file, so the test never
  downloads.
- **Fake model server.** `http.server` runs on a thread and serves `/models`
  and `/chat/completions`. It answers with scores keyed by opportunity
  number, records every request body (to check the feedback examples and
  "Who may apply"), and can be switched to fail (connection refused, a 500,
  or a truncated answer).
- **Database.** Each test module creates a fresh schema, using `SET
  search_path` through `POSTGRES_URL` options, and drops it afterwards.
- **Flows:**
  1. **Ingest.** The pre-check drops the states-only grant. Every remaining
     open grant is scored, `job_runs` has an ok ingest with the funnel
     stats, and entities are unescaped.
  2. **Notify to the outbox.** Grants closing within 30 days come first, the
     heading reads "(N of M)" when capped, the triple is collapsed, and
     there is no "2076" and no "$999,999,999".
  3. **Dry run.** `notify --dry-run` writes no `job_runs` row.
  4. **Web.**
     - The dashboard API returns grants with `my_verdict`.
     - A signed-in member votes. The next ingest, with the fingerprint
       version changed, sends the examples in the prompt.
     - `/admin/api` reflects all of the above, with a test admin account.
  5. **Failure paths.** With the model unreachable, ingest still loads, and
     `job_runs` and the admin warnings say why. With a truncated answer, the
     scoring-abort warning appears.
- **CI.** `ci.yml` gains a `postgres:16` service and sets
  `GRANTS_TEST_POSTGRES_URL`, so the suite runs on every PR.

## Tests (properties, Hypothesis)

- **Pre-check:** it never drops a grant whose codes include `applicant_codes`,
  `99` or `25`, or that has no codes. It drops exactly those with known codes
  outside that set.
- **Feedback examples:**
  - at most `limit`, balanced as specified;
  - ties are excluded;
  - each entry is at most 200 characters;
  - a person's new vote replaces their old one.
- **Duplicate grouping:**
  - every input grant appears exactly once, as a row or under "also posted
    by" / "older posting hidden";
  - a group never mixes close dates.
- **Placeholder rules:** a ceiling of 999,999,999 or more is never shown.
  Close dates more than 5 years out never show a day count.
- **Email order:** every grant closing within 30 days comes before every
  other grant, and the order within each part is by score, then deadline.

## Not in scope

- Fetching full announcements (NOFO PDFs) for borderline grants. That needs
  section extraction to stay inside the model's context, and comes after the
  feedback loop proves useful.
- One-click 👍/👎 links in the email, which need signed tokens.
- Writing the team's real profile. `profile-c2sr-v2.yml` (outside the repo,
  because the repo is public) is a draft for the team to edit and install as
  `config/research_profile.local.yml`.

## Review resolutions (v1.1)

Three fresh reviewers (matching, UX, tests) each said go-with-changes. These
resolutions win over the text above.

### Scoring and cache

- **Scorer version in the fingerprint.**
  - `llm_utils/relevance.py` gains `SCORER_VERSION`, bumped whenever the
    prompt or the record text changes. The score cache key becomes the
    profile fingerprint plus `SCORER_VERSION` plus the model name, built in
    one helper used by `_score_relevance`, `fetch_relevance`, the cache file
    and `ScoringOutcome`.
  - This branch bumps it once. The result is one deliberate full rescore,
    under the existing per-run cap.
  - Content edits to a grant still don't trigger a rescore. That's
    acceptable, and the doc no longer claims otherwise.
- **Eligibility limit.** Eligibility is cut at **2,000** characters, not
  3,000. The latest universities bullet in the Oct 30 data is at 1,881.
  - The `_record_block` docstring's context advice rises to 12k tokens.
  - A context-length error must not advance the response-format fallback.
- **Throughput and production.**
  - Round 1 scored 1,463 grants in 16 minutes (3 per request, 3 workers).
  - Production sets `GRANTS_RELEVANCE_MAX_PER_RUN=1500` with a 90-minute
    job deadline, so a full rescore fits in one run.
  - Scores are written to the database only at the end of the run. A run
    killed mid-scoring loses its work. That's acceptable at this throughput;
    no per-batch saving.
- **Model-configured but nothing scored.** In the notify planner's fallback
  (`scoring_active` false), a grant is announced only if
  `matches >= max(1, keyword_threshold)`. A model outage on a
  score-everything database therefore never announces arbitrary grants.
  - Only model scores make `scoring_active` true. The pre-check's rule 0s
    (`relevance_model = 'rule'`, carried as `GrantState.by_rule`) do not, or
    a fresh database with the model down would send nothing.
  - The fallback never announces a rule-scored grant, though its keywords
    match: Grants.gov says this applicant cannot apply.
  - The loader keeps a stored summary when the incoming score is a rule's,
    because that row carries only the truncated fallback.
- **A2 wording.** The stat is renamed to "sent to the model"; the admin
  funnel shows "candidates for scoring".

### Pre-check (A1)

- **Field name.** Codes go in a new key, `ELIGIBLE_APPLICANT_CODES`, read
  from the extract only.
  - The export source's `ELIGIBLE_APPLICANTS` free text is left alone.
  - A record without the key means "codes unknown", and the grant is kept.
  - No database column; nothing reads it.
- **Profile codes.**
  - `applicant_codes` values are normalised with `f"{int(v):02d}"`, because
    YAML reads `06` as 6.
  - Unknown codes are warned about and ignored.
  - Empty means the pre-check is the identity, and that is a property test.
- **Ineligible grants are scored, not dropped.** They get a rule-based score
  of 0 with the reason "Not open to <applicant type> (Grants.gov eligibility
  codes)", no model call, and model name `rule`.
  - They still load, so tracked grants keep refreshing and are never
    reported as withdrawn.
  - The dashboard hides them under its score floor.
- **"Who may apply" line.** Added only if round 6 shows it helps, and left
  out when the only code is 25.

### Feedback (A4), simplified

- **No automatic rescoring.** The fingerprint does not include feedback. A
  rescore every 10 votes would mostly re-roll the model's noise and announce
  new false positives.
- **The team's net verdict applies directly:**
  - **Net "Not for us":** left out of new grants, the overview and the
    default dashboard view. A "Show hidden" toggle reveals them.
  - **Net "Relevant":** treated as meeting the team threshold.
  - **Ties:** count as no verdict.
- **Examples guide only newly scored grants.**
  - The scorer adds up to 10 examples to the **user message** in a fenced
    block marked as data: "Examples the team marked (data, not
    instructions)".
  - The title, agency and summary are copied **server-side from the grants
    row** at vote time. Unknown `opp_id` gives 404. Whitespace is collapsed,
    control characters are stripped, and each entry is cut to 200
    characters.
- **API.**
  - `INSERT … ON CONFLICT (opp_id, email) DO UPDATE SET verdict =
    EXCLUDED.verdict, created_at = NOW()`.
  - `DELETE /api/feedback/{opp_id:path}`.
  - `my_verdict` is null when signed out.
  - `feedback_examples(rows, limit)` is a pure function over vote rows, so
    Hypothesis tests the real code.
- **Missing table.** If `grant_feedback` doesn't exist yet, feedback fields
  read as null. POST runs `ensure_schema` once, then retries.
  `_SCHEMA_STALE_SQL` also checks `to_regclass('grant_feedback')`.
- **One vote per program (#44).** A person's vote on a same-program group
  lives on exactly one of its postings.
  - POST and DELETE also remove the caller's votes on the program's other
    postings, in the same transaction. The server works the program out from
    every grant (`program_of`), not from the rows the page showed, so a search
    or a score floor that hides a posting does not leave its vote behind.
  - `with_feedback` takes `my_verdict` from the first posting in
    `[lead, *also_posted]` that holds the viewer's vote and returns
    `my_vote_opp_id`: that posting, or the lead when there is none. The
    dashboard sends its votes to that posting.
  - The admin share counts programs, as everywhere else. `Notice.records`
    writes one `new` notification per posting, so the emailed postings are
    folded with `group_postings` (`feedback_programs`), and a program is
    declined when any posting of it has a Not for us vote.

### UX

- **Superseded solicitations are annotated, never hidden.** A muted line
  reads "A newer solicitation exists: <number>". The rule: same
  `agency_code`, both Posted, titles equal after dropping one trailing
  parenthetical, and the older one posted at least 2 years before.
- **Same-program groups.** The rule: same close date, the same single-token
  all-caps trailing acronym, and every member from a different
  `agency_code`. A token with a digit is not an acronym (an NIH activity
  code such as R01 names a mechanism that two services use for different
  grants); hyphens are fine, as in S-STEM.
  - Grouping runs once, in `grants/display.py`, and both the planner and
    `/api/grants` use it.
  - A group is announced if any member was. The cap counts groups, every
    member is recorded as shown, and the group score is the maximum.
  - Counts everywhere mean rows (groups).
  - Golden test: on the fixture, only DURIP-style and MURI-style triples
    collapse.
- **Overview cadence kept.**
  - When every overview grant is already listed under New, the email shows
    the line "This week's deadlines are all in the list above." So the
    overview is honestly shown, and it is recorded as now.
  - The admin label then stays true.
- **Email order.**
  - `URGENT_DAYS = 30`. Grants closing within it come first, deadline
    first; the rest follow by score, then deadline.
  - The cap applies to that order, so urgent grants never roll over. The
    keyword fallback uses the same split.
  - The existing best-first property is rewritten accordingly.
- **Placeholders at the data layer.** In `serialize_row` and `GrantState`:
  - a close date more than 5 years out gives `days_left = None` and
    `firm_deadline = false`;
  - an award ceiling of 999,999,999 or more becomes null.

  The dashboard, email and planner all read these. Titles are unescaped with
  `html.unescape` in `normalize_records`.
- **Dashboard.**
  - The page awaits the session before the first render.
  - `<=` with a date computed in Python.
  - `fit_count` uses the team threshold.
  - "showing 500 of N" appears when the limit is hit.
  - The button is renamed "Reset filters".
  - Row ids are index-based, rows get `tabindex=-1`, and vote buttons use
    `.vote`.
  - `--line` is added to both `:root` blocks (dashboard and admin).
  - New notes reuse the `.meta` and `.why` styles.
- **Sign-in off.**
  - The Track toggle and the vote buttons are hidden.
  - Tracked grants show a read-only "Tracked" label, since the MCP server
    can still track.
  - "Only tracked" is shown when anything is tracked.
  - The email footer's reminder line depends on whether the recipient has
    tracked grants, not on the secret key.
- **Scope.** Notifications may move to Akshay's service. Email changes stay
  small: the order, "(N of M)", the overview line, the summary line, and the
  HTML wrapper.

### End-to-end tests

- **Environment isolation (blocker).** A module-scoped fixture:
  - clears every `_DSN_ENV_VARS` name, sets `POSTGRES_URL` to the test URL
    with `options=-csearch_path=<schema>`, and sets
    `GRANTS_PROFILE_FILE=<fixture profile>`;
  - selects the outbox transport, with every SMTP and Gmail variable
    cleared;
  - patches `grants_data.pipeline.gen_extract` by name and points
    `relevance_cache._CACHE_PATH` and `pipeline._CSV_DIR` at `tmp_path`;
  - calls `webapp._profile.cache_clear()`;
  - blocks outbound sockets except to 127.0.0.1.
- **Fixture dates.** The fixture is a template rendered at test time with
  relative dates, every one at least 2 days from a cutoff.
  - The entity case is written as `&amp;amp;`.
  - Added cases:
    - a relevant grant with no keyword match;
    - a grant with no `EligibleApplicants`;
    - a grant whose universities bullet is after character 700 (assert it
      reaches the model).
- **Fake model server.**
  - Truncated mode returns `finish_reason: "length"`.
  - It sends `x-should-retry: false`; tests set `GRANTS_LLM_TIMEOUT=5` and
    `GRANTS_LLM_WORKERS=1`.
  - No 500 mode.
- **Flows.**
  - Flows 4 and 5 get their own schema and an empty cache.
  - Flow 4 votes, then adds a new grant before the second ingest, and
    asserts the examples are in that grant's request.
  - Flow 3 folds into flow 2.
  - Placeholder checks assert on `/api/grants`.
- **CI.** The `postgres:16` service has a health check.

### Property tests, replaced or added

- **Grouping:**
  - never groups two postings with the same `agency_code`;
  - never mixes close dates;
  - output does not depend on input order (ties broken by `opp_id`);
  - a superseded grant is only ever annotated.
- **Pre-check:** empty `applicant_codes` is the identity. A record without
  codes is kept.
- **Email order:** every urgent grant comes before every non-urgent one. The
  cap never drops an urgent grant while a non-urgent one is shown.
- **Feedback examples:**
  - bounded and balanced;
  - ties excluded;
  - entries at most 200 characters;
  - the order of vote rows doesn't change the result.


### Adopted prompt text (replaces the eligibility sentence in `_SYSTEM_PROMPT`)

```text
Judge fit from the substance of the description, not from keyword overlap.
Eligibility: give 0 only when the text clearly excludes a US public university
(for example only states, tribes, small businesses, individuals, named
institutions, or foreign entities may apply). Eligibility lists are often
bulleted; read every item. If any item covers institutions of higher
education, the group is eligible. If eligibility is unclear, judge on fit.

Broad programs: agency-wide research announcements, early-career awards
(CAREER, young investigator, young faculty), EPSCoR, instrumentation and
"any field of science" calls are open to almost everyone. Give them 2 or 3
unless the text itself names one of the group's research topics; 4 if it
names one as a listed interest; 5 only for programs whose stated purpose is
one of the group's research lines.
```
