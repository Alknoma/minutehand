"""The committed schemas are the generated ones: a model changed without `minutehand schema` being run again fails
here, so an editor reading `schemas/` and a team generating types from them read what the code validates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minutehand import agent_api
from minutehand.application.files import FileKind, schema

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


@pytest.mark.parametrize("kind", list(FileKind))
def test_the_committed_file_schema_is_the_generated_one(kind: FileKind) -> None:
    committed = json.loads((SCHEMAS / f"{kind.value}.schema.json").read_text(encoding="utf-8"))
    assert committed == schema(kind), f"run `minutehand schema {kind.value} > schemas/{kind.value}.schema.json`"


def test_the_committed_agent_api_is_the_generated_one() -> None:
    committed = json.loads((SCHEMAS / "agent-api.openapi.json").read_text(encoding="utf-8"))
    assert committed == agent_api.document(), "run `minutehand schema agent-api > schemas/agent-api.openapi.json`"


def test_the_agent_api_holds_no_endpoint_that_exists_for_minutehand() -> None:
    """Nothing is asked of the agent: the document covers only places the agent's own product already has."""
    operations = {op["operationId"] for path in agent_api.document()["paths"].values() for op in path.values()}  # type: ignore[union-attr]
    assert operations == {"deliverReply", "listPending", "decide"}


def test_the_agent_file_schema_takes_a_sandbox_and_no_wake_kinds() -> None:
    agent = schema(FileKind.AGENT)
    assert "sandbox" in agent["required"]  # type: ignore[operator]
    assert "wakes" not in agent["properties"]  # type: ignore[operator]
    assert not {"Reported", "Marked", "Polled", "Command", "Contained", "GoalByWake"} & set(agent["$defs"])  # type: ignore[arg-type]
