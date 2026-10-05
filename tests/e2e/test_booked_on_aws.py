"""A whole run of an agent that books its follow-up with EventBridge Scheduler and takes it from SQS.

The real proxy, the AWS provider, stock boto3 in the agent's own process configured only by its environment. The
agent's report says IDLE throughout, and it takes a second and a half to act on what its queue delivers: only the
scheduler knows the booking's wake is not over until the agent deletes the message.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.domain.agent import AgentUnderTest, Booked, Reported
from minutehand.domain.scenario import Person, Scenario, Silent
from minutehand.domain.world import Actor, Operation, RecordSnapshot
from tests.e2e.support import OWNER, T0, free_port, world

AGENT = Path(__file__).parent / "agents" / "sqs_agent.py"


async def test_the_bookings_wake_ends_only_once_the_agent_has_deleted_its_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before, the AWS provider could not say whether a delivery was taken: the run moved past the booking's wake
    as soon as the report said IDLE, and the agent's delete landed at the deadline the clock ran on to, or after
    the run had ended."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "agent")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    state_file = tmp_path / "agent" / "state.json"
    agent = AgentUnderTest(
        name="sqs_agent",
        wakes=[Reported(wake_url=f"{base}/wake", report_url=f"{base}/report"), Booked()],
    )
    command = [sys.executable, str(AGENT), "--port", str(port), "--state", str(state_file), "--act", "1.5"]
    scn = Scenario(
        name="booked_follow_up",
        goal="Follow up five hours from now.",
        owner="owner",
        starts_at=T0,
        deadline_after=timedelta(days=2),
        people=[Person(key="owner", name="Olive Owner", email=OWNER, reply=Silent())],
    )

    [outcome] = await session.play(scn, agent, state=tmp_path / "state", command=command)

    assert json.loads(state_file.read_text())["deleted"] == ['{"do": "follow up"}']
    events = world(tmp_path / "state", outcome.record.run_id).events()
    [delivered] = [e for e in events if isinstance(e.after, RecordSnapshot) and e.after.resource == "queue_message"]
    [taken] = [e for e in events if e.actor is Actor.AGENT and e.entity == delivered.entity]
    assert taken.operation is Operation.DELETE
    assert taken.sim_time == T0 + timedelta(hours=5), "taken in the booking's own wake, at the booking's moment"
    assert taken.wake == delivered.wake == 2
