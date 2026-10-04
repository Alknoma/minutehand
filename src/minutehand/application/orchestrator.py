"""The run loop: one scenario, one agent, a set of providers, played forward on the run's clock.

Flow of one wake (docs/design.md): jump the clock to the earliest pending moment, fire what is due there,
wake the agent if any of it reaches the agent, poll it until it is no longer working, then read what it did
from the world and schedule what the world owes back: people's replies and tickets' fates.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from minutehand.application.checkpoint import (
    Checkpoint, Pending, PendingBooking, PendingDirection, PendingFate, PendingReply, PendingWake, write_checkpoint,
)
from minutehand.application.refusals import AgentFailed, RunRefused
from minutehand.application.run_clock import RunClock
from minutehand.application.state_hooks import run_hook, wake_dir
from minutehand.domain.agent import AgentReport, AgentStatus, AgentUnderTest, Booked, Commitment, WakeReason, WakeRequest
from minutehand.domain.checks import WakeRecord
from minutehand.domain.clock import Due, DueKind, next_jump
from minutehand.domain.people import InboundTarget, PersonReply
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Person, ProviderKey, Scenario
from minutehand.domain.world import Actor, EntityRef, MessageSnapshot, Operation, TicketSnapshot, WorldEvent
from minutehand.ports.agent import AgentDriver
from minutehand.ports.people import Replier
from minutehand.ports.provider import ASGIApp, BooksWakes, EditsTickets, HoldsTickets, Provider, PushesEvents
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry

_NOT_CHANGES = frozenset({Operation.READ, Operation.SEARCH})

_PRIORITY = [WakeReason.PERSON_REPLIED, WakeReason.DIRECTION, WakeReason.DUE, WakeReason.TICK]
"""When one jump fires several things, the wake carries the reason that matters most to the agent."""


class Mounts(Protocol):
    """Whoever serves the providers' APIs to the agent (the proxy). Told once per run, since a fork is a new world."""

    def mount(self, provider: ProviderKey, app: ASGIApp) -> None: ...


@dataclass(frozen=True)
class Services:
    """The providers in a run, and which of the optional ports each one also implements."""

    providers: Sequence[Provider]
    pushes: Mapping[ProviderKey, PushesEvents] = field(default_factory=dict)
    tickets: Mapping[ProviderKey, HoldsTickets] = field(default_factory=dict)
    editors: Mapping[ProviderKey, EditsTickets] = field(default_factory=dict)
    schedulers: Mapping[ProviderKey, BooksWakes] = field(default_factory=dict)

    def __post_init__(self) -> None:
        keys = [p.manifest.key for p in self.providers]
        if len(keys) != len(set(keys)):
            raise RunRefused(f"two providers share a key: {sorted(keys)}")
        known = set(keys)
        for role, mapping in (("pushes", self.pushes), ("tickets", self.tickets), ("editors", self.editors),
                              ("schedulers", self.schedulers)):
            stray = sorted(set(mapping) - known)
            if stray:
                raise RunRefused(f"{role} names providers that are not in the run: {', '.join(stray)}")
        for provider in self.providers:
            key = provider.manifest.key
            if provider.manifest.pushes_events != (key in self.pushes):
                raise RunRefused(f"provider {key}: manifest pushes_events={provider.manifest.pushes_events} "
                                 f"but it is{'' if key in self.pushes else ' not'} given as PushesEvents")
            if provider.manifest.books_wakes != (key in self.schedulers):
                raise RunRefused(f"provider {key}: manifest books_wakes={provider.manifest.books_wakes} "
                                 f"but it is{'' if key in self.schedulers else ' not'} given as BooksWakes")


@dataclass(frozen=True)
class Reach:
    """How the loop reaches the agent: `main` answers every wake but ticks; `ticks` answers a `Polled` rhythm.

    Built from `AgentUnderTest.wakes` by `adapters.agent.reach_for`.
    """

    main: AgentDriver | None
    ticks: AgentDriver | None = None
    every: timedelta | None = None

    def __post_init__(self) -> None:
        if (self.ticks is None) != (self.every is None):
            raise RunRefused("a polled driver needs its interval, and an interval needs a polled driver")
        if self.main is None and self.ticks is None:
            raise RunRefused("the agent has no driver that can receive a wake")
        if self.every is not None and self.every <= timedelta(0):
            raise RunRefused(f"a polled interval must be positive, not {self.every}")

    def for_reason(self, reason: WakeReason) -> AgentDriver:
        if reason is WakeReason.TICK and self.ticks is not None:
            return self.ticks
        driver = self.main or self.ticks
        assert driver is not None
        return driver


class _Bookings:
    """`ports.provider.Wakes` for one scheduler provider: its bookings enter the run's pending set."""

    def __init__(self, loop: "Orchestrator", provider: ProviderKey) -> None:
        self._loop = loop
        self._provider = provider

    def book(self, due: Due) -> None:
        self._loop.book(self._provider, due)

    def cancel(self, ref: str) -> None:
        self._loop.cancel(self._provider, ref)


class Orchestrator:
    def __init__(
        self,
        *,
        scenario: Scenario,
        agent: AgentUnderTest,
        reach: Reach,
        store: Store,
        clock: RunClock,
        services: Services,
        replier: Replier,
        telemetry: Telemetry | None = None,
        mounts: Mounts | None = None,
        state_dir: Path | None = None,
        poll_interval: float = 0.05,
        max_polls: int = 1200,
        parent_run: str | None = None,
        forked_at: int | None = None,
        prior_wakes: Sequence[WakeRecord] = (),
    ) -> None:
        if agent.state is not None and state_dir is None:
            raise RunRefused(f"agent {agent.name} has state hooks; the run needs a state_dir to snapshot into")
        if any(isinstance(w, Booked) for w in agent.wakes) and not services.schedulers:
            raise RunRefused(f"agent {agent.name} declares Booked wakes but no provider in the run books wakes")
        self._scenario = scenario
        self._agent = agent
        self._reach = reach
        self._store = store
        self._clock = clock
        self._services = services
        self._replier = replier
        self._telemetry = telemetry
        self._mounts = mounts
        self._state_dir = state_dir
        self._poll_interval = poll_interval
        self._max_polls = max_polls
        self._parent_run = parent_run
        self._forked_at = forked_at
        self._wakes: list[WakeRecord] = list(prior_wakes)
        self._pending: list[Pending] = []
        self._replies: list[PersonReply] = []
        self._fated: list[EntityRef] = []
        self._commitments: list[Commitment] | None = None
        self._seen = 0
        self._people = {p.email: p for p in scenario.people}

    # -- Wakes, for scheduler providers ---------------------------------------------------------------------

    def book(self, provider: ProviderKey, due: Due) -> None:
        self.cancel(provider, due.ref)
        self._pending.append(PendingBooking(
            due=Due(at=due.at, kind=DueKind.AGENT_WAKE, ref=f"booking:{provider}:{due.ref}"),
            provider=provider, ref=due.ref,
        ))

    def cancel(self, provider: ProviderKey, ref: str) -> None:
        self._pending = [
            p for p in self._pending if not (isinstance(p, PendingBooking) and p.provider == provider and p.ref == ref)
        ]

    # -- the run ------------------------------------------------------------------------------------------------

    async def run(self) -> RunRecord:
        """A fresh run: seed the world, checkpoint the setup, send START, and play forward."""
        started = time.monotonic()
        self._begin()
        for provider in self._services.providers:
            provider.seed(self._scenario, self._store)
        for i, direction in enumerate(self._scenario.directions):
            self._pending.append(PendingDirection(
                due=Due(at=self._scenario.starts_at + direction.after, kind=DueKind.DIRECTION, ref=f"direction:{i}"),
                text=direction.text,
            ))
        if self._reach.every is not None:
            self._schedule_tick()
        self._record_new()
        try:
            await self._checkpoint()
        except AgentFailed:
            return self._end(StopReason.AGENT_FAILED, started)
        stop = await self._start()
        if stop is None:
            stop = await self._loop()
        return self._end(stop, started)

    async def resume(self, checkpoint: Checkpoint) -> RunRecord:
        """Carry on from a checkpoint in a store that already holds the world up to it (a fork)."""
        started = time.monotonic()
        if self._clock.now() != checkpoint.now or self._clock.wake() != checkpoint.wake:
            raise RunRefused(f"the clock is at {self._clock.now()} wake {self._clock.wake()}; "
                             f"the checkpoint is at {checkpoint.now} wake {checkpoint.wake}")
        self._begin()
        self._replies = self._store.replies()
        if len(self._replies) < checkpoint.replies:
            raise RunRefused(f"the checkpoint counts {checkpoint.replies} replies; the store holds {len(self._replies)}")
        self._pending = list(checkpoint.pending)
        self._fated = list(checkpoint.fated)
        self._commitments = checkpoint.commitments
        self._seen = self._store.head()
        stop = await self._start() if checkpoint.wake == 0 else None
        if stop is None:
            stop = await self._loop()
        return self._end(stop, started)

    def _begin(self) -> None:
        if self._telemetry is not None:
            self._telemetry.run_started(self._store.run_id, self._scenario)
        if self._mounts is not None:
            for provider in self._services.providers:
                self._mounts.mount(provider.manifest.key, provider.app(self._store, self._clock))
        for key, scheduler in self._services.schedulers.items():
            scheduler.bind(_Bookings(self, key))

    def _end(self, stop: StopReason, started: float) -> RunRecord:
        record = RunRecord(
            run_id=self._store.run_id, scenario=self._scenario.name, seed=self._scenario.seed,
            parent_run=self._parent_run, forked_at=self._forked_at,
            started_at=self._scenario.starts_at, ended_at=self._clock.now(),
            wall_seconds=time.monotonic() - started, stop=stop, wakes=self._wakes,
        )
        if self._telemetry is not None:
            self._telemetry.run_ended(record, None)
        return record

    async def _start(self) -> StopReason | None:
        wake = self._clock.begin_wake()
        request = WakeRequest(run_id=self._store.run_id, now=self._clock.now(), reason=WakeReason.START,
                              goal=self._scenario.goal)
        return await self._wake(wake, [(self._reach.for_reason(WakeReason.START), request)])

    async def _loop(self) -> StopReason:
        deadline = self._scenario.deadline
        while True:
            jump = next_jump(self._clock.now(), [p.due for p in self._pending])
            if jump is None:
                return StopReason.NOTHING_PENDING
            if deadline is not None and jump.now > deadline:
                return StopReason.DEADLINE_PASSED
            fired = [p for p in self._pending if p.due in jump.firing]
            self._pending = [p for p in self._pending if p.due not in jump.firing]
            self._clock.jump(jump.now)
            reaches_agent = [p for p in fired if not isinstance(p, PendingFate)]
            wake = self._clock.begin_wake() if reaches_agent else None
            await self._fire(fired)
            if wake is None:
                continue
            stop = await self._wake(wake, self._requests(fired))
            if stop is not None:
                return stop

    async def _fire(self, fired: list[Pending]) -> None:
        """Change the world for what is due, in a fixed order: tickets, then replies, then bookings."""
        for item in fired:
            if isinstance(item, PendingFate):
                self._tickets(item.ticket.provider).transition(item.ticket, item.becomes, self._store, self._clock)
        for item in fired:
            if isinstance(item, PendingReply):
                reply = self._replies[item.reply]
                provider = reply.in_reply_to.provider
                await self._pushes(provider).deliver(reply, self._inbound(provider), self._store, self._clock)
        for item in fired:
            if isinstance(item, PendingBooking):
                await self._services.schedulers[item.provider].fire(item.ref, self._store, self._clock)
        for item in fired:
            if isinstance(item, PendingWake) and item.reason is WakeReason.TICK:
                self._schedule_tick()

    def _requests(self, fired: list[Pending]) -> list[tuple[AgentDriver, WakeRequest]]:
        """One request per driver that must hear of this wake. A wake made only of bookings sends none:
        the scheduler's own delivery is the wake."""
        reasons: set[WakeReason] = set()
        directions: list[str] = []
        for item in fired:
            if isinstance(item, PendingReply):
                reasons.add(WakeReason.PERSON_REPLIED)
            elif isinstance(item, PendingDirection):
                reasons.add(WakeReason.DIRECTION)
                directions.append(item.text)
            elif isinstance(item, PendingWake):
                reasons.add(item.reason)
        if not reasons:
            return []
        reason = next(r for r in _PRIORITY if r in reasons)
        now = self._clock.now()
        run_id = self._store.run_id
        first = WakeRequest(run_id=run_id, now=now, reason=reason,
                            direction="\n\n".join(directions) if directions else None)
        requests = [(self._reach.for_reason(reason), first)]
        if WakeReason.TICK in reasons and reason is not WakeReason.TICK:
            ticks = self._reach.for_reason(WakeReason.TICK)
            if ticks is not requests[0][0]:
                requests.append((ticks, WakeRequest(run_id=run_id, now=now, reason=WakeReason.TICK)))
        return requests

    async def _wake(self, wake: int, requests: list[tuple[AgentDriver, WakeRequest]]) -> StopReason | None:
        """Send the wake, wait until the agent stops working, read what it did, and checkpoint."""
        if self._telemetry is not None:
            self._telemetry.wake_started(wake, requests[0][1].reason if requests else WakeReason.DUE, self._clock.now())
        failed = False
        done = False
        commitments_changed = False
        try:
            for driver, request in requests:
                await driver.wake(request)
            for driver, _ in requests:
                report = await self._settle(driver)
                done = done or report.status is AgentStatus.DONE
                if driver is self._reach.main:
                    commitments_changed = self._adopt(report)
        except AgentFailed:
            failed = True
        new = self._record_new()
        if not failed:
            await self._schedule(new)
        self._wakes.append(WakeRecord(
            index=wake, sim_time=self._clock.now(),
            world_changes=sum(1 for e in new if e.wake == wake and e.actor is Actor.AGENT
                              and e.operation not in _NOT_CHANGES),
            commitments_changed=commitments_changed,
        ))
        if self._telemetry is not None:
            self._telemetry.wake_ended(wake)
        if failed:
            return StopReason.AGENT_FAILED
        try:
            await self._checkpoint()
        except AgentFailed:
            return StopReason.AGENT_FAILED
        if done:
            return StopReason.AGENT_DONE
        if len(self._wakes) >= self._scenario.max_wakes:
            return StopReason.WAKE_LIMIT
        return None

    async def _settle(self, driver: AgentDriver) -> AgentReport:
        for _ in range(self._max_polls):
            report = await driver.report()
            if report.status is not AgentStatus.WORKING:
                return report
            await asyncio.sleep(self._poll_interval)
        raise AgentFailed(f"agent {self._agent.name} was still working after {self._max_polls} polls")

    def _adopt(self, report: AgentReport) -> bool:
        """Take the agent's next wake, replacing the one it named before. Answers whether its commitments changed."""
        self._pending = [p for p in self._pending if not (isinstance(p, PendingWake) and p.reason is WakeReason.DUE)]
        if report.next_wake is not None:
            self._pending.append(PendingWake(
                due=Due(at=report.next_wake, kind=DueKind.AGENT_WAKE, ref="next_wake"), reason=WakeReason.DUE,
            ))
        changed = report.commitments != self._commitments
        self._commitments = report.commitments
        return changed

    def _schedule_tick(self) -> None:
        assert self._reach.every is not None
        self._pending.append(PendingWake(
            due=Due(at=self._clock.now() + self._reach.every, kind=DueKind.AGENT_WAKE, ref="tick"),
            reason=WakeReason.TICK,
        ))

    def _record_new(self) -> list[WorldEvent]:
        new = self._store.events(since=self._seen)
        if new:
            self._seen = new[-1].seq
        if self._telemetry is not None:
            for event in new:
                self._telemetry.recorded(event)
        return new

    async def _schedule(self, new: list[WorldEvent]) -> None:
        """What the world owes back for what the agent just did: replies to its messages, fates of its tickets."""
        history: list[WorldEvent] | None = None
        for event in new:
            if event.actor is not Actor.AGENT:
                continue
            after = event.after
            if event.operation is Operation.CREATE and isinstance(after, MessageSnapshot):
                if history is None:
                    history = self._store.events()
                for email in after.recipient_emails:
                    if email in self._people:
                        await self._ask(self._people[email], event, [h for h in history if h.seq <= event.seq])
            if (event.operation in (Operation.CREATE, Operation.UPDATE) and isinstance(after, TicketSnapshot)
                    and after.assignee_email in self._people and event.entity not in self._fated):
                self._fate(self._people[after.assignee_email], event)

    async def _ask(self, person: Person, asked: WorldEvent, history: list[WorldEvent]) -> None:
        reply = await self._replier.decide(person, asked, history, self._clock)
        if reply is None:
            return
        self._pushes(reply.in_reply_to.provider)
        self._inbound(reply.in_reply_to.provider)
        self._store.remember(reply)
        position = len(self._replies)
        self._replies.append(reply)
        self._pending.append(PendingReply(
            due=Due(at=reply.at, kind=DueKind.PERSON_REPLY, ref=f"reply:{position}"), reply=position,
        ))

    def _fate(self, person: Person, assigned: WorldEvent) -> None:
        fate = next((f for f in self._scenario.ticket_fates if f.assignee == person.key), None)
        if fate is None:
            return
        self._tickets(assigned.entity.provider)
        self._fated.append(assigned.entity)
        ticket = assigned.entity
        self._pending.append(PendingFate(
            due=Due(at=assigned.sim_time + fate.after, kind=DueKind.TICKET_FATE,
                    ref=f"fate:{ticket.provider}:{ticket.external_id}"),
            ticket=ticket, becomes=fate.becomes,
        ))

    async def _checkpoint(self) -> None:
        wake = self._clock.wake()
        if self._agent.state is not None:
            assert self._state_dir is not None
            await run_hook(self._agent.state.snapshot, wake_dir(self._state_dir, self._store.run_id, wake))
        write_checkpoint(self._store, Checkpoint(
            wake=wake, now=self._clock.now(), replies=len(self._replies), fated=self._fated,
            commitments=self._commitments, pending=self._pending,
        ))
        self._record_new()

    # -- lookups that refuse loudly -------------------------------------------------------------------------

    def _pushes(self, provider: ProviderKey) -> PushesEvents:
        if provider not in self._services.pushes:
            raise RunRefused(f"a person owes a reply on {provider}, which pushes no events to the agent")
        return self._services.pushes[provider]

    def _inbound(self, provider: ProviderKey) -> InboundTarget:
        target = next((t for t in self._agent.inbound if t.provider == provider), None)
        if target is None:
            raise RunRefused(f"agent {self._agent.name} declares no inbound target for {provider}")
        return target

    def _tickets(self, provider: ProviderKey) -> HoldsTickets:
        if provider not in self._services.tickets:
            raise RunRefused(f"a ticket fate is due on {provider}, which holds no tickets a person can move")
        return self._services.tickets[provider]


async def run_scenario(
    *,
    scenario: Scenario,
    agent: AgentUnderTest,
    reach: Reach,
    store: Store,
    clock: RunClock,
    services: Services,
    replier: Replier,
    telemetry: Telemetry | None = None,
    mounts: Mounts | None = None,
    state_dir: Path | None = None,
    poll_interval: float = 0.05,
    max_polls: int = 1200,
) -> RunRecord:
    """Run one scenario from its start."""
    if clock.now() != scenario.starts_at or clock.wake() != 0:
        raise RunRefused(f"the clock must start at the scenario's start ({scenario.starts_at}), wake 0")
    return await Orchestrator(
        scenario=scenario, agent=agent, reach=reach, store=store, clock=clock, services=services, replier=replier,
        telemetry=telemetry, mounts=mounts, state_dir=state_dir, poll_interval=poll_interval, max_polls=max_polls,
    ).run()
