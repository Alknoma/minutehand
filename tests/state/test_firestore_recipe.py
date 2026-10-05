"""examples/state/firestore_emulator against a real emulator container: the agent's state is written through
the emulator's REST API, checkpointed by export, changed, and brought back by restarting the emulator with
`--import`; the verify step passes, and catches a restore of the wrong snapshot.

Needs Docker. The image is the recipe's own Dockerfile unless FIRESTORE_IMAGE names one that already holds
firebase-tools and Java. Run with `-m firestore`; not part of the default run.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.checkpoint import Restorable
from minutehand.application.files import load_agent, load_scenario
from minutehand.application.restore import RestoreFailed, RestoreStep
from minutehand.application.state_hooks import wake_dir
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.experiment import Fork
from tests.e2e.support import free_port

pytestmark = [pytest.mark.firestore, pytest.mark.timeout(1800)]

RECIPE = Path(__file__).parents[2] / "examples" / "state" / "firestore_emulator"
HOOKS = [sys.executable, str(RECIPE / "hooks.py")]
DOCUMENTS = "/v1/projects/demo-minutehand/databases/(default)/documents"


@dataclass(frozen=True)
class Emulator:
    rest: str
    agent_port: int
    env: dict[str, str]

    def hook(self, command: str) -> None:
        subprocess.run([*HOOKS, command], env=self.env, check=True, timeout=900)

    def get(self, path: str) -> dict[str, object] | None:
        request = urllib.request.Request(f"{self.rest}{DOCUMENTS}/{path}", headers={"authorization": "Bearer owner"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def send(self, method: str, path: str, fields: dict[str, object] | None = None) -> None:
        body = json.dumps({"fields": fields}).encode() if fields is not None else None
        request = urllib.request.Request(
            f"{self.rest}{DOCUMENTS}/{path}",
            data=body,
            method=method,
            headers={"authorization": "Bearer owner", "content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10):
            pass

    def job(self) -> dict[str, object]:
        found = self.get("jobs/offsite")
        assert found is not None
        fields = found["fields"]
        assert isinstance(fields, dict)
        return {k: next(iter(v.values())) for k, v in fields.items()}


@pytest.fixture
def emulator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Emulator]:
    rest_port, hub_port, agent_port = free_port(), free_port(), free_port()
    env = {
        "MINUTEHAND_FIRESTORE_PROJECT": f"minutehand-rewind-fs-{uuid.uuid4().hex[:8]}",
        "FIRESTORE_EXPORTS": str(tmp_path / "exports"),
        "FIRESTORE_PORT": str(rest_port),
        "FIRESTORE_HUB_PORT": str(hub_port),
        "PORT": str(agent_port),
        "AGENT_PIDFILE": str(tmp_path / "agent.pid"),
        "AGENT_LOG": str(tmp_path / "agent.log"),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)  # the hooks Minutehand runs inherit its environment
    running = Emulator(rest=f"http://127.0.0.1:{rest_port}", agent_port=agent_port, env=dict(os.environ))
    began = time.monotonic()
    running.hook("up")
    print(f"\nemulator up in {time.monotonic() - began:.1f} s")
    try:
        yield running
    finally:
        subprocess.run([*HOOKS, "stop-agent"], env=running.env, check=False, timeout=60)
        subprocess.run([*HOOKS, "down"], env=running.env, check=False, timeout=300)


def recipe_agent(port: int, *, restore: list[str] | None = None) -> AgentUnderTest:
    """agent.yaml as written, on the test's port, its hooks run by this Python from the recipe."""
    agent = AgentUnderTest.model_validate_json(
        load_agent(RECIPE / "agent.yaml").model_dump_json().replace("127.0.0.1:8710", f"127.0.0.1:{port}")
    )
    assert agent.state is not None
    return agent.model_copy(
        update={
            "state": agent.state.model_copy(
                update={
                    "snapshot": [*HOOKS, "snapshot"],
                    "stop": [*HOOKS, "stop-agent"],
                    "restore": restore or [*HOOKS, "restore"],
                    "start": [*HOOKS, "start-agent"],
                }
            )
        }
    )


async def test_the_firestore_emulator_is_rewound_by_restarting_it_with_an_import_and_a_wrong_snapshot_is_caught(
    emulator: Emulator, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    emulator.hook("start-agent")
    agent = recipe_agent(emulator.agent_port)

    [parent] = await session.play(load_scenario(RECIPE / "scenario.yaml"), agent, state=state)

    points = session.fork_points(state, parent.record.run_id)
    assert [p.wake for p in points] == [0, 1, 2, 3, 4, 4]
    assert all(isinstance(p.agent, Restorable) for p in points), points
    after_start = points[1]
    # What the agent wrote at its last wake, through the emulator's REST API
    assert emulator.job() == {"status": "idle", "follow_ups": "2", "next_wake": None}

    # The state changes after the checkpoint: the commitment is deleted and a later document appears.
    emulator.send("DELETE", "jobs/offsite/commitments/rosa_confirms_venue")
    emulator.send("PATCH", "jobs/later", {"note": {"stringValue": "written after the checkpoint"}})

    seen: dict[str, object] = {}

    def at_each_step(line: str) -> None:
        print(f"restore {line}")
        if line.startswith("verify:"):  # the agent is restored and has answered; nothing has run since
            seen["job"] = emulator.job()
            seen["commitment"] = emulator.get("jobs/offsite/commitments/rosa_confirms_venue")
            seen["later"] = emulator.get("jobs/later")

    began = time.monotonic()
    [child] = await session.fork(
        parent.record.run_id,
        Fork(parent_run=parent.record.run_id, at_seq=after_start.seq),
        state=state,
        progress=at_each_step,
    )
    restored = session.restore_of(state, child.record.run_id)
    assert restored is not None and restored.verified, restored
    seconds = {s.step: s.seconds for s in restored.steps}
    print(
        f"fork: restore step {seconds[RestoreStep.RESTORE]:.1f} s (emulator restarted with --import), "
        f"whole sequence {sum(seconds.values()):.1f} s, fork with its run {time.monotonic() - began:.1f} s"
    )
    assert seen["job"] == {"status": "idle", "follow_ups": "0", "next_wake": "2026-08-26T09:00:00+00:00"}
    assert seen["commitment"] is not None and seen["later"] is None
    assert child.record.parent_run == parent.record.run_id

    # A restore that puts back another moment: the snapshot after wake 2, for a fork from the end of wake 1.
    other = wake_dir(state / session.RUNS, parent.record.run_id, 2)
    wrong = recipe_agent(
        emulator.agent_port,
        restore=["env", f"MINUTEHAND_SNAPSHOT_DIR={other}", *HOOKS, "restore"],
    )
    session.run_dir(state, parent.record.run_id).joinpath(session.AGENT).write_text(wrong.model_dump_json())
    world = session.run_dir(state, parent.record.run_id) / session.WORLD
    before = _runs(world)

    with pytest.raises(RestoreFailed) as refused:
        await session.fork(
            parent.record.run_id,
            Fork(parent_run=parent.record.run_id, at_seq=after_start.seq),
            state=state,
            progress=print,
        )

    message = str(refused.value)
    assert "next_wake: 2026-08-26T09:00:00+00:00 at the checkpoint, 2026-08-28T09:00:00+00:00 after" in message
    assert _runs(world) == before


def _runs(world: Path) -> list[str]:
    db = sqlite3.connect(world)
    try:
        return [r[0] for r in db.execute("SELECT run_id FROM run ORDER BY rowid")]
    finally:
        db.close()
