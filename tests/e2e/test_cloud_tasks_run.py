"""A whole run of an agent that defers its follow-up with Google Cloud Tasks: the real proxy, the provider, stock
`google-cloud-tasks` in the agent's own process, and Cloud Tasks calling the agent back on the run's clock."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.adapters.providers.google_cloud_tasks.provider import CloudTasksSeed, SeededQueue
from minutehand.domain.agent import AgentUnderTest, Booked, Reported
from minutehand.domain.scenario import DispatchFault, DispatchRule, Person, PlannedBy, ProviderSeed, Scenario, Silent
from tests.e2e.support import OWNER, T0, free_port

pytestmark = pytest.mark.timeout(120)

AGENT = Path(__file__).parent / "agents" / "tasks_agent.py"
QUEUE = "projects/sim-project/locations/us-central1/queues/follow-ups"


def scenario(**more: object) -> Scenario:
    queue = SeededQueue(name=QUEUE, min_backoff=timedelta(minutes=1))
    return Scenario.model_validate(
        {
            "name": "deferred_follow_up",
            "goal": "Follow up five hours from now.",
            "owner": "owner",
            "starts_at": T0,
            "deadline_after": timedelta(days=1),
            "people": [Person(key="owner", name="Olive Owner", email=OWNER, reply=Silent())],
            "provider_seeds": [
                ProviderSeed(provider="google_cloud_tasks", body=CloudTasksSeed(queues=[queue]).model_dump_json())
            ],
            **more,
        }
    )


async def play(tmp_path: Path, scn: Scenario, *flags: str) -> dict[str, list[dict[str, str]]]:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    state_file = tmp_path / "agent" / "state.json"
    agent = AgentUnderTest(
        name="tasks_agent", wakes=[Reported(wake_url=f"{base}/wake", report_url=f"{base}/report"), Booked()]
    )
    command = [sys.executable, str(AGENT), "--port", str(port), "--state", str(state_file), "--queue", QUEUE, *flags]
    await session.play(scn, agent, state=tmp_path / "state", command=command)
    found: dict[str, list[dict[str, str]]] = json.loads(state_file.read_text())
    return found


async def test_the_task_the_agent_defers_calls_it_back_five_simulated_hours_later(tmp_path: Path) -> None:
    held = await play(tmp_path, scenario())

    [handled] = held["handled"]
    assert handled["body"] == '{"do": "follow up"}' and handled["retry"] == "0"
    assert held["created"] == [f"{QUEUE}/tasks/{handled['task']}"]


async def test_a_handler_that_answers_503_is_called_again_after_the_queues_backoff(tmp_path: Path) -> None:
    held = await play(tmp_path, scenario(), "--fail-first")

    assert [h["retry"] for h in held["handled"]] == ["0", "1"]


async def test_a_task_the_scenario_delivers_twice_calls_the_agent_twice(tmp_path: Path) -> None:
    rule = DispatchRule(wakes=PlannedBy.BOOKED, fault=DispatchFault.TWICE, by=timedelta(minutes=2))
    held = await play(tmp_path, scenario(dispatch=[rule]))

    assert [h["retry"] for h in held["handled"]] == ["0", "1"]
