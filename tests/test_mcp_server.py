"""The MCP server registers its tools and answers without a database where it can."""
from __future__ import annotations

import asyncio

from grants import profile as profile_module
from mcp_server import server as mcp_server


def test_tools_are_registered():
    tools = asyncio.run(mcp_server.server.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        "search_grants", "upcoming_deadlines", "grant_details",
        "track_grant", "untrack_grant", "tracked_grants", "research_profile",
    }
    search = next(tool for tool in tools if tool.name == "search_grants")
    schema = getattr(search, "input_schema", None) or getattr(search, "inputSchema")
    assert "closing_within_days" in schema["properties"]
    assert "soonest deadline first" in (search.description or "")


def test_research_profile_survives_missing_database(monkeypatch):
    # The committed template, not a team's research_profile.local.yml, which load_profile() prefers.
    monkeypatch.setenv("GRANTS_PROFILE_FILE", str(profile_module._DEFAULT_PATH))
    monkeypatch.setenv("POSTGRES_URL", "postgresql://nobody:nobody@127.0.0.1:1/nodb")
    monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT", "1")
    out = mcp_server.research_profile()
    assert out["configured"] is False
    assert out["deadline_horizons"] == [7, 30, 60, 90]
    assert "error" in out["data"]
