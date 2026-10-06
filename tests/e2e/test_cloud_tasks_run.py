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


async def test_a_check_the_agents_repository_keeps_runs_after_the_run_and_its_finding_is_kept(tmp_path: Path) -> None:
    (tmp_path / "team_checks.py").write_text(
        """
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, Severity


class NoTaskWithoutAName:
    id = "no_task_without_a_name"
    needs = frozenset({Needs.WORLD})

    def run(self, view):
        made = [e for e in view.events if e.entity.provider == "google_cloud_tasks" and e.actor.value == "agent"]
        unnamed = [e.seq for e in made if e.entity.external_id.rsplit("/", 1)[1].isdigit()]
        if not unnamed:
            return CheckReport()
        return CheckReport(findings=[Finding(check=self.id, severity=Severity.WARNING, kind=FindingKind.REVIEW,
            message="a task was created without a name, so a retried create would not be deduplicated",
            evidence=unnamed)])
"""
    )
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    agent = AgentUnderTest(
        name="tasks_agent",
        wakes=[Reported(wake_url=f"{base}/wake", report_url=f"{base}/report"), Booked()],
        checks=[str(tmp_path / "team_checks.py")],
    )
    command = [sys.executable, str(AGENT), "--port", str(port), "--state", str(tmp_path / "s.json"), "--queue", QUEUE]

    [outcome] = await session.play(scenario(), agent, state=tmp_path / "state", command=command)

    [finding] = [f for f in outcome.result.findings if f.check == "no_task_without_a_name"]
    assert finding.message.startswith("a task was created without a name") and finding.evidence
    kept = session.load(tmp_path / "state", outcome.record.run_id).result
    assert "no_task_without_a_name" in {f.check for f in kept.findings}
