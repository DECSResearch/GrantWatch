"""Entry point for GrantWatch.

    python main.py                  ingest today's extract, then email the team
    python main.py ingest           only download, filter, score and load
    python main.py notify           only send email (reminders still go out when ingest fails)
    python main.py notify --dry-run print what would be sent; sends and records nothing
    python main.py recipients list | add EMAIL [--name N] [--min-score S] | remove EMAIL
    python main.py test-email EMAIL send one short message to check delivery
    python main.py admin-user NAME  make the line that gives NAME access to the admin page
"""
from __future__ import annotations

import argparse
import getpass
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

from grants.profile import load_profile
from grants.runs import recorded
from grants.sql_utils import ensure_schema
from grants_data.pipeline import onlyTheGoodStuff
from notifications.notify import run_notify


def ingest_detail(ok: bool, stats: Dict[str, Any]) -> str:
    """One sentence about an ingest run for the run log, from ``onlyTheGoodStuff.last_stats``.

    Runs inside the run log's ``with`` block, so it must cope with any stats.
    """
    text = _ingest_sentence(ok, stats)
    abort = stats.get("scoring_abort")
    return f"{text.rstrip('.')}. Scoring stopped: {str(abort).rstrip('.')}." if abort else text


def _ingest_sentence(ok: bool, stats: Dict[str, Any]) -> str:
    reason = stats.get("reason")
    if not ok:
        return str(reason or "The pipeline failed; check the logs.")
    n = {key: value for key, value in stats.items() if isinstance(value, int)}
    parts = []
    if "extract" in n:
        parts.append(f"{n['extract']:,} in the extract")
    if "kept" in n:
        parts.append(f"{n['kept']:,} kept")
    scored, failed, deferred = (n.get(key, 0) for key in ("scored", "failed", "deferred"))
    if scored or failed or deferred:
        trouble = [f"{count:,} {text}" for count, text in ((failed, "failed"), (deferred, "left for later")) if count]
        parts.append(f"{scored:,} newly scored" + (f" ({', '.join(trouble)})" if trouble else ""))
    if "inserted" in n:
        parts.append(f"{n['inserted']:,} new and {n.get('updated', 0):,} updated in the database")
    sentence = ", ".join(parts) or "The pipeline finished"
    return f"{sentence}. {reason}" if reason else sentence


def ingest() -> int:
    try:
        ensure_schema()
    except Exception as exc:
        print(f"Warning: could not verify database schema: {exc}")

    with recorded("ingest") as run:
        code = _ingest()
        run.ok = code == 0
        run.stats = dict(onlyTheGoodStuff.last_stats)
        run.detail = ingest_detail(run.ok, run.stats)
        return code


def _ingest() -> int:
    success, grants = onlyTheGoodStuff()
    if not success:
        print("Pipeline failed; check logs for details.")
        return 1

    profile = getattr(onlyTheGoodStuff, "last_profile", None) or load_profile()
    csv_path = getattr(onlyTheGoodStuff, "last_csv_path", None)
    message = f"Pipeline complete. {len(grants)} grants kept."
    if csv_path:
        message += f" CSV saved to: {csv_path}"
    print(message)
    if not profile.is_configured:
        print("Relevance scoring is off until config/research_profile.yml has a summary.")
    return 0


def notify_step(dry_run: bool = False) -> int:
    """Run the notify step; a real run (not a dry run) goes in the run log."""
    if dry_run:
        return run_notify(dry_run=True)
    with recorded("notify") as run:
        code = run_notify()
        run.ok = code == 0
        run.stats = getattr(run_notify, "last_counts", None)
        run.detail = run_notify.last_summary
        return code


def recipients(args: argparse.Namespace) -> int:
    from notifications import store

    ensure_schema()
    if args.action == "add":
        added = store.add_recipient(args.email, name=args.name, min_score=args.min_score)
        print(f"{added.email} will get GrantWatch email.")
    elif args.action == "remove":
        print("Removed." if store.remove_recipient(args.email) else f"{args.email} was not a recipient.")
    else:
        rows = store.list_recipients(active_only=False)
        if not rows:
            print("No recipients yet. Add one with: python main.py recipients add you@example.edu")
        for row in rows:
            state = "active" if row["active"] else f"unsubscribed {row['unsubscribed_at']:%Y-%m-%d}"
            threshold = f", min score {row['min_score']}" if row["min_score"] is not None else ""
            print(f"{row['email']}  ({state}{threshold})")
    return 0


def test_email(address: str) -> int:
    """Send one short message and say exactly what happened."""
    from notifications.transport import build_message, get_transport, sender_address

    try:
        transport = get_transport()
    except ValueError as exc:
        print(f"Email is misconfigured: {exc}")
        return 1
    if transport is None:
        print("No email transport configured. Set GRANTS_SMTP_HOST (or GMAIL_TOKEN_FILE) in .env; see .env.example.")
        return 1
    sender = sender_address()
    text = "This is a GrantWatch test email. If you can read it, GrantWatch notifications reach this address.\n"
    message = build_message(sender, address, "GrantWatch test email", text, f"<p>{text.strip()}</p>")
    try:
        transport.send(message)
    except Exception as exc:
        print(f"Sending through {transport.name} failed: {exc}")
        return 1
    print(f"Handed to {transport.name} from {sender or 'the account default'} to {address}.")
    print("If it has not arrived in a few minutes, check Junk and the Other tab, then the Microsoft 365")
    print("quarantine at https://security.microsoft.com/quarantine (university mail often lands there).")
    return 0


def admin_user(name: str) -> int:
    """Ask for a password and print the GRANTS_ADMIN_USERS line for NAME. Needs no database."""
    from src.web import admin_auth

    account = admin_auth.canonical_name(name)
    if account is None:
        print("A name is 1 to 32 lowercase letters, digits, dots, dashes or underscores.")
        return 1
    password = getpass.getpass(f"Password for {account}: ")
    if getpass.getpass("Password again: ") != password:
        print("The two passwords differ.")
        return 1
    if len(password) < admin_auth.MIN_PASSWORD_LENGTH or not password.isascii():
        print(f"The password must be at least {admin_auth.MIN_PASSWORD_LENGTH} characters, all ASCII: browsers send it as ASCII.")
        return 1
    print(f"{account}:{admin_auth.hash_password(password)}")
    print("Add this line to admin_users in ansible/secrets.yml and redeploy.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GrantWatch: Grants.gov opportunities and deadlines for the team.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("ingest", help="download, filter, score and load today's extract")
    notify = sub.add_parser("notify", help="email each recipient what is new since their last email")
    notify.add_argument("--dry-run", action="store_true", help="print the emails instead of sending or recording them")
    people = sub.add_parser("recipients", help="manage who gets the email")
    people.add_argument("action", choices=["list", "add", "remove"])
    people.add_argument("email", nargs="?")
    people.add_argument("--name")
    people.add_argument("--min-score", type=int, choices=range(0, 6), help="this person's threshold instead of the profile's")
    test = sub.add_parser("test-email", help="send one short message to check that email gets through")
    test.add_argument("address")
    admin = sub.add_parser("admin-user", help="make the line that lets one person open the admin page")
    admin.add_argument("name")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Run the requested step; return a process exit code so schedulers see failures."""
    args = _parser().parse_args(argv)
    if args.command == "ingest":
        return ingest()
    if args.command == "notify":
        return notify_step(dry_run=args.dry_run)
    if args.command == "test-email":
        return test_email(args.address)
    if args.command == "admin-user":
        return admin_user(args.name)
    if args.command == "recipients":
        if args.action != "list" and not args.email:
            _parser().error(f"recipients {args.action} needs an email address")
        return recipients(args)
    # Both steps, and notify even when ingest failed: tracked deadlines do not
    # depend on today's download.
    ingested = ingest()
    notified = notify_step()
    return ingested or notified


if __name__ == "__main__":
    raise SystemExit(main())
