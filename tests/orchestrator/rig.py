from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from minutehand_agent._wire import ON, URL

from minutehand.adapters.agent.reach import reach_for
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.telemetry.receiver import AGENT_PATH, Receiver
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.application.run_clock import RunClock
from minutehand.domain.agent import AgentUnderTest, Command, WakeSource
from minutehand.domain.people import InboundTarget
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import (
    DelayRange,
    Person,
    ProviderKey,
    ReplyBehaviour,
    Scenario,
    Scripted,
    ScriptedReply,
    Silent,
)
from minutehand.ports.clock import Clock
from minutehand.ports.people import Replier
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store
from tests.orchestrator.world import CHAT, SECRET, Chat, RecordingClock, Scheduler, Switchboard, serving

AGENTS = Path(__file__).parent / "agents"
T0 = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)  # a Monday


def person(key: str, reply: ReplyBehaviour) -> Person:
    return Person(key=key, name=key.title(), email=f"{key}@example.com", reply=reply)


def scripted(*texts: str, hours: float) -> Scripted:
    delay = timedelta(hours=hours)
    return Scripted(
        delay=DelayRange(shortest=delay, longest=delay),
        replies=[ScriptedReply(to_ask=n + 1, text=t) for n, t in enumerate(texts)],
    )


def scenario(**overrides: object) -> Scenario:
    fields: dict[str, object] = {
        "name": "pricing",
        "goal": "Pricing is confirmed and the legal review is done.",
        "owner": "owner",
        "starts_at": T0,
        "deadline_after": timedelta(days=14),
        "people": [
            person("owner", Silent()),
            person("sofia", scripted("Yes, 40k.", hours=36)),
            person("tom", Silent()),
            person("dania", Silent()),
        ],
        "ticket_fates": [{"assignee": "tom", "becomes": "done", "after": timedelta(days=3)}],
    }
    fields.update(overrides)
    return Scenario.model_validate(fields)


@dataclass
class Mounted:
    """The switchboard and the receiver, mounted on each run together, as the proxy and its receiver are."""

    board: Switchboard
    receiver: Receiver

    def mount(self, world: Store, clock: Clock, apps: Mapping[ProviderKey, ASGIApp], *, scenario: Scenario) -> None:
        self.board.mount(world, clock, apps, scenario=scenario)
        self.receiver.mount(world, clock)

    def flush(self) -> None:
        self.board.flush()


@dataclass
class Rig:
    base: str
    board: Switchboard
    chat: Chat
    sched: Scheduler
    tmp: Path
    receiver: Receiver

    @property
    def mounts(self) -> Mounted:
        return Mounted(self.board, self.receiver)

    def agent(self, behaviour: str, *, extra: list[WakeSource] | None = None) -> AgentUnderTest:
        return AgentUnderTest(
            name="asker",
            wakes=[Command(argv=[sys.executable, str(AGENTS / "asker.py"), behaviour]), *(extra or [])],
            inbound=[InboundTarget(provider=CHAT, url=f"{self.base}/{CHAT}/pushed")],
        )

    def env(self, **more: str) -> dict[str, str]:
        """What the test agents are handed: the test providers, where a file of their own goes, and the run's memory
        (`minutehand_agent.store`), as `minutehand run` hands it."""
        return {
            "MH_BASE": self.base,
            "AGENT_STATE": str(self.tmp / "agent"),
            ON: "1",
            URL: f"http://127.0.0.1:{self.receiver.port}{AGENT_PATH}",
            **more,
        }

    def services(self) -> Services:
        return Services(
            providers=[self.chat, self.sched],
            pushes={CHAT: self.chat},
            tickets={CHAT: self.chat},
            editors={CHAT: self.chat},
            schedulers={self.sched.manifest.key: self.sched},
        )

    def open(self, run_id: str, clock: Clock) -> SqliteStore:
        return SqliteStore(self.tmp / "world.db", run_id, clock)

    async def run(
        self,
        scn: Scenario,
        agent: AgentUnderTest,
        *,
        run_id: str = "root",
        env: dict[str, str] | None = None,
        replier: Replier | None = None,
    ) -> tuple[RunRecord, SqliteStore, RecordingClock]:
        clock = RecordingClock(scn.starts_at)
        store = self.open(run_id, clock)
        record = await run_scenario(
            scenario=scn,
            agent=agent,
            reach=reach_for(agent, env=env or self.env()),
            store=store,
            clock=clock,
            services=self.services(),
            replier=replier or ScriptedReplier(scn),
            mounts=self.mounts,
            signing={CHAT: SECRET},
            traffic=self.board,
        )
        return record, store, clock


@asynccontextmanager
async def rigged(tmp_path: Path) -> AsyncIterator[Rig]:
    board = Switchboard()
    clock = RunClock(T0)
    lobby = SqliteStore(tmp_path / "lobby.db", "lobby", clock)
    async with serving(board) as base, Receiver(lobby, clock) as receiver:
        yield Rig(base=base, board=board, chat=Chat(), sched=Scheduler(), tmp=tmp_path, receiver=receiver)
    lobby.close()
