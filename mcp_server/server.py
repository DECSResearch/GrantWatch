"""GrantWatch MCP server: lets Claude Code or Claude Desktop ask about grants.

Run it with the project's virtualenv so it can import the shared query
code and reach the same Postgres database the pipeline fills:

    .venv/bin/python mcp_server/server.py

Register it once with Claude Code from the repository root:

    claude mcp add grantwatch -e POSTGRES_URL=postgresql://... -- \\
        "$PWD/.venv/bin/python" "$PWD/mcp_server/server.py"

Every tool reads the database; nothing here calls a language model, the
client on the other end does the reasoning.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from mcp.server.mcpserver import MCPServer

from grants.profile import load_profile
from grants.queries import GrantQuery, data_status, get_grant, query_grants
from grants.sql_utils import add_to_watchlist, fetch_upcoming, list_watchlist, remove_from_watchlist

server = MCPServer(
    "grantwatch",
    instructions=(
        "GrantWatch tracks Grants.gov opportunities scored against a research profile. "
        "Scores run 0 to 5 (5 = squarely in scope). Dates are ISO strings; days_left is "
        "relative to today; forecasted=true means the deadline is an estimate. Start with "
        "upcoming_deadlines for 'what is due soon', search_grants for topics, and "
        "grant_details before recommending anything. track_grant marks a grant the group "
        "intends to pursue so its deadline is always surfaced."
    ),
)

_LIST_FIELDS = (
    "opp_id", "title", "agency", "close_date", "days_left", "forecasted",
    "relevance_score", "relevance_reason", "summary", "url", "watched", "award_ceiling",
)


def _compact(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in _LIST_FIELDS}


@server.tool()
def search_grants(
    query: str = "",
    closing_within_days: Optional[int] = None,
    min_score: int = 0,
    include_forecasted: bool = True,
    tracked_only: bool = False,
    limit: int = 25,
) -> List[Dict[str, Any]]:
    """Search open grants, soonest deadline first.

    query matches title, agency, summary, or opportunity number (case-insensitive
    substring). closing_within_days limits to deadlines inside that window.
    min_score keeps grants scored at least that high (tracked grants always pass).
    """
    rows = query_grants(
        GrantQuery(
            q=query,
            min_score=min_score or None,
            closing_within=closing_within_days,
            include_forecasted=include_forecasted,
            watched_only=tracked_only,
            limit=max(1, min(int(limit), 200)),
        )
    )
    return [_compact(row) for row in rows]


@server.tool()
def upcoming_deadlines(days: int = 30, min_score: Optional[int] = None) -> List[Dict[str, Any]]:
    """Relevant or tracked grants closing within `days` days, soonest first.

    min_score defaults to the profile's notification threshold.
    """
    profile = load_profile()
    threshold = profile.min_score_to_notify if min_score is None else int(min_score)
    rows = fetch_upcoming(days=max(1, int(days)), min_score=threshold, include_forecasted=profile.include_forecasted)
    out = []
    for row in rows:
        close = row.get("close_date")
        out.append(
            {
                "opp_id": row.get("opp_id"),
                "title": row.get("title"),
                "agency": row.get("agency"),
                "close_date": close.isoformat() if close else None,
                "forecasted": str(row.get("opportunity_status") or "").lower() == "forecasted",
                "relevance_score": row.get("relevance_score"),
                "relevance_reason": row.get("relevance_reason"),
                "summary": row.get("summary"),
                "url": row.get("url"),
                "watched": bool(row.get("watched")),
                "watch_note": row.get("watch_note"),
            }
        )
    return out


@server.tool()
def grant_details(opportunity_number: str) -> Dict[str, Any]:
    """Everything stored for one grant, including the full description."""
    grant = get_grant(opportunity_number)
    if grant is None:
        return {"error": f"No grant with opportunity number {opportunity_number!r} in the database."}
    return grant


@server.tool()
def track_grant(opportunity_number: str, note: str = "") -> Dict[str, Any]:
    """Add a grant to the watchlist so its deadline is always surfaced; note is optional."""
    add_to_watchlist(opportunity_number, note or None)
    return {"opp_id": opportunity_number, "watched": True, "note": note or None}


@server.tool()
def untrack_grant(opportunity_number: str) -> Dict[str, Any]:
    """Remove a grant from the watchlist."""
    removed = remove_from_watchlist(opportunity_number)
    return {"opp_id": opportunity_number, "watched": False, "removed": removed}


@server.tool()
def tracked_grants() -> List[Dict[str, Any]]:
    """The watchlist with each grant's deadline and score."""
    out = []
    for row in list_watchlist():
        item = dict(row)
        for key in ("created_at", "close_date"):
            if item.get(key) is not None:
                item[key] = item[key].isoformat()
        out.append(item)
    return out


@server.tool()
def research_profile() -> Dict[str, Any]:
    """The research profile the scores are relative to, plus data freshness."""
    profile = load_profile()
    try:
        status: Optional[Dict[str, Any]] = data_status()
    except Exception as exc:  # the profile is still useful without the database
        status = {"error": f"database unavailable: {exc}"}
    return {
        "configured": profile.is_configured,
        "summary": profile.summary if profile.is_configured else "",
        "institution": profile.institution,
        "applicant_type": profile.applicant_type,
        "keywords": profile.keywords,
        "agencies_exclude": profile.agencies_exclude,
        "min_score_to_notify": profile.min_score_to_notify,
        "deadline_horizons": profile.deadline_horizons,
        "include_forecasted": profile.include_forecasted,
        "data": status,
    }


def main() -> None:
    server.run(transport=os.getenv("GRANTWATCH_MCP_TRANSPORT", "stdio"))  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
