"""The run loop: one scenario, one agent, a set of providers, played forward on the run's clock.

Flow of one wake (docs/design.md): jump the clock to the earliest pending moment, fire what is due there,
wake the agent if any of it reaches the agent, poll it until it is no longer working, then read what it did
from the world and schedule what the world owes back: people's replies and tickets' fates.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from minutehand.application.checkpoint import (
    AgentState,
    Checkpoint,
    NoHooks,
    NotRestorable,
    Pending,
    PendingBooking,
    PendingDirection,
    PendingFate,
    PendingHappening,
    PendingReply,
    PendingWake,
    Restorable,
    write_checkpoint,
)
from minutehand.application.outbound import outbound_uses
from minutehand.application.refusals import AgentFailed, RunRefused
from minutehand.application.restore import RestoreStep, Settled, Traffic, digest_of, run_command, settle
from minutehand.application.run_clock import RunClock
from minutehand.application.state_hooks import take_snapshot, wake_dir
from minutehand.checks.runner import RunResult
from minutehand.domain.agent import (
    AgentReport,
    AgentStatus,
    AgentUnderTest,
    Booked,
    Commitment,
    GoalByMessage,
    WakeReason,
    WakeRequest,
)
from minutehand.domain.checks import WakeRecord
from minutehand.domain.clock import Due, DueKind, next_jump
from minutehand.domain.people import InboundTarget, PersonMessage, PersonReply
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import DocumentHappening, Happening, Person, ProviderKey, Scenario, TicketHappening
from minutehand.domain.world import Actor, EntityRef, MessageSnapshot, Operation, TicketSnapshot, WorldEvent
from minutehand.ports.agent import AgentDriver, Reports, TakesReplies
from minutehand.ports.clock import Clock
from minutehand.ports.people import Replier
from minutehand.ports.provider import (
    ActsOnTickets,
    ASGIApp,
    BooksWakes,
    ChangesDocuments,
    EditsTickets,
    HoldsTickets,
    NotifiesChanges,
    Provider,
    PushesEvents,
    PushesInteractions,
)
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry

_NOT_CHANGES = frozenset({Operation.READ, Operation.SEARCH})

_PRIORITY = [WakeReason.PERSON_REPLIED, WakeReason.DIRECTION, WakeReason.DUE, WakeReason.TICK]
"""When one jump fires several things, the wake carries the reason that matters most to the agent."""


class Mounts(Protocol):
    """Whoever serves the providers' APIs to the agent (the proxy). Told once per run, since a fork is a new world:
    from then on the calls it answers are recorded in `world`, and each provider in `apps` answers its hosts. A
    provider the agent calls that is not in `apps` is seeded with `scenario` on its first call."""

    def mount(self, world: Store, clock: Clock, apps: Mapping[ProviderKey, ASGIApp], *, scenario: Scenario) -> None: ...


class Scorer(Protocol):
    """Whoever judges a finished run (the checks). Its findings and scorecard end the run's telemetry.
    Async, because a judged check waits on a model."""

    async def score(self, record: RunRecord, world: Store) -> RunResult: ...


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
        for role, mapping in (
            ("pushes", self.pushes),
            ("tickets", self.tickets),
            ("editors", self.editors),
            ("schedulers", self.schedulers),
        ):
            stray = sorted(set(mapping) - known)
            if stray:
                raise RunRefused(f"{role} names providers that are not in the run: {', '.join(stray)}")
        for provider in self.providers:
            key = provider.manifest.key
            if provider.manifest.pushes_events != (key in self.pushes):
                raise RunRefused(
                    f"provider {key}: manifest pushes_events={provider.manifest.pushes_events} "
                    f"but it is{'' if key in self.pushes else ' not'} given as PushesEvents"
                )
            if provider.manifest.books_wakes != (key in self.schedulers):
                raise RunRefused(
                    f"provider {key}: manifest books_wakes={provider.manifest.books_wakes} "
                    f"but it is{'' if key in self.schedulers else ' not'} given as BooksWakes"
                )


@dataclass(frozen=True)
class Reach:
    """How the loop reaches the agent: `main` answers every wake but ticks; `ticks` answers a `Polled` rhythm.

    Built from `AgentUnderTest.wakes` by `adapters.agent.reach_for`. Neither is set for an agent reached only
    through pushed events: its goal comes by message (`GoalByMessage`) and acknowledging a push ends its wake.
    """

    main: AgentDriver | None
    ticks: AgentDriver | None = None
    every: timedelta | None = None

    def __post_init__(self) -> None:
        if (self.ticks is None) != (self.every is None):
            raise RunRefused("a polled driver needs its interval, and an interval needs a polled driver")
        if self.every is not None and self.every <= timedelta(0):
            raise RunRefused(f"a polled interval must be positive, not {self.every}")

    def for_reason(self, reason: WakeReason) -> AgentDriver | None:
        """The driver this wake goes to; None when the agent has no wake endpoint and hears only pushed events."""
        if reason is WakeReason.TICK and self.ticks is not None:
            return self.ticks
        return self.main or self.ticks


class _Bookings:
    """`ports.provider.Wakes` for one scheduler provider: its bookings enter the run's pending set."""

    def __init__(self, loop: Orchestrator, provider: ProviderKey) -> None:
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
        scorer: Scorer | None = None,
        state_dir: Path | None = None,
        signing: Mapping[ProviderKey, str] | None = None,
        parent_run: str | None = None,
        forked_at: int | None = None,
        prior_wakes: Sequence[WakeRecord] = (),
        traffic: Traffic | None = None,
        channels: Mapping[ProviderKey, TakesReplies] | None = None,
    ) -> None:
        if agent.state is not None and state_dir is None:
            raise RunRefused(f"agent {agent.name} has state hooks; the run needs a state_dir to snapshot into")
        if agent.state is not None and traffic is None:
            raise RunRefused(
                f"agent {agent.name} has state hooks, and nothing in the run sees its outbound calls, so no "
                "checkpoint could be known to be settled"
            )
        if any(isinstance(w, Booked) for w in agent.wakes) and not services.schedulers:
            raise RunRefused(f"agent {agent.name} declares Booked wakes but no provider in the run books wakes")
        if isinstance(agent.goal, GoalByMessage):
            if agent.goal.provider not in services.pushes:
                raise RunRefused(
                    f"agent {agent.name} takes its goal by message on {agent.goal.provider}, "
                    "which is not a provider in the run that pushes events"
                )
        elif reach.for_reason(WakeReason.START) is None:
            raise RunRefused(f"agent {agent.name} takes its goal in the START wake and has no driver to receive it")
        _refuse_unlanded_happenings(scenario, agent, services)
        self._scenario = scenario
        self._agent = agent
        self._reach = reach
        self._store = store
        self._clock = clock
        self._services = services
        self._replier = replier
        self._telemetry = telemetry
        self._mounts = mounts
        self._scorer = scorer
        self._state_dir = state_dir
        self._signing = dict(signing or {})
        self._parent_run = parent_run
        self._forked_at = forked_at
        self._traffic = traffic
        self._channels = dict(channels or {})
        self._mounted = False
        self._agent_state: AgentState = NoHooks()
        self._last_report: AgentReport | None = None
        self._wakes: list[WakeRecord] = list(prior_wakes)
        self._pending: list[Pending] = []
        self._replies: list[PersonReply] = []
        self._withdrawn: list[int] = []
        self._fated: list[EntityRef] = []
        self._commitments: list[Commitment] | None = None
        self._failure: str | None = None
        self._seen = 0
        self._calls_seen = 0
        self._people = {p.email: p for p in scenario.people}

    # -- Wakes, for scheduler providers ---------------------------------------------------------------------

    def book(self, provider: ProviderKey, due: Due) -> None:
        self.cancel(provider, due.ref)
        self._pending.append(
            PendingBooking(
                due=Due(at=due.at, kind=DueKind.AGENT_WAKE, ref=f"booking:{provider}:{due.ref}"),
                provider=provider,
                ref=due.ref,
            )
        )

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
            self._pending.append(
                PendingDirection(
                    due=Due(
                        at=self._scenario.starts_at + direction.after, kind=DueKind.DIRECTION, ref=f"direction:{i}"
                    ),
                    text=direction.text,
                )
            )
        for i, happening in enumerate(self._scenario.happenings):
            self._pending.append(
                PendingHappening(
                    due=Due(
                        at=self._scenario.starts_at + happening.after, kind=DueKind.HAPPENING, ref=f"happening:{i}"
                    ),
                    happening=i,
                )
            )
        if self._reach.every is not None:
            self._schedule_tick()
        self._record_new()
        try:
            await self._checkpoint()
        except AgentFailed as e:
            self._failure = str(e)
            return await self._end(StopReason.AGENT_FAILED, started)
        stop = await self._start()
        if stop is None:
            stop = await self._loop()
        return await self._end(stop, started)

    async def resume(self, checkpoint: Checkpoint) -> RunRecord:
        """Carry on from a checkpoint in a store that already holds the world up to it (a fork)."""
        started = time.monotonic()
        if self._clock.now() != checkpoint.now or self._clock.wake() != checkpoint.wake:
            raise RunRefused(
                f"the clock is at {self._clock.now()} wake {self._clock.wake()}; "
                f"the checkpoint is at {checkpoint.now} wake {checkpoint.wake}"
            )
        self._begin()
        self._replies = self._store.replies()
        self._withdrawn = list(checkpoint.withdrawn)
        if len(self._replies) < checkpoint.replies:
            raise RunRefused(
                f"the checkpoint counts {checkpoint.replies} replies; the store holds {len(self._replies)}"
            )
        self._pending = list(checkpoint.pending)
        self._fated = list(checkpoint.fated)
        self._commitments = checkpoint.commitments
        self._agent_state = checkpoint.agent
        if isinstance(checkpoint.agent, Restorable):
            self._last_report = checkpoint.agent.report
        self._seen = self._store.head()
        stop = await self._start() if checkpoint.wake == 0 else None
        if stop is None:
            stop = await self._loop()
        return await self._end(stop, started)

    def mount(self) -> None:
        """Serve the run's providers to the agent, over this run's world, from now on: before the agent is
        restored for a fork, so whatever it calls as it starts is recorded in the fork and not its parent."""
        if self._mounts is not None and not self._mounted:
            self._mounts.mount(
                self._store,
                self._clock,
                {
                    provider.manifest.key: provider.app(self._store, self._clock)
                    for provider in self._services.providers
                },
                scenario=self._scenario,
            )
        self._mounted = True

    def _begin(self) -> None:
        if self._telemetry is not None:
            self._telemetry.run_started(self._store.run_id, self._scenario)
        self.mount()
        self._calls_seen = len(self._store.calls())
        for key, scheduler in self._services.schedulers.items():
            scheduler.bind(_Bookings(self, key))

    async def _end(self, stop: StopReason, started: float) -> RunRecord:
        record = RunRecord(
            run_id=self._store.run_id,
            scenario=self._scenario.name,
            seed=self._scenario.seed,
            parent_run=self._parent_run,
            forked_at=self._forked_at,
            started_at=self._scenario.starts_at,
            ended_at=self._clock.now(),
            wall_seconds=time.monotonic() - started,
            stop=stop,
            failure=self._failure,
            providers=list(dict.fromkeys(c.provider for c in self._store.calls() if c.provider is not None)),
            outbound=outbound_uses(self._store.calls()),
            wakes=self._wakes,
        )
        result = await self._scorer.score(record, self._store) if self._scorer is not None else None
        if self._telemetry is not None:
            for finding in result.findings if result is not None else []:
                self._telemetry.found(finding)
            self._telemetry.run_ended(record, result.effectiveness if result is not None else None)
        return record

    @property
    def _by_message(self) -> ProviderKey | None:
        goal = self._agent.goal
        return goal.provider if isinstance(goal, GoalByMessage) else None

    async def _start(self) -> StopReason | None:
        """The START wake. By message, the owner sends the goal first and a wake endpoint, if any, hears no goal."""
        wake = self._clock.begin_wake()
        provider = self._by_message
        request = WakeRequest(
            run_id=self._store.run_id,
            now=self._clock.now(),
            reason=WakeReason.START,
            goal=self._scenario.goal if provider is None else None,
        )
        driver = self._reach.for_reason(WakeReason.START)

        async def say_goal() -> None:
            if provider is not None:
                await self._say(provider, self._scenario.goal)

        requests = [(driver, request)] if driver is not None else []
        return await self._wake(wake, WakeReason.START, say_goal, requests, [d for d, _ in requests])

    async def _loop(self) -> StopReason:
        deadline = self._scenario.deadline
        while True:
            jump = next_jump(self._clock.now(), [p.due for p in self._pending])
            if jump is None:
                self._run_on_to(deadline)
                return StopReason.NOTHING_PENDING
            if deadline is not None and jump.now > deadline:
                self._run_on_to(deadline)
                return StopReason.DEADLINE_PASSED
            fired = [p for p in self._pending if p.due in jump.firing]
            self._pending = [p for p in self._pending if p.due not in jump.firing]
            self._clock.jump(jump.now)
            if all(isinstance(p, PendingFate) or self._unheard(p) for p in fired):
                await self._fire(fired)
                watched = self._watched(fired)
                if not watched:
                    continue
                stop = await self._wake(
                    self._clock.begin_wake(),
                    WakeReason.DUE,
                    lambda watched=watched: self._notify(watched),
                    [],
                    [self._reach.main] if self._reach.main is not None else [],
                )
                if stop is not None:
                    return stop
                continue
            wake = self._clock.begin_wake()
            requests, reason = self._requests(fired)
            settle = [d for d, _ in requests] or self._delivered_to(fired)

            async def fire(due: list[Pending] = fired) -> None:
                await self._fire(due)
                await self._notify(self._watched(due))

            stop = await self._wake(wake, reason, fire, requests, settle)
            if stop is not None:
                return stop

    def _run_on_to(self, deadline: datetime | None) -> None:
        """The world does not stop when the agent goes quiet: with nothing more due before it, the clock runs on
        to the scenario's deadline, and a checkpoint there records the moment the run reached. Without it a run
        would end where the agent stopped, and a wait it abandoned would never be seen to expire."""
        if deadline is None or self._clock.now() >= deadline:
            return
        self._clock.jump(deadline)
        write_checkpoint(
            self._store,
            Checkpoint(
                wake=self._clock.wake(),
                now=self._clock.now(),
                replies=len(self._replies),
                withdrawn=self._withdrawn,
                fated=self._fated,
                commitments=self._commitments,
                pending=self._pending,
                agent=self._agent_state,
            ),
        )
        self._record_new()

    async def _fire(self, fired: list[Pending]) -> None:
        """Change the world for what is due, in a fixed order: tickets, then replies (a message written back, or a
        control used), then what people do unprompted, then directions sent by message, then bookings."""
        for item in fired:
            if isinstance(item, PendingFate):
                self._tickets(item.ticket.provider).transition(item.ticket, item.becomes, self._store, self._clock)
        for item in fired:
            if isinstance(item, PendingReply):
                reply = self._replies[item.reply]
                provider = reply.in_reply_to.provider
                if provider in self._channels:
                    await self._channels[provider].deliver(reply, self._store, self._clock)
                elif reply.press is not None:
                    await self._interactions(provider).press(
                        reply, self._inbound(provider), self._store, self._clock, secret=self._secret(provider)
                    )
                else:
                    await self._pushes(provider).deliver(
                        reply, self._inbound(provider), self._store, self._clock, secret=self._secret(provider)
                    )
        for item in fired:
            if isinstance(item, PendingHappening):
                await self._happen(self._scenario.happenings[item.happening])
        by_message = self._by_message
        for item in fired:
            if isinstance(item, PendingDirection) and by_message is not None:
                await self._say(by_message, item.text)
        for item in fired:
            if isinstance(item, PendingBooking):
                await self._services.schedulers[item.provider].fire(item.ref, self._store, self._clock)
        for item in fired:
            if isinstance(item, PendingWake) and item.reason is WakeReason.TICK:
                self._schedule_tick()

    async def _happen(self, happening: Happening) -> None:
        """What a person does by themselves lands through the port its family has: a ticket happening through the
        ticket provider's `ActsOnTickets`, a document happening through the document provider's `ChangesDocuments`,
        a messaging happening pushed through `PushesEvents`."""
        if isinstance(happening, TicketHappening):
            provider = self._scenario.happening_ticket(happening).provider
            self._acts(provider).act(happening, self._scenario, self._store, self._clock)
            return
        if isinstance(happening, DocumentHappening):
            provider = self._scenario.happening_document(happening).provider
            self._changes(provider).change(happening, self._scenario, self._store, self._clock)
            return
        await self._pushes(happening.provider).happen(
            happening,
            self._inbound(happening.provider),
            self._store,
            self._clock,
            secret=self._secret(happening.provider),
        )

    def _unheard(self, pending: Pending) -> bool:
        """A pending happening the agent is not told of as it lands: one on a ticket or a document, which the agent
        finds on its next read (a document's provider may then tell a watching agent, `_watched`). A messaging
        happening is pushed to the agent, and that push is a wake, as a reply's is."""
        return isinstance(pending, PendingHappening) and isinstance(
            self._scenario.happenings[pending.happening], TicketHappening | DocumentHappening
        )

    def _delivered_to(self, fired: list[Pending]) -> list[AgentDriver]:
        """A wake made only of bookings sends no request, since the scheduler's delivery is the wake, but the agent
        still acts on what was delivered: the loop waits on its main driver until it is no longer working."""
        if self._reach.main is None:
            return []
        if not any(isinstance(p, PendingBooking) for p in fired) and not self._watched(fired):
            return []
        return [self._reach.main]

    def _requests(self, fired: list[Pending]) -> tuple[list[tuple[AgentDriver, WakeRequest]], WakeReason]:
        """One request per driver that must hear of this wake, and the reason the wake carries. A wake made only
        of bookings sends none: the scheduler's own delivery is the wake. Nor does one that reaches an agent
        with no wake endpoint: the pushed event is the wake. A direction sent by message is not repeated in
        the request."""
        reasons: set[WakeReason] = set()
        directions: list[str] = []
        for item in fired:
            if isinstance(item, PendingReply) or (isinstance(item, PendingHappening) and not self._unheard(item)):
                reasons.add(WakeReason.PERSON_REPLIED)
            elif isinstance(item, PendingDirection):
                reasons.add(WakeReason.DIRECTION)
                if self._by_message is None:
                    directions.append(item.text)
            elif isinstance(item, PendingWake):
                reasons.add(item.reason)
        if not reasons:
            return [], WakeReason.DUE
        reason = next(r for r in _PRIORITY if r in reasons)
        now = self._clock.now()
        run_id = self._store.run_id
        requests: list[tuple[AgentDriver, WakeRequest]] = []
        main = self._reach.for_reason(reason)
        if main is not None:
            requests.append(
                (
                    main,
                    WakeRequest(
                        run_id=run_id, now=now, reason=reason, direction="\n\n".join(directions) if directions else None
                    ),
                )
            )
        if WakeReason.TICK in reasons and reason is not WakeReason.TICK:
            ticks = self._reach.for_reason(WakeReason.TICK)
            if ticks is not None and ticks is not main:
                requests.append((ticks, WakeRequest(run_id=run_id, now=now, reason=WakeReason.TICK)))
        return requests, reason

    async def _wake(
        self,
        wake: int,
        reason: WakeReason,
        fire: Callable[[], Awaitable[None]],
        requests: list[tuple[AgentDriver, WakeRequest]],
        settle: list[AgentDriver],
    ) -> StopReason | None:
        """Change the world for what is due, send the wake, wait until each driver in `settle` stops working, read
        what the agent did, and checkpoint. An agent that refuses a pushed event fails the wake as one that refuses
        the wake does. The store keeps the real moments the wake began and, after its checkpoint, ended: the
        agent's spans are placed in the wake whose window holds their start, however late they arrive."""
        self._store.wake_began(wake)
        try:
            return await self._played(wake, reason, fire, requests, settle)
        finally:
            self._store.wake_ended(wake)

    async def _played(
        self,
        wake: int,
        reason: WakeReason,
        fire: Callable[[], Awaitable[None]],
        requests: list[tuple[AgentDriver, WakeRequest]],
        settle: list[AgentDriver],
    ) -> StopReason | None:
        if self._telemetry is not None:
            self._telemetry.wake_started(wake, reason, self._clock.now())
        failed = False
        done = False
        commitments_changed = False
        try:
            await fire()
            for driver, request in requests:
                await driver.wake(request)
            for driver in settle:
                report = await driver.settled()
                done = done or report.status is AgentStatus.DONE
                if driver is self._reach.main:
                    commitments_changed = self._adopt(report)
        except AgentFailed as e:
            failed = True
            self._failure = str(e)
        settled = None if failed else await self._settled(wake)
        if isinstance(settled, Settled) and settled.report is not None and isinstance(self._reach.main, Reports):
            # The wake is over when the agent's background work is: what it reports now replaces what it said when
            # its driver first answered, and what it wrote meanwhile belongs to this wake.
            commitments_changed = self._adopt(settled.report) or commitments_changed
            done = done or settled.report.status is AgentStatus.DONE
        new = self._record_new()
        if not failed:
            await self._schedule(new)
        self._wakes.append(
            WakeRecord(
                index=wake,
                sim_time=self._clock.now(),
                world_changes=sum(
                    1 for e in new if e.wake == wake and e.actor is Actor.AGENT and e.operation not in _NOT_CHANGES
                ),
                commitments_changed=commitments_changed,
            )
        )
        if self._telemetry is not None:
            self._telemetry.wake_ended(wake)
        if failed:
            return StopReason.AGENT_FAILED
        try:
            await self._checkpoint(settled)
        except AgentFailed as e:
            self._failure = str(e)
            return StopReason.AGENT_FAILED
        if done:
            return StopReason.AGENT_DONE
        if len(self._wakes) >= self._scenario.max_wakes:
            return StopReason.WAKE_LIMIT
        return None

    def _adopt(self, report: AgentReport) -> bool:
        """Take the agent's next wake, replacing the one it named before. Answers whether its commitments changed."""
        self._pending = [p for p in self._pending if not (isinstance(p, PendingWake) and p.reason is WakeReason.DUE)]
        if report.next_wake is not None:
            self._pending.append(
                PendingWake(
                    due=Due(at=report.next_wake, kind=DueKind.AGENT_WAKE, ref="next_wake"),
                    reason=WakeReason.DUE,
                )
            )
        changed = report.commitments != self._commitments
        self._commitments = report.commitments
        self._last_report = report
        return changed

    def _schedule_tick(self) -> None:
        assert self._reach.every is not None
        self._pending.append(
            PendingWake(
                due=Due(at=self._clock.now() + self._reach.every, kind=DueKind.AGENT_WAKE, ref="tick"),
                reason=WakeReason.TICK,
            )
        )

    def _record_new(self) -> list[WorldEvent]:
        new = self._store.events(since=self._seen)
        if new:
            self._seen = new[-1].seq
        if self._telemetry is not None:
            for event in new:
                self._telemetry.recorded(event)
            calls = self._store.calls()
            for call in calls[self._calls_seen :]:
                if call.exchange.captured is not None:
                    self._telemetry.captured(call)
            self._calls_seen = len(calls)
        return new

    async def _schedule(self, new: list[WorldEvent]) -> None:
        """What the world owes back for what the agent just did: replies to its messages, fates of its tickets.

        A person answers a message as it reads when the wake ends: an agent that posts a placeholder and edits
        it into its question in the same wake is answered about the question, once, and never about the
        placeholder. An edit in a later wake that changes the text is put to the person again unless they
        have already answered that message: a reply still on its way to the edited message is withdrawn and
        decided afresh on the new text."""
        history: list[WorldEvent] | None = None
        shown: dict[EntityRef, WorldEvent] = {}
        for event in new:
            if event.actor is not Actor.AGENT:
                continue
            after = event.after
            if event.operation in (Operation.CREATE, Operation.UPDATE) and isinstance(after, MessageSnapshot):
                if history is None:
                    history = self._store.events()
                if event.operation is Operation.CREATE or _text_changed(event, history):
                    shown[event.entity] = event
            if (
                event.operation in (Operation.CREATE, Operation.UPDATE)
                and isinstance(after, TicketSnapshot)
                and after.assignee_email in self._people
                and event.entity not in self._fated
            ):
                self._fate(self._people[after.assignee_email], event)
        for event in sorted(shown.values(), key=lambda e: e.seq):
            assert history is not None and isinstance(event.after, MessageSnapshot)
            if not event.after.answerable:
                continue  # a captured send: nobody can answer where it went
            for email in event.after.recipient_emails:
                if email not in self._people:
                    continue
                person = self._people[email]
                if event.operation is Operation.UPDATE and not self._withdraw(event.entity, person):
                    continue
                await self._ask(person, event, [h for h in history if h.seq <= event.seq])

    def _withdraw(self, message: EntityRef, person: Person) -> bool:
        """Before an edited message is put to `person` again: withdraw their reply to it that has not landed yet,
        and record it as withdrawn, so it settles nothing. False when they have already answered it, and the edit
        is not put to them."""
        mine = [
            i
            for i, r in enumerate(self._replies)
            if r.in_reply_to == message and r.person == person.key and i not in self._withdrawn
        ]
        waiting = {p.reply for p in self._pending if isinstance(p, PendingReply)}
        if any(i not in waiting for i in mine):
            return False
        self._pending = [p for p in self._pending if not (isinstance(p, PendingReply) and p.reply in mine)]
        self._withdrawn += mine
        return True

    async def _say(self, provider: ProviderKey, text: str) -> None:
        """The scenario's owner messages the agent: its goal, or a direction."""
        message = PersonMessage(person=self._scenario.owner, text=text, at=self._clock.now())
        await self._pushes(provider).say(
            message, self._inbound(provider), self._store, self._clock, secret=self._secret(provider)
        )

    async def _ask(self, person: Person, asked: WorldEvent, history: list[WorldEvent]) -> None:
        reply = await self._replier.decide(person, asked, history, self._clock)
        if reply is None:
            return
        if reply.in_reply_to.provider not in self._channels:
            self._pushes(reply.in_reply_to.provider)
            if reply.press is not None:
                self._interactions(reply.in_reply_to.provider)
            self._inbound(reply.in_reply_to.provider)
        self._store.remember(reply)
        position = len(self._replies)
        self._replies.append(reply)
        self._pending.append(
            PendingReply(
                due=Due(at=reply.at, kind=DueKind.PERSON_REPLY, ref=f"reply:{position}"),
                reply=position,
            )
        )

    def _fate(self, person: Person, assigned: WorldEvent) -> None:
        fate = next((f for f in self._scenario.ticket_fates if f.assignee == person.key), None)
        if fate is None:
            return
        self._tickets(assigned.entity.provider)
        self._fated.append(assigned.entity)
        ticket = assigned.entity
        self._pending.append(
            PendingFate(
                due=Due(
                    at=assigned.sim_time + fate.after,
                    kind=DueKind.TICKET_FATE,
                    ref=f"fate:{ticket.provider}:{ticket.external_id}",
                ),
                ticket=ticket,
                becomes=fate.becomes,
            )
        )

    async def _settled(self, wake: int) -> Settled | NotRestorable | None:
        """Wait for the agent's background work to end, as a checkpoint does: None for an agent with no hooks."""
        hooks = self._agent.state
        if hooks is None:
            return None
        assert self._state_dir is not None and self._traffic is not None
        main = self._reach.main
        return await settle(
            hooks,
            self._traffic,
            main if isinstance(main, Reports) else None,
            self._last_report,
            directory=wake_dir(self._state_dir, self._store.run_id, wake),
        )

    async def _checkpoint(self, settled: Settled | NotRestorable | None = None) -> None:
        """`settled` is how the wake just played settled, when it has; otherwise the checkpoint settles it."""
        wake = self._clock.wake()
        self._agent_state = await self._snapshot(wake, settled)
        write_checkpoint(
            self._store,
            Checkpoint(
                wake=wake,
                now=self._clock.now(),
                replies=len(self._replies),
                withdrawn=self._withdrawn,
                fated=self._fated,
                commitments=self._commitments,
                pending=self._pending,
                agent=self._agent_state,
            ),
        )
        self._record_new()

    async def _snapshot(self, wake: int, settled: Settled | NotRestorable | None) -> AgentState:
        """The agent's own state at the end of this wake: snapshotted once it has settled, or recorded as not
        restorable, with the reason, when it did not settle in time. A snapshot command that fails raises."""
        hooks = self._agent.state
        if hooks is None:
            return NoHooks()
        assert self._state_dir is not None
        directory = wake_dir(self._state_dir, self._store.run_id, wake)
        if settled is None:
            settled = await self._settled(wake)
        assert settled is not None
        if isinstance(settled, NotRestorable):
            return settled
        fingerprint: str | None = None
        if hooks.fingerprint is not None:
            printed = await run_command(
                RestoreStep.FINGERPRINT, hooks.fingerprint, directory, hooks.step_limit.total_seconds()
            )
            if printed.exit_code != 0:
                return NotRestorable(
                    reason=f"the agent's fingerprint command failed, so a restore here could not be verified: "
                    f"{' '.join(printed.command)} exited {printed.exit_code}: {printed.output.strip()[-500:]}"
                )
            fingerprint = digest_of(printed.output)
        await take_snapshot(hooks, self._store, self._state_dir, wake)
        return Restorable(
            snapshot_of=self._store.run_id,
            wake=wake,
            report=settled.report,
            fingerprint=fingerprint,
            unconfirmed=settled.unconfirmed,
        )

    # -- lookups that refuse loudly -------------------------------------------------------------------------

    def _pushes(self, provider: ProviderKey) -> PushesEvents:
        if provider not in self._services.pushes:
            raise RunRefused(f"a person owes a reply on {provider}, which pushes no events to the agent")
        return self._services.pushes[provider]

    def _interactions(self, provider: ProviderKey) -> PushesInteractions:
        pushes = self._pushes(provider)
        if not isinstance(pushes, PushesInteractions):
            raise RunRefused(f"a person uses a control on a {provider} message, and {provider} carries no controls")
        return pushes

    def _inbound(self, provider: ProviderKey) -> InboundTarget:
        target = next((t for t in self._agent.inbound if t.provider == provider), None)
        if target is None:
            raise RunRefused(f"agent {self._agent.name} declares no inbound target for {provider}")
        return target

    def _secret(self, provider: ProviderKey) -> str:
        if provider not in self._signing:
            raise RunRefused(f"no signing secret was resolved for the agent's inbound target on {provider}")
        return self._signing[provider]

    def _changes(self, provider: ProviderKey) -> ChangesDocuments:
        found = next((p for p in self._services.providers if p.manifest.key == provider), None)
        if not isinstance(found, ChangesDocuments):
            raise RunRefused(f"a happening changes a seeded {provider} document, and {provider} cannot change one")
        return found

    def _watched(self, fired: list[Pending]) -> list[NotifiesChanges]:
        """The providers a document happening just landed in whose agent asked to be told of changes: telling it is
        a wake, as a pushed event is. A change nobody watches is found on the agent's next read, as a ticket's is."""
        happenings = [self._scenario.happenings[p.happening] for p in fired if isinstance(p, PendingHappening)]
        providers = {self._scenario.happening_provider(h) for h in happenings if isinstance(h, DocumentHappening)}
        found = [p for p in self._services.providers if p.manifest.key in providers]
        return [p for p in found if isinstance(p, NotifiesChanges) and p.watched(self._store, self._clock)]

    async def _notify(self, watched: list[NotifiesChanges]) -> None:
        for changer in watched:
            await changer.notify(self._store, self._clock)

    def _tickets(self, provider: ProviderKey) -> HoldsTickets:
        if provider not in self._services.tickets:
            raise RunRefused(f"a ticket fate is due on {provider}, which holds no tickets a person can move")
        return self._services.tickets[provider]

    def _acts(self, provider: ProviderKey) -> ActsOnTickets:
        found = next((p for p in self._services.providers if p.manifest.key == provider), None)
        if not isinstance(found, ActsOnTickets):
            raise RunRefused(f"a happening acts on a seeded {provider} ticket, and {provider} cannot act on one")
        return found


def _refuse_unlanded_happenings(scenario: Scenario, agent: AgentUnderTest, services: Services) -> None:
    """Every happening lands through a port its provider has, checked before anything is seeded: a ticket happening
    needs the ticket's provider in the run implementing `ActsOnTickets`, a document happening the document's provider
    implementing `ChangesDocuments`; a messaging happening needs its provider
    pushing events and the agent declaring an inbound target for it."""
    for n, happening in enumerate(scenario.happenings, start=1):
        provider = scenario.happening_provider(happening)
        found = next((p for p in services.providers if p.manifest.key == provider), None)
        if isinstance(happening, TicketHappening):
            if isinstance(found, ActsOnTickets):
                continue
            why = "is not in this run" if found is None else "has no tickets a person can act on"
            what = f"{happening.person} {happening.action.kind} the seeded ticket {happening.ticket!r}"
        elif isinstance(happening, DocumentHappening):
            if isinstance(found, ChangesDocuments):
                continue
            why = "is not in this run" if found is None else "has no documents a person can change"
            what = f"{happening.person} {happening.action.kind} the seeded document {happening.document!r}"
        else:
            if provider not in services.pushes:
                why = "pushes no events to the agent"
            elif not any(t.provider == provider for t in agent.inbound):
                why = f"is no inbound target of agent {agent.name}"
            else:
                continue
            what = f"{happening.person} {happening.kind}"
        raise RunRefused(f"happening {n} ({what}) lands on {provider}, which {why}")


def _text_changed(edit: WorldEvent, history: list[WorldEvent]) -> bool:
    """Whether an edit changed what the message says or what a reader can press on it, against its version before
    the edit: a card whose buttons appear in an edit asks something new."""
    assert isinstance(edit.after, MessageSnapshot)
    before = next(
        (
            (e.after.text, e.after.actions)
            for e in reversed(history)
            if e.seq < edit.seq and e.entity == edit.entity and isinstance(e.after, MessageSnapshot)
        ),
        None,
    )
    return before != (edit.after.text, edit.after.actions)


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
    scorer: Scorer | None = None,
    state_dir: Path | None = None,
    signing: Mapping[ProviderKey, str] | None = None,
    traffic: Traffic | None = None,
    channels: Mapping[ProviderKey, TakesReplies] | None = None,
) -> RunRecord:
    """Run one scenario from its start. `signing` holds the secret each provider signs its pushed events with;
    `traffic` sees the agent's outbound calls, which an agent with `StateHooks` needs to settle a checkpoint;
    `channels` delivers people's answers to the agent's captured sends."""
    if clock.now() != scenario.starts_at or clock.wake() != 0:
        raise RunRefused(f"the clock must start at the scenario's start ({scenario.starts_at}), wake 0")
    return await Orchestrator(
        scenario=scenario,
        agent=agent,
        reach=reach,
        store=store,
        clock=clock,
        services=services,
        replier=replier,
        telemetry=telemetry,
        mounts=mounts,
        scorer=scorer,
        state_dir=state_dir,
        signing=signing,
        traffic=traffic,
        channels=channels,
    ).run()
