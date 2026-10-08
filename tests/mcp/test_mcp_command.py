"""`minutehand mcp` itself: the installed command, spoken to over stdio by the SDK's own client."""

from __future__ import annotations

import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from minutehand.adapters.mcp.results import RunListing

MINUTEHAND = Path(sys.executable).parent / "minutehand"
TOOLS = {
    "list_scenarios",
    "run_scenario",
    "list_findings",
    "show_evidence",
    "list_outbound_calls",
    "rerun_from",
    "list_runs",
    "schema",
    "query_run",
    "trace",
    "explain",
}


async def test_the_command_serves_every_tool_over_stdio(tmp_path: Path) -> None:
    server = StdioServerParameters(command=str(MINUTEHAND), args=["mcp", "--state", str(tmp_path / "state")])
    async with stdio_client(server) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        tools = await client.list_tools()
        listed = await client.call_tool("list_runs", {})

    assert {t.name for t in tools.tools} == TOOLS
    assert all(t.description and t.outputSchema for t in tools.tools)
    assert RunListing.model_validate(listed.structuredContent).runs == []
