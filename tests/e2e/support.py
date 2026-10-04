"""What the end-to-end tests share: the scenario, the agent file, and the agent's own program.

The agent is `agents/slack_agent.py`, started by Minutehand itself as `-- <command>` would start it, on a
port of its own, keeping its state in a file the test can read afterwards.
"""

from __future__ import annotations

import json
import socket
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.agent import AgentUnderTest, GoalByMessage, GoalByWake, Reported, StateHooks
from minutehand.domain.people import GeneratedSecret, InboundTarget
from minutehand.domain.scenario import (
    DelayRange,
    Person,
    PersonAsked,
    ReplyBehaviour,
    Scenario,
    Scripted,
    ScriptedReply,
    Silent,
)
from minutehand.domain.world import Actor, MessageSnapshot, Operation, WorldEvent
from minutehand.session import RUNS, WORLD

AGENT = Path(__file__).parent / "agents" / "slack_agent.py"
T0 = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)  # a Monday
SECRET_VARIABLE = "AGENT_SLACK_SIGNING_SECRET"
OWNER = "owner@example.com"
SOFIA = "sofia@example.com"
QUESTION = "Could you confirm the partner pricing, please?"
FOLLOW_UP = "Following up on the partner pricing: could you confirm it?"
ANSWER = "Yes, 40k a year."
THANKS = "Thank you!"


def answers(*, to_ask: int = 1, after: timedelta) -> Scripted:
    return Scripted(
        delay=DelayRange(shortest=after, longest=after), replies=[ScriptedReply(to_ask=to_ask, text=ANSWER)]
    )


NEVER_EXPECTED_TO_ANSWER = Scripted(replies=[])
"""Someone who answers nothing and from whom no answer is expected: the owner, thanked at the end."""


def scenario(sofia: ReplyBehaviour, *, owner: ReplyBehaviour = Silent(), name: str = "partner_pricing") -> Scenario:  # noqa: B008 - a frozen model
    return Scenario(
        name=name,
        goal="The partner pricing is confirmed with Sofia.",
        owner="owner",
        starts_at=T0,
        deadline_after=timedelta(days=14),
        people=[
            Person(key="owner", name="Olive Owner", email=OWNER, reply=owner),
            Person(key="sofia", name="Sofia Romano", email=SOFIA, reply=sofia),
        ],
        expect=[PersonAsked(person="sofia"), PersonAsked(person="owner", mentions=["confirmed"])],
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass(frozen=True)
class Launched:
    """The agent file and the command that starts the agent's program."""

    agent: AgentUnderTest
    command: list[str]
    state_file: Path

    def state(self) -> dict[str, object]:
        loaded = json.loads(self.state_file.read_text())
        assert isinstance(loaded, dict)
        return loaded


def agent_under_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    behaviour: str,
    *,
    by_message: bool = False,
    hooks: bool = False,
) -> Launched:
    """The agent under test, its behaviour chosen through the environment its command inherits."""
    monkeypatch.setenv("AGENT_BEHAVIOUR", behaviour)
    monkeypatch.setenv("ASK_EMAIL", SOFIA)
    monkeypatch.setenv("OWNER_EMAIL", OWNER)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    state_file = tmp_path / "agent" / "state.json"
    program = [sys.executable, str(AGENT)]
    agent = AgentUnderTest(
        name=f"{behaviour}_agent",
        goal=GoalByMessage(provider="slack") if by_message else GoalByWake(),
        wakes=[] if by_message else [Reported(wake_url=f"{base}/wake", report_url=f"{base}/report")],
        inbound=[
            InboundTarget(provider="slack", url=f"{base}/slack/events", secret=GeneratedSecret(env=SECRET_VARIABLE))
        ],
        state=StateHooks(
            snapshot=[*program, "snapshot", str(state_file)], restore=[*program, "restore", str(state_file)]
        )
        if hooks
        else None,
    )
    return Launched(
        agent=agent, command=[*program, "serve", "--port", str(port), "--state", str(state_file)], state_file=state_file
    )


def world(state: Path, run_id: str, *, root: str | None = None) -> SqliteStore:
    """A finished run's world, read from its directory (a fork's, from its root's file)."""
    return SqliteStore(state / RUNS / (root or run_id) / WORLD, run_id, RunClock(T0))


def messages(events: list[WorldEvent], actor: Actor, to: str | None = None) -> list[WorldEvent]:
    return [
        e
        for e in events
        if e.actor is actor
        and e.operation is Operation.CREATE
        and isinstance(e.after, MessageSnapshot)
        and (to is None or to in e.after.recipient_emails)
    ]


def texts(events: list[WorldEvent]) -> list[str]:
    return [e.after.text for e in events if isinstance(e.after, MessageSnapshot)]
