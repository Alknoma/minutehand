from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from minutehand.adapters.agent.reach import reach_for
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.domain.agent import AgentUnderTest, Command, StateHooks, WakeSource
from minutehand.domain.people import InboundTarget
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import DelayRange, Person, ReplyBehaviour, Scenario, Scripted, ScriptedReply, Silent
from minutehand.ports.clock import Clock
from minutehand.ports.people import Replier
from tests.orchestrator.world import CHAT, SECRET, Chat, RecordingClock, Scheduler, Switchboard, serving

AGENTS = Path(__file__).parent / "agents"
T0 = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)  # a Monday
QUIET = timedelta(milliseconds=20)
"""The test agents' quiet period: each checkpoint waits this long after the agent's last call."""


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
class Rig:
    base: str
    board: Switchboard
    chat: Chat
    sched: Scheduler
    tmp: Path

    def agent(self, behaviour: str, *, extra: list[WakeSource] | None = None, hooks: bool = False) -> AgentUnderTest:
        state = None
        if hooks:
            hook = [sys.executable, str(AGENTS / "hooks.py")]
            state = StateHooks(
                snapshot=[*hook, "snapshot", str(self.tmp / "agent")],
                restore=[*hook, "restore", str(self.tmp / "agent")],
                quiet=QUIET,
            )
        return AgentUnderTest(
            name="asker",
            wakes=[Command(argv=[sys.executable, str(AGENTS / "asker.py"), behaviour]), *(extra or [])],
            inbound=[InboundTarget(provider=CHAT, url=f"{self.base}/{CHAT}/pushed")],
            state=state,
        )

    def env(self, **more: str) -> dict[str, str]:
        return {"MH_BASE": self.base, "AGENT_STATE": str(self.tmp / "agent"), **more}

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
            mounts=self.board,
            state_dir=self.tmp / "state",
            signing={CHAT: SECRET},
            traffic=self.board,
        )
        return record, store, clock


@asynccontextmanager
async def rigged(tmp_path: Path) -> AsyncIterator[Rig]:
    board = Switchboard()
    async with serving(board) as base:
        yield Rig(base=base, board=board, chat=Chat(), sched=Scheduler(), tmp=tmp_path)
