"""`minutehand mcp` itself: the installed command, spoken to over stdio by the SDK's own client."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from minutehand import cli
from minutehand.adapters.mcp.server import tools

MINUTEHAND = Path(sys.executable).parent / "minutehand"


async def test_the_command_serves_every_command_over_stdio(tmp_path: Path) -> None:
    server = StdioServerParameters(command=str(MINUTEHAND), args=["mcp", "--state", str(tmp_path / "state")])
    async with stdio_client(server) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        listed = await client.list_tools()
        runs = await client.call_tool("runs", {})

    assert {t.name for t in listed.tools} == {t.name for t in tools(cli._parser())}
    assert all(t.description for t in listed.tools)
    [text] = [c.text for c in runs.content if isinstance(c, TextContent)]
    assert json.loads(text)["exit_code"] == 0
