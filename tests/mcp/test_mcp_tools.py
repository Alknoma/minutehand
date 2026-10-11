"""`minutehand mcp` is the command line: every command the parser has is a tool of the same name with its flags as
parameters, and a call is the command run as typed. Driven through the SDK's own client over an in-memory connection,
on real runs of the end-to-end test agent: a forgetful agent and a person who never answers fail `follows_up_when_due`,
and a rerun from the first checkpoint, where the person answers, does not."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

from minutehand import cli
from minutehand.adapters.mcp.server import build
from minutehand.domain.experiment import PersonChange
from minutehand.domain.scenario import Silent
from tests.e2e.support import agent_under_test, answers, scenario


def _commands(parser: argparse.ArgumentParser, words: tuple[str, ...] = ()) -> dict[str, set[str]]:
    """Every command the parser has, by its words joined with hyphens, with its arguments' long flags or names."""
    found: dict[str, set[str]] = {}
    nested = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for name, sub in nested.choices.items():
        if sub.get_default(cli.OVER_MCP) is False:
            continue
        here = (*words, name)
        found["-".join(here)] = {
            (max(a.option_strings, key=len).lstrip("-").replace("-", "_") if a.option_strings else a.dest)
            for a in sub._actions
            if not isinstance(a, argparse._HelpAction | argparse._SubParsersAction)
        } | ({"command"} if sub.get_default(cli.TAKES_COMMAND) else set())
        if any(isinstance(a, argparse._SubParsersAction) for a in sub._actions):
            found |= _commands(sub, here)
    return found


async def call(client: ClientSession, tool: str, **arguments: object) -> dict[str, object]:
    result = await client.call_tool(tool, arguments)
    [text] = [c.text for c in result.content if isinstance(c, TextContent)]
    answered = json.loads(text)
    assert isinstance(answered, dict)
    return answered


async def test_every_command_is_a_tool_with_its_flags_and_none_that_serves_until_stopped(tmp_path: Path) -> None:
    async with create_connected_server_and_client_session(build(tmp_path)) as client:
        listed = (await client.list_tools()).tools

    assert {t.name: set(t.inputSchema["properties"]) for t in listed} == _commands(cli._parser())
    assert not {"mcp", "mcp-relay", "serve", "view"} & {t.name for t in listed}


async def test_a_call_is_the_command_as_typed(tmp_path: Path) -> None:
    async with create_connected_server_and_client_session(build(tmp_path / "state")) as client:
        answered = await call(client, "schema", kind="agent")
        missing = await call(client, "findings", run_id="nope")

    typed = subprocess.run([sys.executable, "-m", "minutehand.cli", "schema", "agent"], capture_output=True, text=True)
    assert (answered["exit_code"], answered["stdout"]) == (typed.returncode, typed.stdout)
    assert missing["exit_code"] != 0 and "nope" in str(missing["stderr"])


@pytest.mark.timeout(300)
async def test_run_findings_and_a_fork_from_a_checkpoint_over_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    (tmp_path / "scenario.yaml").write_text(yaml.safe_dump(scenario(Silent()).model_dump(mode="json")))
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(launched.agent.model_dump(mode="json")))
    change = PersonChange(person="sofia", reply=answers(after=timedelta(hours=36)))
    (tmp_path / "fork.yaml").write_text(yaml.safe_dump({"overrides": [change.model_dump(mode="json")]}))

    async with create_connected_server_and_client_session(build(tmp_path / "state")) as client:
        ran = await call(
            client, "run", scenario=str(tmp_path / "scenario.yaml"), agent=str(tmp_path / "agent.yaml"), json=True,
            command=launched.command,
        )  # fmt: skip
        [parent] = json.loads(str(ran["stdout"]))["outcomes"]
        failed = {f["check"] for f in parent["result"]["findings"]}
        checkpoints = await call(client, "checkpoints", run_id=parent["record"]["run_id"])
        after_first_wake = next(line for line in str(checkpoints["stdout"]).splitlines() if "after wake 1" in line)
        forked = await call(
            client, "fork", run_id=parent["record"]["run_id"], at=int(after_first_wake.split()[1].rstrip(",")),
            changes=str(tmp_path / "fork.yaml"), json=True, command=launched.command,
        )  # fmt: skip
        [child] = json.loads(str(forked["stdout"]))["outcomes"]
        runs = await call(client, "runs")

    assert "follows_up_when_due" in failed
    assert "follows_up_when_due" not in {f["check"] for f in child["result"]["findings"]}
    assert child["record"]["parent_run"] == parent["record"]["run_id"]
    assert parent["record"]["run_id"] in str(runs["stdout"]) and child["record"]["run_id"] in str(runs["stdout"])
