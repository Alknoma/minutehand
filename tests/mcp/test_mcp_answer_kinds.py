"""The MCP tools' boundary: a refusal keeps its words, the machine failing is named as that, and anything else is
Minutehand's own error, named as one and logged at error, never read by the client as its request being wrong. Driven
through the SDK's own client."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from minutehand import session
from minutehand.adapters.mcp.server import build

MCP = "minutehand.adapters.mcp.server"


async def _called(state: Path, run_id: str) -> str:
    async with create_connected_server_and_client_session(build(state)) as client:
        result = await client.call_tool("list_findings", {"run_id": run_id})
    assert result.isError
    [said] = result.content
    assert isinstance(said, TextContent)
    framed = "Error executing tool list_findings: "  # the SDK's own frame around the tool's error
    assert said.text.startswith(framed)
    return said.text.removeprefix(framed)


async def test_a_run_that_is_not_there_is_refused_in_its_own_words(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=MCP)
    said = await _called(tmp_path, "r1")
    assert said.startswith("no finished run r1")
    assert "internal error" not in said
    assert [r.levelno for r in caplog.records if r.name == MCP] == []


async def test_a_record_minutehand_cannot_read_is_its_internal_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    broken = session.run_dir(tmp_path, "r1")
    broken.mkdir(parents=True)
    (broken / session.RECORD).write_text("{not json", encoding="utf-8")
    caplog.set_level(logging.DEBUG, logger=MCP)
    said = await _called(tmp_path, "r1")
    assert said.startswith("minutehand internal error in list_findings: ValidationError: ")
    assert "a bug in minutehand, not in the request" in said
    assert [r.levelno for r in caplog.records if r.name == MCP] == [logging.ERROR]
