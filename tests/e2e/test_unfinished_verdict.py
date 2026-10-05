"""A run cut off at its wake limit while the agent's question is still open: no check fails, and no surface may
call it a pass. The command, the viewer and the MCP tools all state the same verdict, and the command exits 3."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from minutehand.adapters.mcp.results import FindingList
from minutehand.adapters.web.app import create_app
from minutehand.adapters.web.responses import FindingsResponse, RunsResponse
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.domain.scenario import PersonAsked
from tests.e2e.support import agent_under_test, answers, scenario
from tests.mcp.test_mcp_tools import call, connected

MINUTEHAND = Path(sys.executable).parent / "minutehand"
CUT_OFF = (
    "Not finished: no check failed, but the agent never reported it was done; the run stopped at the scenario's "
    "wake limit, with 1 wait still open."
)


async def test_a_run_cut_off_at_its_wake_limit_with_a_wait_open_is_not_finished_everywhere_and_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    played = scenario(answers(after=timedelta(days=5))).model_copy(
        update={"max_wakes": 1, "expect": [PersonAsked(person="sofia")]}
    )
    scenario_file = tmp_path / "scenario.yaml"
    scenario_file.write_text(yaml.safe_dump(played.model_dump(mode="json")))
    agent_file = tmp_path / "agent.yaml"
    agent_file.write_text(yaml.safe_dump(launched.agent.model_dump(mode="json")))
    state = tmp_path / "state"

    ran = subprocess.run(
        [
            str(MINUTEHAND),
            "run",
            str(scenario_file),
            "--agent",
            str(agent_file),
            "--state",
            str(state),
            "--",
            *launched.command,
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=120,
    )

    assert ran.returncode == 3, ran.stdout + ran.stderr
    head = ran.stdout.splitlines()
    assert head[1] == f"  {CUT_OFF}"
    assert "\nfail (" not in ran.stdout and "\ninformational (1)\n  expectations: sofia asked: met by" in ran.stdout
    found = re.match(r"run ([0-9a-f]+):", head[0])
    assert found is not None
    run_id = found.group(1)

    transport = httpx.ASGITransport(app=create_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as viewer:
        findings = FindingsResponse.model_validate_json((await viewer.get(f"/api/runs/{run_id}/findings")).content)
        listed = RunsResponse.model_validate_json((await viewer.get("/api/runs")).content)
    assert findings.verdict is not None and findings.verdict.words == CUT_OFF
    assert findings.verdict.stop is StopReason.WAKE_LIMIT
    assert [r.verdict for r in listed.runs] == [VerdictKind.UNFINISHED]

    async with connected(state) as client:
        listing = await call(client, "list_findings", FindingList, run_id=run_id)
    assert listing.verdict.kind is VerdictKind.UNFINISHED and listing.verdict.words == CUT_OFF

    again = subprocess.run(
        [str(MINUTEHAND), "findings", run_id, "--state", str(state)],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=60,
    )
    assert again.returncode == 3 and f"  {CUT_OFF}" in again.stdout
