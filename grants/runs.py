"""Run log for the scheduled steps, read by the admin page.

Recording is best effort: a database error is logged and swallowed, never
allowed to change whether the job succeeded.
"""
from __future__ import annotations

import json
import logging
import os
import re
import traceback
from collections import deque
from contextlib import contextmanager
from typing import Any, Deque, Dict, Iterator, List, Optional, Sequence

from grants.sql_utils import db_connection
from llm_utils.relevance import safe_host
from logs.status_logger import FORMATTER, logger

_DETAIL_LIMIT = 1000
_LOG_LINES = 500
_LINE_LIMIT = 500
_TRACEBACK_LIMIT = 5000
_LOG_LIMIT = 100_000

_SECRET_ENV = (
    "GRANTS_LLM_API_KEY",
    "OPENAI_API_KEY",
    "POSTGRES_PASSWORD",
    "POSTGRES_URL",
    "GRANTS_SECRET_KEY",
    "GRANTS_ADMIN_PASSWORD",
    "GRANTS_SMTP_PASSWORD",
    "GMAIL_TOKEN_JSON",
)
_URL_ENV = ("GRANTS_LLM_BASE_URL", "OPENAI_BASE_URL")
_SCHEME = r"\b[A-Za-z][A-Za-z0-9+.-]{1,15}://"
_USERINFO = re.compile(rf"({_SCHEME})[^/\s@]*@")
# The query of a URL, and of the "url: /path?query" that urllib3 prints in its errors.
_QUERY = re.compile(rf"({_SCHEME}[^\s?]+|\burl: /[^\s?]*)\?\S*")

_FINISH_SQL = "UPDATE job_runs SET finished_at = NOW(), ok = %s, detail = %s, log = %s, stats = %s::jsonb WHERE id = %s;"
_FINISH_PLAIN_SQL = "UPDATE job_runs SET finished_at = NOW(), ok = %s, detail = %s WHERE id = %s;"


def _env_values(names: Sequence[str]) -> List[str]:
    values = ((os.getenv(name) or "").strip() for name in names)
    return [value for value in values if len(value) >= 8]


def secret_values() -> List[str]:
    """Environment values that must never reach a log, the run log or the admin page."""
    return _env_values(_SECRET_ENV) + _env_values(_URL_ENV)


def redact(text: str, urls: Sequence[str] = (), known: Optional[Sequence[str]] = None) -> str:
    """``text`` without secrets: base URLs shrink to their host, other secrets become ``***``.

    ``urls`` are further URLs (a request URL) to shrink the same way; ``known`` is
    ``secret_values()`` when the caller already has it.
    """
    shrink = set(_env_values(_URL_ENV)) | {url for url in urls if url}
    for value in sorted(shrink | set(secret_values() if known is None else known), key=len, reverse=True):
        text = text.replace(value, safe_host(value) if value in shrink else "***")
    return _QUERY.sub(r"\1", _USERINFO.sub(r"\1", text))


class Run:
    """What the caller may set while the step runs."""

    def __init__(self) -> None:
        self.ok = True
        self.detail: Optional[str] = None
        self.stats: Optional[Dict[str, Any]] = None


class _Capture(logging.Handler):
    """Keeps the last lines the ``grantwatch`` logger writes while a step runs."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(FORMATTER)
        self.known = secret_values()
        self.lines: Deque[str] = deque(maxlen=_LOG_LINES)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = redact(self.format(record), known=self.known)
            self.lines.extend(line[:_LINE_LIMIT] for line in text.split("\n"))
        except Exception:
            pass  # capturing must never reach the job

    def text(self, error_traceback: Optional[str] = None) -> str:
        """The kept lines, then the traceback of the error that ended the step."""
        lines = list(self.lines)
        if error_traceback:
            lines.append(redact(error_traceback.rstrip("\n"), known=self.known)[-_TRACEBACK_LIMIT:])
        return "\n".join(lines)[-_LOG_LIMIT:]


def _clean(text: Optional[str]) -> Optional[str]:
    return redact(text).replace("\x00", "") if text else None


def _scrub(value: Any) -> Any:
    """``value`` with every string redacted and free of NUL, which Postgres refuses in JSON."""
    if isinstance(value, str):
        return _clean(value) or ""
    if isinstance(value, dict):
        return {_scrub(key): _scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_scrub(item) for item in value]
    return value


def _start(step: str) -> Optional[int]:
    try:
        with db_connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO job_runs (step) VALUES (%s) RETURNING id;", (step,))
            return cur.fetchone()[0]
    except Exception as exc:
        logger("warning", f"Could not record the start of the {step} step: {exc}")
        return None


def _update(step: str, sql: str, params: tuple) -> bool:
    try:
        with db_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
        return True
    except Exception as exc:
        logger("warning", f"Could not record the end of the {step} step: {exc}")
        return False


def _finish(
    run_id: int,
    step: str,
    ok: bool,
    detail: Optional[str],
    log: Optional[str] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> None:
    """Write the outcome; if the log or stats will not go in, write the outcome alone."""
    detail = (_clean(detail) or "")[:_DETAIL_LIMIT] or None
    try:
        saved = (
            (_clean(log) or "")[-_LOG_LIMIT:] or None,
            json.dumps(_scrub(stats), default=str) if stats is not None else None,
        )
    except Exception as exc:
        logger("warning", f"Could not prepare the {step} step's log for saving: {exc}")
        saved = None
    if saved is None or not _update(step, _FINISH_SQL, (ok, detail, *saved, run_id)):
        _update(step, _FINISH_PLAIN_SQL, (ok, detail, run_id))


@contextmanager
def recorded(step: str) -> Iterator[Run]:
    """Log one run of ``step``; the body sets ``run.ok``, ``run.detail`` and ``run.stats``.

    The ``grantwatch`` logger's lines are kept while the body runs and saved with the run.
    """
    run = Run()
    run_id = _start(step)
    capture = _Capture()
    channel = logging.getLogger("grantwatch")
    channel.addHandler(capture)
    error: Optional[BaseException] = None
    error_traceback: Optional[str] = None
    try:
        yield run
    except BaseException as exc:
        error, error_traceback = exc, traceback.format_exc()
        raise
    finally:
        channel.removeHandler(capture)
        if run_id is not None:
            try:
                detail = run.detail or (f"{type(error).__name__}: {error}" if error is not None else None)
                _finish(run_id, step, run.ok and error is None, detail, capture.text(error_traceback), run.stats)
            except Exception as exc:
                logger("warning", f"Could not record the end of the {step} step: {exc}")
