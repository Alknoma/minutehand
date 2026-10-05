"""The worked example, as a user runs it: stripe-mock, a ready-made open-source emulator downloaded as it is, in a
container Minutehand starts, with the payments API's real host forwarded to it. Killed mid-run, the run says the
environment failed and exits 2. Needs Docker and the image: run with -m docker."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from minutehand import session
from minutehand.domain.world import CallOutcome

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "external_emulator"
MINUTEHAND = Path(sys.executable).parent / "minutehand"
CONTAINER = "minutehand-ext-stripe"

pytestmark = pytest.mark.docker


def _answered(state: Path) -> int:
    """How many calls the emulator has logged an answer to, across the run's emulator logs."""
    return sum(p.read_text(errors="replace").count("Response: elapsed") for p in state.glob("runs/*/emulator-*.log"))


async def test_the_downloaded_emulator_answers_the_agent_and_its_death_fails_the_environment(tmp_path: Path) -> None:
    state = tmp_path / "state"
    command = [str(MINUTEHAND), "run", "scenario.yaml", "--agent", "agent.yaml", "--state", str(state)]
    env = {**os.environ, "EXAMPLE_PAUSE": "3"}
    run = await asyncio.create_subprocess_exec(
        *command, cwd=EXAMPLE, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        # Its readiness check and the first wake's two calls are answered; then it is killed, as a person would.
        while _answered(state) < 3:
            assert run.returncode is None, "the run ended before the emulator answered the first wake"
            await asyncio.sleep(0.1)
        subprocess.run(["docker", "kill", CONTAINER], check=True, capture_output=True)
        out, err = await asyncio.wait_for(run.communicate(), 50)
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    said = out.decode()
    assert run.returncode == 2, err.decode()
    assert "Environment failed: the run stopped because an external emulator it used was unavailable" in said
    assert "the first call it failed: POST api.stripe.com/v1/customers at wake 2, answered 502" in said
    assert "Listening for HTTP at address" in said  # the end of its log
    [run_id] = [d.name for d in (state / "runs").iterdir()]
    with session.reading(state, run_id) as kept:
        outcomes = [c.exchange.outcome for c in kept.calls() if c.exchange.host == "api.stripe.com"]
    assert outcomes[:2] == [CallOutcome.ANSWERED, CallOutcome.ANSWERED]
    assert CallOutcome.UNAVAILABLE in outcomes[2:]
