"""Read-only numbers for the admin page: did the daily job run, does scoring
work, who was emailed what, and how the team and the settings look."""
from __future__ import annotations

import os
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import requests
from psycopg2.extras import RealDictCursor

from grants.profile import Profile, load_profile
from grants.runs import redact
from grants.sql_utils import db_connection, ensure_schema, open_grant_sql
from llm_utils.relevance import LLMSettings, safe_host
from notifications.notify import settings_from_profile
from notifications.transport import get_transport, sender_address

# The job starts at 07:30 Central and may run for 90 minutes, so a day-old
# run is normal; past this the daily job has missed a day.
STALE_INGEST_SECONDS = 30 * 3600
STALE_DATA_SECONDS = 30 * 3600
# A run with no end time is still going until this long after it started: the
# job's activeDeadlineSeconds (5400, in k8s/cronjob-daily.yaml) plus five minutes.
RUNNING_SECONDS = 95 * 60

_RUN_DAYS = 15
_RUN_LIMIT = 200
_EMAIL_LIMIT = 30
_MODEL_LIMIT = 10
_DEFAULT_TIMEZONE = "America/Chicago"

_RUN_FIELDS = ("id", "step", "started_at", "finished_at", "ok", "detail", "stats", "has_log", "age_seconds", "duration_seconds")
_HEALTH_FIELDS = (
    "data_as_of", "data_age_seconds", "open", "open_scored", "open_unscored", "watched",
    "last_ok_ingest_age_seconds", "last_ok_notify_age_seconds",
)
_RECIPIENT_FIELDS = ("email", "name", "min_score", "active", "unsubscribed_at", "last_emailed")
_EMAIL_FIELDS = ("email", "day", "new", "reminder", "changed", "removed", "overview")


def run_state(finished_at: Any, ok: Optional[bool], age_seconds: Optional[float], superseded: bool = False) -> str:
    """ok, failed, running, or did not finish (killed before it could record an end).

    A run with no end is not running once a newer run of its step exists.
    """
    if finished_at is None:
        gone = superseded or (age_seconds is not None and age_seconds > RUNNING_SECONDS)
        return "did not finish" if gone else "running"
    return "ok" if ok else "failed"


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} minutes"
    hours = round(seconds / 3600)
    return f"{hours} hours" if hours < 48 else f"{round(seconds / 86400)} days"


def _newest(runs: List[Dict[str, Any]], step: str, state: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return next((run for run in runs if run.get("step") == step and (state is None or run.get("state") == state)), None)


def _stats(run: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    stats = (run or {}).get("stats")
    return stats if isinstance(stats, dict) else {}


def _with_detail(message: str, run: Dict[str, Any]) -> str:
    return f"{message} {run['detail']}" if run.get("detail") else message


def _unhappy(run: Optional[Dict[str, Any]], step: str) -> Optional[str]:
    if run is None or run.get("state") not in ("failed", "did not finish"):
        return None
    return _with_detail(f"The newest {step} run {'failed' if run['state'] == 'failed' else 'did not finish'}.", run)


def warnings_for(health: Dict[str, Any], runs: List[Dict[str, Any]], recipient_count: int, scoring_on: bool) -> List[str]:
    """Plain sentences about anything that looks wrong; no clock, only the ages from the database."""
    found: List[str] = []
    last_ingest = _newest(runs, "ingest")
    ok_age = health.get("last_ok_ingest_age_seconds")
    if last_ingest is None and ok_age is None:
        found.append("No daily run has been recorded yet.")
    elif ok_age is None:
        found.append("No ingest has finished successfully yet.")
    elif ok_age > STALE_INGEST_SECONDS:
        found.append(f"The last successful ingest was {_ago(ok_age)} ago; the daily job should run about every 24 hours.")
    data_age = health.get("data_age_seconds")
    if data_age is not None and data_age > STALE_DATA_SECONDS:
        found.append(f"Grants.gov data was last updated {_ago(data_age)} ago.")
    for run, step in ((last_ingest, "ingest"), (_newest(runs, "notify"), "notify")):
        problem = _unhappy(run, step)
        if problem:
            found.append(problem)
    good_ingest = _newest(runs, "ingest", "ok")
    if good_ingest is not None and _stats(good_ingest).get("kept") == 0:
        found.append(_with_detail("The newest ingest kept 0 grants.", good_ingest))
    abort = _stats(last_ingest).get("scoring_abort")
    if abort:
        found.append(f"Scoring stopped during the newest ingest: {abort}")
    unscored = int(health.get("open_unscored") or 0)
    if scoring_on and unscored > 0:
        found.append(
            f"{unscored:,} open {'grant has' if unscored == 1 else 'grants have'} no relevance score yet; "
            "the model server may be unreachable, or the per-run cap left them for later."
        )
    if recipient_count == 0:
        found.append("Nobody gets email yet: there are no active recipients.")
    return found


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def _pick(row: Dict[str, Any], fields: tuple) -> Dict[str, Any]:
    """Only the named fields, so a column added to a query never reaches the page by accident."""
    return {field: row.get(field) for field in fields}


_SCHEMA_STALE_SQL = """
    SELECT to_regclass('job_runs') IS NULL OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'job_runs' AND column_name = 'stats'
    );
"""


def _ensure_runs_table() -> None:
    """Apply the schema once if the daily job has not created job_runs (or its newest column) yet.

    Checked first so a normal page load never runs the schema's ALTER TABLEs,
    which could wait behind a running ingest.
    """
    with db_connection() as conn, conn.cursor() as cur:
        cur.execute(_SCHEMA_STALE_SQL)
        stale = cur.fetchone()[0]
    if stale:
        ensure_schema()


# Timestamps are cast to timestamptz so the JSON carries a UTC offset and the
# page shows the right local time. The log text is never selected here.
_RUNS_SQL = f"""
    SELECT r.id, r.step, r.started_at::timestamptz AS started_at, r.finished_at::timestamptz AS finished_at,
           r.ok, r.detail, r.stats, (r.log IS NOT NULL) AS has_log,
           EXTRACT(EPOCH FROM NOW() - r.started_at)::float AS age_seconds,
           EXTRACT(EPOCH FROM r.finished_at - r.started_at)::float AS duration_seconds,
           EXISTS (SELECT 1 FROM job_runs n WHERE n.step = r.step AND n.started_at > r.started_at) AS superseded
    FROM job_runs r
    WHERE r.started_at > NOW() - INTERVAL '{_RUN_DAYS} days'
    ORDER BY r.started_at DESC, r.id DESC
    LIMIT {_RUN_LIMIT};
"""

_HEALTH_SQL = f"""
    SELECT (SELECT MAX(last_seen_at)::timestamptz FROM grants) AS data_as_of,
           (SELECT EXTRACT(EPOCH FROM NOW() - MAX(last_seen_at))::float FROM grants) AS data_age_seconds,
           COUNT(*) FILTER (WHERE {open_grant_sql("g")}) AS open,
           COUNT(*) FILTER (WHERE ({open_grant_sql("g")}) AND g.relevance_score IS NOT NULL) AS open_scored,
           COUNT(*) FILTER (WHERE ({open_grant_sql("g")}) AND g.relevance_score IS NULL) AS open_unscored,
           (SELECT COUNT(*) FROM grant_watchlist) AS watched,
           (SELECT EXTRACT(EPOCH FROM NOW() - MAX(started_at))::float FROM job_runs
             WHERE step = 'ingest' AND finished_at IS NOT NULL AND ok) AS last_ok_ingest_age_seconds,
           (SELECT EXTRACT(EPOCH FROM NOW() - MAX(started_at))::float FROM job_runs
             WHERE step = 'notify' AND finished_at IS NOT NULL AND ok) AS last_ok_notify_age_seconds,
           (SELECT MAX(sent_at)::timestamptz FROM notifications) AS last_sent
    FROM grants g;
"""

# The unsubscribe token is deliberately not selected.
_RECIPIENTS_SQL = """
    SELECT r.email, r.name, r.min_score, r.active, r.unsubscribed_at::timestamptz AS unsubscribed_at,
           MAX(n.sent_at)::timestamptz AS last_emailed
    FROM recipients r
    LEFT JOIN notifications n ON n.recipient_id = r.id
    GROUP BY r.id
    ORDER BY r.email;
"""

# Counted by the team's calendar day, not the database's.
_EMAILS_SQL = f"""
    SELECT r.email, n.day,
           COUNT(*) FILTER (WHERE n.kind = 'new') AS "new",
           COUNT(*) FILTER (WHERE n.kind = 'reminder') AS reminder,
           COUNT(*) FILTER (WHERE n.kind = 'changed') AS changed,
           COUNT(*) FILTER (WHERE n.kind = 'removed') AS removed,
           COUNT(*) FILTER (WHERE n.kind = 'overview') AS overview
    FROM (
        SELECT recipient_id, kind, (sent_at::timestamptz AT TIME ZONE %(tz)s)::date AS day FROM notifications
    ) n
    JOIN recipients r ON r.id = n.recipient_id
    GROUP BY r.email, n.day
    ORDER BY n.day DESC, r.email
    LIMIT {_EMAIL_LIMIT};
"""

_LOG_SQL = "SELECT id, step, log FROM job_runs WHERE id = %s;"


def email_status(last_sent: Any = None) -> Dict[str, Any]:
    """Which email service is set up (no network call), who it sends as, and when it last sent."""
    error = None
    try:
        transport = get_transport()
    except ValueError as exc:
        transport, error = None, redact(str(exc))
    return {
        "service": transport.name if transport else None,
        "sender": redact(sender_address()) or None,
        "error": error,
        "last_sent": _iso(last_sent),
    }


def profile_summary(profile: Optional[Profile] = None) -> Dict[str, Any]:
    """What the team's research profile says, for the Team section."""
    profile = profile or load_profile()
    return {
        "configured": profile.is_configured,
        "summary": profile.summary,
        "institution": profile.institution,
        "keywords": list(profile.keywords),
        "keyword_threshold": profile.keyword_threshold,
        "min_score": profile.min_score_to_notify,
        "reminder_days": list(profile.reminder_days),
        "overview_day": profile.overview_day,
        "include_forecasted": profile.include_forecasted,
        "agencies_exclude": list(profile.agencies_exclude),
        "horizons": list(profile.deadline_horizons),
    }


def settings_summary() -> Dict[str, Any]:
    """The settings that shape a run. Nothing secret: no keys, passwords, tokens or database address."""
    llm = LLMSettings.from_env()
    chat_template = llm.extra_body.get("chat_template_kwargs")
    thinking = chat_template.get("enable_thinking") if isinstance(chat_template, dict) else None
    try:
        lookback_days = int((os.getenv("GRANTS_GOV_LOOKBACK_DAYS") or "").strip())
    except ValueError:
        lookback_days = None
    notify = settings_from_profile(Profile())  # only its environment settings are used
    return {
        "data_source": (os.getenv("GRANTS_DATA_SOURCE") or "extract").strip().lower(),
        "lookback_days": lookback_days,
        "llm": {
            "host": safe_host(llm.base_url),
            "model": llm.model,
            "batch_size": llm.batch_size,
            "workers": llm.workers,
            "max_per_run": llm.max_per_run,
            "timeout": llm.timeout,
            "max_tokens": llm.max_tokens,
            "thinking": thinking if isinstance(thinking, bool) else None,
        },
        "email_max_new": notify.max_new,
        "dashboard_url": notify.dashboard_url,
    }


def _model_ids(body: Any) -> List[str]:
    """Model ids from an OpenAI-style ``data`` list, else a ``models`` list (``id`` or ``name``)."""
    if not isinstance(body, dict):
        return []
    for key in ("data", "models"):
        items = body.get(key)
        ids = []
        for item in items if isinstance(items, list) else []:
            name = (item.get("id") or item.get("name")) if isinstance(item, dict) else None
            if name:
                ids.append(str(name)[:200])
        if ids:
            return ids
    return []


def _short_error(exc: BaseException, *urls: str) -> str:
    try:
        text = str(exc)
    except Exception:
        text = ""
    return f"{type(exc).__name__}: {redact(text, urls)}"[:200]


def llm_check(timeout: float = 5.0) -> Dict[str, Any]:
    """Ask the model server which models it serves. Never raises and never includes the key."""
    settings = LLMSettings.from_env()
    if not settings.enabled:
        return {"configured": False}
    url = settings.base_url.rstrip("/") + "/models"
    result: Dict[str, Any] = {
        "configured": True, "reachable": False, "status": None, "latency_ms": None,
        "host": safe_host(settings.base_url), "model": settings.model, "model_listed": None,
        "models": [], "error": None, "timeout_s": timeout,
    }
    started = time.monotonic()
    try:
        reply = requests.get(url, headers={"Authorization": f"Bearer {settings.api_key}"}, timeout=timeout)
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
        result["status"] = reply.status_code
        try:
            body, is_json = reply.json(), True
        except Exception:
            body, is_json = None, False
        if reply.status_code != 200:
            result["error"] = f"The server answered HTTP {reply.status_code}."
        elif not is_json:
            result["error"] = "The server answered, but not with JSON. Check the base URL."
        else:
            ids = _model_ids(body)
            result.update(reachable=True, model_listed=settings.model in ids, models=ids[:_MODEL_LIMIT])
    except Exception as exc:
        result["error"] = _short_error(exc, url, settings.base_url)
    return result


def _timezone() -> str:
    return (os.getenv("GRANTS_TIMEZONE") or "").strip() or _DEFAULT_TIMEZONE


def admin_status() -> Dict[str, Any]:
    """Everything the admin page shows, read from the database."""
    _ensure_runs_table()
    profile = load_profile()
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(_RUNS_SQL)
        run_rows = [dict(row) for row in cur.fetchall()]
        cur.execute(_HEALTH_SQL)
        health_row = dict(cur.fetchone() or {})
        cur.execute(_RECIPIENTS_SQL)
        recipients = [_pick(row, _RECIPIENT_FIELDS) for row in cur.fetchall()]
        cur.execute(_EMAILS_SQL, {"tz": _timezone()})
        emails = [_pick(row, _EMAIL_FIELDS) for row in cur.fetchall()]

    runs = []
    for row in run_rows:
        run = _pick(row, _RUN_FIELDS)
        run["state"] = run_state(row.get("finished_at"), row.get("ok"), row.get("age_seconds"), bool(row.get("superseded")))
        run["started_at"], run["finished_at"] = _iso(run["started_at"]), _iso(run["finished_at"])
        run["has_log"] = bool(run["has_log"])
        runs.append(run)
    health = _pick(health_row, _HEALTH_FIELDS)
    health["data_as_of"] = _iso(health["data_as_of"])
    health["scoring_on"] = profile.is_configured
    for row in recipients:
        row["unsubscribed_at"], row["last_emailed"] = _iso(row["unsubscribed_at"]), _iso(row["last_emailed"])
    for row in emails:
        row["day"] = _iso(row["day"])

    active = sum(1 for row in recipients if row["active"])
    return {
        "runs": runs,
        "health": health,
        "recipients": recipients,
        "emails": emails,
        "warnings": warnings_for(health, runs, active, profile.is_configured),
        "email": email_status(health_row.get("last_sent")),
        "profile": profile_summary(profile),
        "settings": settings_summary(),
    }


def run_log(run_id: int) -> Optional[Dict[str, Any]]:
    """The saved log of one run, or None when there is no such run."""
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(_LOG_SQL, (run_id,))
        row = cur.fetchone()
    return {"id": row["id"], "step": row["step"], "log": row["log"]} if row else None
