"""What the agent comes back to by itself is a booking with a scheduler, kept in the run's log; no deadline a task
in its sandbox waits for is a wake.

A sandbox holds deadlines nobody declared: an HTTP server's poll, a runtime's keepalive, a database emulator's event
loop re-arming five minutes forever. Read as wakes, one of them woke a finished run every five simulated minutes,
about 400 times in 19 simulated hours, while the agent's own schedule never drove one. Here the sandbox reports such
deadlines exactly as the patched gVisor prints them, and the run reads none of them. A booking, by contrast, is in
the log: the run delivers it at its moment, and a fork from a checkpoint where it is still pending delivers it too.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path

import httpx

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.provider import SlackProvider, build
from minutehand.adapters.providers.slack.state import BOT_USER_ID, SlackWorld
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.checkpoint import PendingBooking, checkpoints
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier import PeopleReplier
from minutehand.application.restore import Played
from minutehand.application.rewind import fork_run
from minutehand.application.run_clock import RunClock
from minutehand.application.sandbox import SandboxClock, SandboxReading
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.experiment import Fork
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Scenario, Silent
from minutehand.ports.clock import Clock
from tests.providers.slack.slack_workspace import SCENARIO as WORKSPACE
from tests.providers.slack.slack_workspace import TOKEN
from tests.support.scenarios import SANDBOX

SECOND = 1_000_000_000
NETTY = 299_400_000_000  # a JVM emulator's event loop, re-armed every time it fires
DAY = 86_400 * SECOND  # a scheduler thread of the agent's own, asleep until tomorrow
SCENARIO = Scenario.model_validate(
    {
        **WORKSPACE.model_dump(exclude={"goal"}),
        "people": [p.model_copy(update={"reply": Silent()}).model_dump() for p in WORKSPACE.people],
    }
)  # no goal, and nobody answers: the only thing that can wake the agent is what it booked
NOOR = state.conversation_id([BOT_USER_ID, state.user_id("noor")])


class _Kernel(SandboxClock):
    """The sandbox as the patched gVisor reports it: every task blocked, its clock the run's as advanced, and two
    tasks waiting on deadlines that are nobody's wake."""

    def __init__(self, scenario: Scenario) -> None:
        super().__init__(SANDBOX)
        self._now_ns = int(scenario.starts_at.timestamp()) * SECOND

    async def read(self) -> SandboxReading:
        printed = {
            "idle": True,
            "now_ns": self._now_ns,
            "tasks": 3,
            "timed": 2,
            "earliest_ns": NETTY,
            "waits": [{"task": 7, "ns": NETTY}, {"task": 9, "ns": DAY}],
        }
        return SandboxReading.model_validate_json(json.dumps(printed))

    async def advance(self, nanoseconds: int) -> None:
        self._now_ns += nanoseconds


class _Booker:
    """The agent's start: one Slack message to Noor scheduled a day on, through the provider's own Web API."""

    def __init__(self, slack: SlackProvider, store: SqliteStore, clock: RunClock) -> None:
        self._app = slack.app(store, clock)
        self._clock = clock
        self.scheduled: str | None = None

    async def __call__(self) -> None:
        at = int((self._clock.now() + timedelta(days=1)).timestamp())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self._app), base_url="https://slack.com") as c:
            answer = await c.post(
                "/api/chat.scheduleMessage",
                data={"channel": NOOR, "post_at": str(at), "text": "Following up on the launch checklist"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        found = json.loads(answer.text)
        assert found["ok"] is True, found
        self.scheduled = str(found["scheduled_message_id"])


def _services(slack: SlackProvider) -> Services:
    return Services(providers=[slack], pushes={"slack": slack}, schedulers={"slack": slack})


def _posted(store: SqliteStore) -> list[str]:
    world = SlackWorld(store)
    return [m.text for m in world.messages(NOOR)] if world.channel(NOOR) is not None else []


async def _played(tmp_path: Path, *, booking: bool) -> tuple[RunRecord, SqliteStore, SlackProvider]:
    clock = RunClock(SCENARIO.starts_at)
    store = SqliteStore(tmp_path / "world.db", "parent", clock)
    slack = build()
    start = _Booker(slack, store, clock) if booking else None
    record = await run_scenario(
        scenario=SCENARIO,
        agent=AgentUnderTest(name="agent", sandbox=SANDBOX),
        sandbox=_Kernel(SCENARIO),
        store=store,
        clock=clock,
        services=_services(slack),
        replier=PeopleReplier(SCENARIO, None, []),
        start=start,
    )
    return record, store, slack


async def test_no_deadline_a_task_in_the_sandbox_waits_for_is_a_wake(tmp_path: Path) -> None:
    record, _, _ = await _played(tmp_path, booking=False)
    assert [w.reason for w in record.wakes] == ["start"], (
        f"the run woke {len(record.wakes)} times for a sandbox whose only deadlines are a JVM's 299.4 s event loop "
        "and a thread's day-long sleep: a deadline in the sandbox was read as a wake"
    )
    assert record.stop is StopReason.NOTHING_PENDING


async def test_a_booking_is_the_wake_and_is_delivered_at_its_moment(tmp_path: Path) -> None:
    record, store, _ = await _played(tmp_path, booking=True)
    assert [(w.reason, w.sim_time - SCENARIO.starts_at) for w in record.wakes] == [
        ("start", timedelta(0)),
        ("due", timedelta(days=1)),
    ]
    assert _posted(store) == ["Following up on the launch checklist"]
    assert record.stop is StopReason.NOTHING_PENDING


class _Program:
    """`OwnProgram` for an agent whose sandbox is `_Kernel`: there is no process to start or stop."""

    command: Sequence[str] = ("agent",)

    async def start(self, environment: Mapping[str, str] | None = None) -> None:
        del environment

    async def stop(self) -> None:
        return None


class _NothingToPlay:
    """`Playback` of an agent whose only call before the checkpoint booked the message: nothing to answer again,
    since the booking is the scheduler's record in the log the child shares, not the agent's."""

    async def play(self, progress: object, *, start: object) -> Played:
        del progress, start
        return Played(calls_answered=0, deliveries_resent=0, jumps=0)


async def test_a_fork_with_a_booking_pending_delivers_it_from_the_log_it_shares(tmp_path: Path) -> None:
    parent, store, _ = await _played(tmp_path, booking=True)
    pending = {seq: c for seq, c in checkpoints(store).items() if any(isinstance(p, PendingBooking) for p in c.pending)}
    assert pending, "the checkpoint after the start holds the booking"
    at_seq = min(pending)
    store.close()

    def open_parent(clock: Clock) -> SqliteStore:
        return SqliteStore(tmp_path / "world.db", "parent", clock)

    slack = build()
    [child] = await fork_run(
        fork=Fork(parent_run="parent", at_seq=at_seq),
        parent=parent,
        open_parent=open_parent,
        run_id="child",
        scenario=SCENARIO,
        agent=AgentUnderTest(name="agent", sandbox=SANDBOX),
        sandbox=_Kernel(SCENARIO),
        playback_for=lambda _store, _checkpoint: _NothingToPlay(),
        services=_services(slack),
        replier_for=lambda scenario, _pins: PeopleReplier(scenario, None, []),
        state_dir=tmp_path / "state",
        own=_Program(),
    )
    assert [w.reason for w in child.wakes if w.index > pending[at_seq].wake] == ["due"]
    reopened = SqliteStore(tmp_path / "world.db", "child", RunClock(SCENARIO.starts_at))
    assert _posted(reopened) == ["Following up on the launch checklist"]
