"""The notify step: email each recipient what they have not been told yet.

Runs after the pipeline, and also on its own (``python main.py notify``), so
deadline reminders go out even on a day the Grants.gov download fails. A
notice is recorded only after its email was sent; a failed send is retried on
the next run, and a rerun on the same day sends nothing new.
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional, Protocol, Sequence

from grants.profile import Profile, load_profile
from logs.status_logger import logger
from notifications.digest import RenderedEmail, render_email
from notifications.planner import GrantState, NotifySettings, Recipient, SentRecord, plan_email
from notifications.transport import Transport, build_message, get_transport, sender_address


class Store(Protocol):
    def ensure_schema(self) -> None: ...
    def sync_recipients(self, emails: Sequence[str]) -> int: ...
    def recipients(self) -> List[Recipient]: ...
    def grants(self) -> List[GrantState]: ...
    def sent(self, recipient_ids: Sequence[int]) -> List[SentRecord]: ...
    def record(self, rows: Sequence[SentRecord]) -> int: ...
    def acquire_lock(self, minutes: int = 30) -> bool: ...
    def release_lock(self) -> None: ...


def env_recipients() -> List[str]:
    raw = os.getenv("GRANTS_NOTIFY_RECIPIENTS") or os.getenv("GMAIL_NOTIFY_RECIPIENTS") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int((os.getenv(name) or "").strip() or default))
    except ValueError:
        return default


def settings_from_profile(profile: Profile) -> NotifySettings:
    return NotifySettings(
        min_score=profile.min_score_to_notify,
        reminder_days=list(profile.reminder_days),
        horizons=list(profile.deadline_horizons),
        overview_day=profile.overview_day,
        include_forecasted=profile.include_forecasted,
        max_new=_env_int("GRANTS_EMAIL_MAX_NEW", 25),
        dashboard_url=(os.getenv("GRANTS_DASHBOARD_URL") or "").strip().rstrip("/"),
        profile_line=profile.institution,
    )


def plan_all(store: Store, recipients: Sequence[Recipient], today: date, settings: NotifySettings) -> Dict[int, Optional[RenderedEmail]]:
    grants = store.grants()
    by_recipient: Dict[int, List[SentRecord]] = defaultdict(list)
    for row in store.sent([r.id for r in recipients]):
        by_recipient[row.recipient_id].append(row)
    return {
        r.id: render_email(plan_email(r, grants, by_recipient[r.id], today, settings), settings, today)
        for r in recipients
    }


def _report(level: str, summary: str) -> None:
    """Log the outcome and keep it for the run log (``run_notify.last_summary``)."""
    logger(level, f"Notify: {summary}")
    run_notify.last_summary = summary  # type: ignore[attr-defined]


def run_notify(
    *,
    store: Optional[Store] = None,
    transport: Optional[Transport] = None,
    today: Optional[date] = None,
    settings: Optional[NotifySettings] = None,
    dry_run: bool = False,
) -> int:
    """Send today's emails; returns a process exit code (non-zero when anything failed)."""
    run_notify.last_summary = None  # type: ignore[attr-defined]
    run_notify.last_counts = None  # type: ignore[attr-defined]
    if store is None:
        from notifications.store import PostgresStore

        store = PostgresStore()
    today = today or date.today()
    settings = settings or settings_from_profile(load_profile())

    try:
        store.ensure_schema()
        store.sync_recipients(env_recipients())
        recipients = store.recipients()
    except Exception as exc:
        _report("error", f"cannot read recipients from the database: {exc}")
        return 1
    if not recipients:
        _report("info", "no recipients yet; add one with `python main.py recipients add EMAIL`")
        return 0

    if transport is None and not dry_run:
        try:
            transport = get_transport()
        except ValueError as exc:
            _report("error", str(exc))
            return 1
        if transport is None:
            _report("error", "no email transport configured (set GRANTS_SMTP_HOST, GMAIL_TOKEN_FILE or GRANTS_EMAIL_OUTBOX)")
            return 1

    if not dry_run:
        try:
            if not store.acquire_lock():
                _report("warning", "another run is sending right now; skipping this one")
                return 0
        except Exception as exc:
            _report("error", f"cannot take the send lock: {exc}")
            return 1

    failures = sent = quiet = 0
    try:
        emails = plan_all(store, recipients, today, settings)
        sender = sender_address()
        for recipient in recipients:
            email = emails.get(recipient.id)
            if email is None:
                quiet += 1
                continue
            if dry_run:
                print(f"--- To: {recipient.email}\nSubject: {email.subject}\n\n{email.text}")
                continue
            message = build_message(sender, recipient.email, email.subject, email.text, email.html, email.unsubscribe_url)
            try:
                transport.send(message)  # type: ignore[union-attr]
            except Exception as exc:
                failures += 1
                logger("error", f"Notify: sending to {recipient.email} failed; it will be retried next run: {exc}")
                continue
            sent += 1
            try:
                store.record(email.records(recipient.id, today))
            except Exception as exc:
                failures += 1
                logger("error", f"Notify: emailed {recipient.email} but could not record it, so the next run may repeat it: {exc}")
    except Exception as exc:
        _report("error", f"failed while preparing emails: {exc}")
        return 1
    finally:
        if not dry_run:
            try:
                store.release_lock()
            except Exception as exc:
                logger("warning", f"Notify: could not release the send lock (it expires on its own): {exc}")

    _report("info", f"sent {sent}, failed {failures}, nothing new for {quiet} of {len(recipients)} recipients")
    run_notify.last_counts = {"sent": sent, "failed": failures, "quiet": quiet, "recipients": len(recipients)}  # type: ignore[attr-defined]
    return 1 if failures else 0


run_notify.last_summary = None  # type: ignore[attr-defined]
run_notify.last_counts = None  # type: ignore[attr-defined]
