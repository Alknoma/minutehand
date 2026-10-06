"""A whole run of an agent that books its follow-up with EventBridge Scheduler and takes it from SQS.

The real proxy, the AWS provider, stock boto3 in the agent's own process configured only by its environment. The
agent's report says IDLE throughout, and it takes a second and a half to act on what its queue delivers: only the
scheduler knows the booking's wake is not over until the agent deletes the message.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.checkpoint import Restorable
from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import AgentUnderTest, Booked, Reported, StateHooks
from minutehand.domain.experiment import Fork
from minutehand.domain.scenario import Person, Scenario, Silent
from minutehand.domain.world import Actor, Operation, RecordSnapshot
from tests.e2e.support import OWNER, T0, free_port, world

AGENT = Path(__file__).parent / "agents" / "sqs_agent.py"
SCENARIO = Scenario(
    name="booked_follow_up",
    goal="Follow up five hours from now.",
    owner="owner",
    starts_at=T0,
    deadline_after=timedelta(days=2),
    people=[Person(key="owner", name="Olive Owner", email=OWNER, reply=Silent())],
)


@dataclass(frozen=True)
class SqsAgent:
    agent: AgentUnderTest
    command: list[str]
    state_file: Path


def sqs_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, hooks: bool = False) -> SqsAgent:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "agent")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    state_file = tmp_path / "agent" / "state.json"
    program = [sys.executable, str(AGENT)]
    state = StateHooks(
        snapshot=[*program, "snapshot", str(state_file)],
        restore=[*program, "restore", str(state_file)],
        quiet=timedelta(milliseconds=300),
        settle_limit=timedelta(seconds=3),  # the poller is never quiet before its delivery: those are not restorable
    )
    agent = AgentUnderTest(
        name="sqs_agent",
        wakes=[Reported(wake_url=f"{base}/wake", report_url=f"{base}/report"), Booked()],
        state=state if hooks else None,
    )
    command = [*program, "--port", str(port), "--state", str(state_file), "--act", "1.5"]
    return SqsAgent(agent=agent, command=command, state_file=state_file)


async def test_the_bookings_wake_ends_only_once_the_agent_has_deleted_its_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before, the AWS provider could not say whether a delivery was taken: the run moved past the booking's wake
    as soon as the report said IDLE, and the agent's delete landed at the deadline the clock ran on to, or after
    the run had ended."""
    launched = sqs_agent(tmp_path, monkeypatch)

    [outcome] = await session.play(SCENARIO, launched.agent, state=tmp_path / "state", command=launched.command)

    assert json.loads(launched.state_file.read_text())["deleted"] == ['{"do": "follow up"}']
    events = world(tmp_path / "state", outcome.record.run_id).events()
    [delivered] = [e for e in events if isinstance(e.after, RecordSnapshot) and e.after.resource == "queue_message"]
    [taken] = [e for e in events if e.actor is Actor.AGENT and e.entity == delivered.entity]
    assert taken.operation is Operation.DELETE
    assert taken.sim_time == T0 + timedelta(hours=5), "taken in the booking's own wake, at the booking's moment"
    assert taken.wake == delivered.wake == 2


async def test_a_fork_after_the_agent_used_aws_is_refused_naming_what_it_cannot_rewind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkpoint after the delivery was taken has nothing pending, so the pending-booking refusal let it
    through: the child's AWS was a fresh account without the agent's queue, which the restored agent still named,
    and the fork ran as if nothing were missing."""
    launched = sqs_agent(tmp_path, monkeypatch, hooks=True)
    state = tmp_path / "state"
    [outcome] = await session.play(SCENARIO, launched.agent, state=state, command=launched.command)
    run_id = outcome.record.run_id
    last = [p for p in session.fork_points(state, run_id) if isinstance(p.agent, Restorable)][-1]
    assert last.wake >= 2, "the fork is taken after the booking's wake, with nothing pending"

    refusal = (
        rf"the fork at seq {last.seq} of run {run_id} cannot rewind what the run had built up in aws "
        r"\(\d+ call\(s\) before it, the first POST sqs\.us-east-1\.amazonaws\.com/\), which keeps its queues"
    )
    with pytest.raises(RunRefused, match=refusal):
        await session.fork(run_id, Fork(parent_run=run_id, at_seq=last.seq), state=state, command=launched.command)
    assert [o.record.run_id for o in session.runs(state)] == [run_id], "the refused fork left no run behind"
