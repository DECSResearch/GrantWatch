from sql_utils import fetch_upcoming
from grants.data.loader import load_grants_from_json

def main():
    # Example: load some grants JSON dump
    # load_grants_from_json("sample_grants.json")

    # Fetch concept proposals due in 30 days
    concept = fetch_upcoming(stage="concept", days=30)
    print("Concept proposals due soon:")
    for row in concept:
        print(f"{row['title']} | Due: {row['close_date']}")

    # Fetch all full proposals due in 60 days
    full = fetch_upcoming(stage="full", days=60)
    print("\nFull proposals due soon:")
    for row in full:
        print(f"{row['title']} | Due: {row['close_date']}")

if __name__ == "__main__":
    main()
