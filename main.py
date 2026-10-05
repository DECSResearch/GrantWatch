"""Entry point for the GrantWatch data pipeline."""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Set

from dotenv import load_dotenv

load_dotenv()

from grants.profile import Profile, load_profile
from grants.sql_utils import ensure_schema, fetch_upcoming
from grants_data.pipeline import onlyTheGoodStuff
from notifications.digest import DigestItem, build_digest, merge_items
from notifications.gmail_notifier import digest_recipients, send_digest


def _upcoming_from_db(profile: Profile) -> List[DigestItem]:
    """Deadlines already in the database, including grants older than this run's window."""
    try:
        rows = fetch_upcoming(
            days=max(profile.deadline_horizons),
            min_score=profile.min_score_to_notify,
            include_forecasted=profile.include_forecasted,
        )
    except Exception as exc:
        print(f"Unable to read upcoming deadlines from the database: {exc}")
        return []
    return [DigestItem.from_row(row) for row in rows]


def _items_from_run(records: List[Dict[str, object]], new_ids: Optional[Set[str]], watched: Set[str]) -> List[DigestItem]:
    items = []
    for record in records:
        opp_id = str(record.get("OPPORTUNITY_NUMBER") or "")
        items.append(
            DigestItem.from_record(
                record,
                is_new=bool(new_ids) and opp_id in new_ids,
                watched=opp_id in watched,
            )
        )
    return items


def main() -> int:
    """Run the pipeline; return a process exit code so schedulers see failures."""
    try:
        ensure_schema()
    except Exception as exc:
        print(f"Warning: could not verify database schema: {exc}")

    success, grants = onlyTheGoodStuff()
    if not success:
        print("Pipeline failed; check logs for details.")
        return 1

    profile: Profile = getattr(onlyTheGoodStuff, "last_profile", None) or load_profile()
    new_ids: Optional[Set[str]] = getattr(onlyTheGoodStuff, "last_new_ids", None)
    csv_path = getattr(onlyTheGoodStuff, "last_csv_path", None)

    message = f"Pipeline complete. {len(grants)} grants kept."
    if csv_path:
        message += f" CSV saved to: {csv_path}"
    print(message)
    if not profile.is_configured:
        print("Relevance scoring is off until config/research_profile.yml has a summary.")

    from_db = _upcoming_from_db(profile)
    watched = {item.opp_id for item in from_db if item.watched}
    items = merge_items(from_db, _items_from_run(grants, new_ids, watched))

    digest = build_digest(
        items,
        today=date.today(),
        horizons=profile.deadline_horizons,
        min_score=profile.min_score_to_notify,
        profile_line=profile.institution,
    )
    if digest is None:
        print("Nothing new and no relevant deadlines in the next "
              f"{max(profile.deadline_horizons)} days.")
        return 0

    print()
    print(digest.text)
    if digest_recipients():
        sent = send_digest(digest.subject, digest.text, digest.html)
        print("Digest emailed." if sent else "Digest email failed; see logs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
