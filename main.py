"""Entry point for the GrantWatch data pipeline."""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from grants_data.pipeline import onlyTheGoodStuff
from notifications.gmail_notifier import notify_grant_release
from sql_utils import fetch_upcoming


def _print_upcoming(stage: str, days: int) -> None:
    try:
        rows = fetch_upcoming(stage=stage, days=days)
    except Exception as exc:
        print(f"Unable to fetch upcoming {stage} proposals: {exc}")
        return

    if not rows:
        print(f"No upcoming {stage} proposals in the next {days} days.")
        return

    for row in rows:
        print(f"{row['title']} | Due: {row['close_date']}")


def main() -> None:
    success, filtered_grants = onlyTheGoodStuff()
    if success:
        csv_path = getattr(onlyTheGoodStuff, "last_csv_path", None)
        message = f"Pipeline complete. Filtered {len(filtered_grants)} grants."
        if csv_path:
            message += f" CSV saved to: {csv_path}"
        print(message)
        try:
            notify_grant_release(filtered_grants, str(csv_path) if csv_path else None)
        except Exception as exc:
            print(f"Failed to dispatch email notification: {exc}")
    else:
        print("Pipeline failed; check logs for details.")
        return

    print("Concept proposals due soon:")
    _print_upcoming(stage="concept", days=30)

    print("\nFull proposals due soon:")
    _print_upcoming(stage="full", days=60)


if __name__ == "__main__":
    main()
