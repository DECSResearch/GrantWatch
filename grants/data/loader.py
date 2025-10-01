import json
from sql_utils import get_connection

def derive_stage(title, description):
    """Basic heuristic: mark concept vs full proposal."""
    text = (title + " " + description).lower()
    if any(word in text for word in ["concept", "pre-proposal", "preproposal", "letter of intent", "loi"]):
        return "concept"
    return "full"

def load_grants_from_json(path):
    with open(path, "r") as f:
        data = json.load(f)

    with get_connection() as conn, conn.cursor() as cur:
        for grant in data:
            opp_id = grant.get("opportunityNumber")
            title = grant.get("title", "")
            desc = grant.get("description", "")
            stage = derive_stage(title, desc)
            status = grant.get("opportunityStatus", "Posted")
            post_date = grant.get("postDate")
            close_date = grant.get("closeDate")
            archive_date = grant.get("archiveDate")

            cur.execute("""
                INSERT INTO grants (opp_id, title, stage, opportunity_status,
                                    post_date, close_date, archive_date, description)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (opp_id) DO NOTHING;
            """, (opp_id, title, stage, status, post_date, close_date, archive_date, desc))
