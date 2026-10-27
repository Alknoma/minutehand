"""A signed request is stamped with the agent's own clock as it is sent, never with world time.

An agent's stack can take minutes to start inside its sandbox, whose clock runs on in real time meanwhile, so by the
first push the sandbox stands minutes ahead of the run. Slack's `SignatureVerifier` refuses a request timestamp more
than five minutes from the agent's own clock: stamped from the run's clock, the owner's first message was accepted
4 min 36 s after the scenario's start and answered 401 at 4 min 59 s, and that run never had a goal. Here the sandbox
stands 299 s and 301 s ahead, and the agent's verifier reads the sandbox's clock, as a real one does.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier import PeopleReplier
from minutehand.application.run_clock import RunClock
from minutehand.application.sandbox import SandboxClock, SandboxReading
from minutehand.domain.agent import AgentUnderTest, GoalByMessage, Sandbox
from minutehand.domain.people import InboundTarget
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Scenario, Silent
from tests.providers.slack.intercepted import SECRET, AgentEndpoint, agent
from tests.providers.slack.slack_workspace import SCENARIO as WORKSPACE
from tests.support.scenarios import SANDBOX

__all__ = ["agent"]

SECOND = 1_000_000_000
SCENARIO = Scenario.model_validate(
    {
        **WORKSPACE.model_dump(),
        "people": [p.model_copy(update={"reply": Silent()}).model_dump() for p in WORKSPACE.people],
    }
)  # the owner hands the goal by Slack message at the start, and nobody answers
STARTED_SLOWLY = Sandbox(
    deadlines=SANDBOX.deadlines, advance=SANDBOX.advance, settle_limit=timedelta(minutes=10)
)  # an agent whose stack takes minutes to start: its sandbox may stand that far ahead of the run


class _StartedLate(SandboxClock):
    """A sandbox whose clock ran on in real time while the agent's stack started: `ahead` past the run's start."""

    def __init__(self, ahead: timedelta) -> None:
        super().__init__(STARTED_SLOWLY)
        self._now_ns = int((SCENARIO.starts_at + ahead).timestamp()) * SECOND

    async def read(self) -> SandboxReading:
        return SandboxReading.model_validate_json(json.dumps({"idle": True, "now_ns": self._now_ns}))

    async def advance(self, nanoseconds: int) -> None:
        self._now_ns += nanoseconds


@pytest.mark.parametrize("ahead", [299, 301])
async def test_the_goal_message_is_stamped_with_the_sandboxs_clock_and_accepted_by_the_agents_verifier(
    tmp_path: Path, agent: AgentEndpoint, ahead: int
) -> None:
    sandbox_at = SCENARIO.starts_at + timedelta(seconds=ahead)
    agent.clock = RunClock(sandbox_at)  # the agent's verifier reads its own clock: the sandbox's
    clock = RunClock(SCENARIO.starts_at)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    slack = build()
    slack.listen(InboundTarget(provider="slack", url=agent.url), SECRET)

    record = await run_scenario(
        scenario=SCENARIO,
        agent=AgentUnderTest(
            name="agent",
            sandbox=STARTED_SLOWLY,
            goal=GoalByMessage(provider="slack"),
            inbound=[InboundTarget(provider="slack", url=agent.url)],
        ),
        sandbox=_StartedLate(timedelta(seconds=ahead)),
        store=store,
        clock=clock,
        services=Services(providers=[slack], pushes={"slack": slack}),
        replier=PeopleReplier(SCENARIO, None, []),
        signing={"slack": SECRET},
    )

    assert agent.forged == [], "the agent's verifier refused the goal message: it was stamped off the agent's clock"
    assert record.stop is not StopReason.AGENT_FAILED, record.failure
    [goal] = agent.received
    assert goal.headers["x-slack-request-timestamp"] == str(int(sandbox_at.timestamp()))
    assert goal.json["event_time"] == int(SCENARIO.starts_at.timestamp()), "world time is the run's"
    assert int(float(goal.json["event"]["ts"])) == int(SCENARIO.starts_at.timestamp())
