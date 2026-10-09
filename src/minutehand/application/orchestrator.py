"""The run loop: one scenario, one agent, a set of providers, played forward on the run's clock.

Flow of one wake (docs/design.md): jump the clock to the earliest pending moment, fire what is due there,
wake the agent if any of it reaches the agent, poll it until it is no longer working, then read what it did
from the world and schedule what the world owes back: what people do about what waits on them
(`application.people`), and tickets' fates.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from minutehand.application import memory
from minutehand.application.checkpoint import (
    Checkpoint,
    Pending,
    PendingBooking,
    PendingCall,
    PendingDirection,
    PendingHappening,
    PendingMachine,
    PendingService,
    PendingTimer,
    PendingTransition,
    PendingWake,
    Remembered,
    write_checkpoint,
)
from minutehand.application.dues import Dues
from minutehand.application.inboxes import Inboxes, refuse_clashing, refuse_untakeable
from minutehand.application.machine import record_machine, run_machine
from minutehand.application.outbound import emulator_uses, outbound_uses
from minutehand.application.people import Booking, People, Ready
from minutehand.application.refusals import AgentFailed, RunRefused
from minutehand.application.run_clock import RunClock
from minutehand.application.sandbox import SandboxClock
from minutehand.application.services import ServiceDesk
from minutehand.application.traffic import Traffic
from minutehand.application.watching import Watcher
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
from minutehand.domain.experiment import ReplyAt
from minutehand.domain.people import InboundTarget, PersonMessage
from minutehand.domain.run import RunRecord, StopReason, wake_limit
from minutehand.domain.scenario import DocumentHappening, Happening, ProviderKey, Scenario, TicketHappening
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    NextWakeSnapshot,
    Operation,
    WorldEvent,
)
from minutehand.ports.agent import AgentDriver, TakesReplies
from minutehand.ports.clock import Clock
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.people import Replier
from minutehand.ports.provider import (
    ASGIApp,
    BooksWakes,
    ChangesDocuments,
    ConfirmsDelivery,
    HeldCalls,
    ListensForAgent,
    NotifiesChanges,
    Provider,
    PushesEvents,
)
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry
from minutehand.ports.transitions import HoldsSeeded, TalksToAgent

_NOT_CHANGES = frozenset({Operation.READ, Operation.SEARCH})
_NOT_THE_WORLD = frozenset({EntityKind.MEMORY, EntityKind.NEXT_WAKE})
"""The agent's own memory and plan: its writes, but not changes to the world a person could see."""


class OutsideState(Protocol):
    """State the agent keeps outside its memory that the run can see (`AgentUnderTest.own_databases`)."""

    def outside(self) -> list[str]:
        """What is there now, one line each, for a person; empty when nothing is."""
        ...


TAKEN_EVERY = 0.05
"""Seconds between two asks whether the agent has taken a booking's delivery."""

_PRIORITY = [WakeReason.PERSON_REPLIED, WakeReason.DIRECTION, WakeReason.DUE, WakeReason.TICK]
"""When one jump fires several things, the wake carries the reason that matters most to the agent."""


class Mounts(Protocol):
    """Whoever serves the providers' APIs to the agent (the proxy). Told once per run, since a fork is a new world:
    from then on the calls it answers are recorded in `world`, and each provider in `apps` answers its hosts. A
    provider the agent calls that is not in `apps` is seeded with `scenario` on its first call."""

    def mount(
        self,
        world: Store,
        clock: Clock,
        apps: Mapping[ProviderKey, ASGIApp],
        *,
        scenario: Scenario,
        holds: HeldCalls | None = None,
    ) -> None:
        """`holds` takes a call that waits on the world (`ports.provider.HeldCalls`) into the run's table; with
        none, such a call is refused, since nothing would move the clock it waits on."""
        ...

    def flush(self) -> None:
        """Record every call still in progress as far as it has gone (a burst on a tunnel it relays unopened, kept
        once it falls quiet): the run is about to be summarised."""
        ...


@runtime_checkable
class HoldsMemory(Protocol):
    """Mounts that also answer the agent's memory (`minutehand.agent.store`): the receiver beside the proxy."""

    def memory_reads(self, wake: int) -> int:
        """The agent's gets and listings of its memory in `wake`, every one, though the log keeps only those that
        could find something new (`application.memory.Reads`)."""
        ...


class Environment(Protocol):
    """What the run stands on besides the agent and Minutehand's own fakes: the external emulators it forwards to.
    Asked after every wake, and when the run ends, whether any has failed under it."""

    def failure(self) -> str | None: ...


class Scorer(Protocol):
    """Whoever judges a finished run (the checks). Its findings and scorecard end the run's telemetry.
    Async, because a judged check waits on a model."""

    async def score(self, record: RunRecord, world: Store) -> RunResult: ...


@dataclass(frozen=True)
class Services:
    """The providers in a run, and which of the optional ports each one also implements."""

    providers: Sequence[Provider]
    pushes: Mapping[ProviderKey, PushesEvents] = field(default_factory=dict)
    schedulers: Mapping[ProviderKey, BooksWakes] = field(default_factory=dict)

    def __post_init__(self) -> None:
        keys = [p.manifest.key for p in self.providers]
        if len(keys) != len(set(keys)):
            raise RunRefused(f"two providers share a key: {sorted(keys)}")
        known = set(keys)
        for role, mapping in (
            ("pushes", self.pushes),
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
            if provider.manifest.books_wakes != (key in self.schedulers) and not provider.manifest.books_work:
                raise RunRefused(
                    f"provider {key}: manifest books_wakes={provider.manifest.books_wakes} "
                    f"but it is{'' if key in self.schedulers else ' not'} given as BooksWakes"
                )
            if key in self.schedulers and not (provider.manifest.books_wakes or provider.manifest.books_work):
                raise RunRefused(f"provider {key} books neither wakes nor work, and is given as BooksWakes")


@dataclass(frozen=True)
class Reach:
    """How the loop reaches the agent: `main` answers every wake but ticks; `ticks` answers a `Polled` rhythm.

    Built from `AgentUnderTest.wakes` by `adapters.agent.reach_for`. Neither is set for an agent reached only
    through pushed events: its goal comes by message (`GoalByMessage`) and acknowledging a push ends its wake.
    """

    main: AgentDriver | None
    ticks: AgentDriver | None = None
    every: timedelta | None = None
    sandbox: SandboxClock | None = None
    """The sandbox whose clock Minutehand owns (`Contained`): moved with every jump, its timers read as wakes."""

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


class _Holding:
    """`ports.provider.HeldCalls` for the run: each call held until the world can answer it is an entry of the run's
    table (`PendingCall`) at its moment, and how to look at it again."""

    def __init__(self, dues: Dues, clock: Clock) -> None:
        self._dues = dues
        self._clock = clock
        self.looks: dict[str, Callable[[bool], Awaitable[bool]]] = {}
        self._settled: Callable[[], Awaitable[None]] | None = None

    def answering(self, settled: Callable[[], Awaitable[None]]) -> None:
        self._settled = settled

    async def settled(self) -> None:
        """Every call that has reached the world is answered or held."""
        if self._settled is not None:
            await self._settled()

    def hold(self, ref: str, at: datetime, *, ends: bool, look: Callable[[bool], Awaitable[bool]]) -> None:
        self.looks[ref] = look
        due = Due(at=max(at, self._clock.now()), kind=DueKind.CALL, ref=ref)
        self._dues.replace(lambda p: _held(p, ref), PendingCall(due=due, ends=ends))

    def answered(self, ref: str) -> None:
        self.looks.pop(ref, None)
        self._dues.cancel(lambda p: _held(p, ref))

    async def look(self, *, over: bool = False) -> None:
        """Look at every call held, on the world as it now stands: each is answered, or held to its next moment.
        `over`: answer each as the world stands, its wait cut short by the run's end."""
        for ref in list(self.looks):
            if ref in self.looks:
                await self.looks[ref](over)


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
        signing: Mapping[ProviderKey, str] | None = None,
        parent_run: str | None = None,
        forked_at: int | None = None,
        prior_wakes: Sequence[WakeRecord] = (),
        traffic: Traffic | None = None,
        channels: Mapping[ProviderKey, TakesReplies] | None = None,
        environment: Environment | None = None,
        inboxes: Inboxes | None = None,
        outside: OutsideState | None = None,
        model: LanguageModel | None = None,
        desk: ServiceDesk | None = None,
        pins: Sequence[ReplyAt] = (),
    ) -> None:
        if scenario.services and desk is None:
            raise RunRefused("the scenario declares services, and the run was given no desk to answer them")
        if inboxes is not None:
            reaches = list(inboxes.reaches.values())
            refuse_clashing(reaches, [*(p.manifest.key for p in services.providers), *(channels or {})])
            refuse_untakeable(scenario, reaches)
        if any(isinstance(w, Booked) for w in agent.wakes) and not any(
            p.manifest.books_wakes for p in services.providers
        ):
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
        self._limit = wake_limit(scenario, agent)
        self._reach = reach
        self._store = store
        self._clock = clock
        self._services = services
        self._replier = replier
        self._telemetry = telemetry
        self._mounts = mounts
        self._scorer = scorer
        self._signing = dict(signing or {})
        self._parent_run = parent_run
        self._forked_at = forked_at
        self._traffic = traffic
        self._channels = dict(channels or {})
        self._environment = environment
        self._inboxes = inboxes
        self._outside = outside
        self._desk = desk
        self._engine = People(
            scenario,
            transition_ports(services, agent, signing or {}, channels or {}, inboxes, desk, scenario),
            replier,
            model,
            pins,
        )
        self._untaken: list[EntityRef] = []
        self._retried: list[EntityRef] = []
        self._mounted = False
        self._last_report: AgentReport | None = None
        self._wakes: list[WakeRecord] = list(prior_wakes)
        self._dues = Dues(store, clock, scenario.dispatch)
        self._holding = _Holding(self._dues, clock)
        self._machine_failed: str | None = None
        self._sandbox_owed = 0
        self._quiet_tasks: dict[int, int] = {}
        """A task's housekeeping period: its timer fired at this distance and did nothing."""
        self._timer_tasks: set[int] = set()
        self._timer_ns = -1
        self._watcher = Watcher(agent.watches)
        self._commitments: list[Commitment] | None = None
        self._failure: str | None = None
        self._seen = 0
        self._calls_seen = 0
        self._people = {p.email: p for p in scenario.people}

    @property
    def people(self) -> People:
        """The run's people engine."""
        return self._engine

    # -- Wakes, for scheduler providers ---------------------------------------------------------------------

    def book(self, provider: ProviderKey, due: Due) -> None:
        self._dues.replace(
            lambda p: isinstance(p, PendingBooking) and p.provider == provider and p.ref == due.ref,
            PendingBooking(
                due=Due(at=due.at, kind=DueKind.AGENT_WAKE, ref=f"booking:{provider}:{due.ref}"),
                provider=provider,
                ref=due.ref,
            ),
        )

    def cancel(self, provider: ProviderKey, ref: str) -> None:
        self._dues.cancel(lambda p: isinstance(p, PendingBooking) and p.provider == provider and p.ref == ref)

    # -- the run ------------------------------------------------------------------------------------------------

    async def run(self) -> RunRecord:
        """A fresh run: seed the world, checkpoint the setup, send START, and play forward."""
        started = time.monotonic()
        self._begin()
        for provider in self._services.providers:
            provider.seed(self._scenario, self._store)
        if self._scenario.memory:
            memory.seed(self._store, self._scenario.memory)
        for i, direction in enumerate(self._scenario.directions):
            self._dues.enter(
                PendingDirection(
                    due=Due(
                        at=self._scenario.starts_at + direction.after, kind=DueKind.DIRECTION, ref=f"direction:{i}"
                    ),
                    text=direction.text,
                )
            )
        for i, command in enumerate(self._scenario.machine):
            self._dues.enter(
                PendingMachine(
                    due=Due(at=self._scenario.starts_at + command.after, kind=DueKind.MACHINE, ref=f"machine:{i}"),
                    command=i,
                )
            )
        for i, happening in enumerate(self._scenario.happenings):
            self._dues.enter(
                PendingHappening(
                    due=Due(
                        at=self._scenario.starts_at + happening.after, kind=DueKind.HAPPENING, ref=f"happening:{i}"
                    ),
                    happening=i,
                )
            )
        if self._reach.every is not None:
            self._schedule_tick()
        self._watcher.look()
        self._record_new()
        self._checkpoint()
        stop = await self._start()
        if stop is None:
            stop = await self._loop()
        return await self._end(stop, started)

    async def resume(self, checkpoint: Checkpoint, *, replan: AgentReport | None = None) -> RunRecord:
        """Carry on from a checkpoint in a store that already holds the world up to it (a fork). With `replan`, the
        agent's report asked again after the fork changed its memory: its own planned wakes (reported, its timer,
        bookings) are replaced by its next wake, recorded as REPLACED; what the scenario owes (replies, happenings,
        directions, fates, machine commands) and its declared rhythm stay as the checkpoint holds them."""
        started = time.monotonic()
        if self._clock.now() != checkpoint.now or self._clock.wake() != checkpoint.wake:
            raise RunRefused(
                f"the clock is at {self._clock.now()} wake {self._clock.wake()}; "
                f"the checkpoint is at {checkpoint.now} wake {checkpoint.wake}"
            )
        self._begin()
        held = len(self._store.replies())
        if held < checkpoint.replies:
            raise RunRefused(f"the checkpoint counts {checkpoint.replies} replies; the store holds {held}")
        # a held call is a connection of the process that played the parent: the fork's agent makes its own calls
        self._dues.resume([p for p in checkpoint.pending if not isinstance(p, PendingCall)])
        self._untaken = list(checkpoint.untaken)
        self._commitments = checkpoint.commitments
        self._last_report = checkpoint.agent.report
        if replan is not None:
            new = (
                PendingWake(
                    due=Due(at=replan.next_wake, kind=DueKind.AGENT_WAKE, ref="next_wake"), reason=WakeReason.DUE
                )
                if replan.next_wake is not None
                else None
            )
            self._dues.replace(_planned_by_agent, new)
            self._commitments = replan.commitments
            self._last_report = replan
        self._seen = self._store.head()
        self._watcher.look()
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
                holds=self._holding,
            )
        self._mounted = True

    def _begin(self) -> None:
        if self._telemetry is not None:
            self._telemetry.run_started(self._store.run_id, self._scenario)
        self.mount()
        for provider in self._services.providers:
            key = provider.manifest.key
            if isinstance(provider, ListensForAgent):
                target = next((t for t in self._agent.inbound if t.provider == key), None)
                provider.listen(target, self._signing[key] if key in self._signing else None)
        self._calls_seen = len(self._store.calls())
        for key, scheduler in self._services.schedulers.items():
            scheduler.bind(_Bookings(self, key))

    async def _end(self, stop: StopReason, started: float) -> RunRecord:
        await self._holding.look(over=True)  # a call still held is answered as the world stands at the run's end
        if self._mounts is not None:
            self._mounts.flush()
        failed = self._environment.failure() if self._environment is not None else None
        if failed is not None:
            # Whatever the agent did after its emulator failed under it, the run is the environment's failure.
            stop, self._failure = StopReason.ENVIRONMENT_FAILED, failed
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
            emulators=emulator_uses(self._store.calls()),
            wakes=self._wakes,
            wake_limit=self._limit,
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
            if provider is not None and self._scenario.goal is not None:
                await self._say(provider, self._scenario.goal)

        requests = [(driver, request)] if driver is not None else []
        return await self._wake(wake, WakeReason.START, say_goal, requests, [d for d, _ in requests])

    async def _loop(self) -> StopReason:
        deadline = self._scenario.window_end
        ended = StopReason.DEADLINE_PASSED if self._scenario.runs_for is None else StopReason.WINDOW_ENDED
        while True:
            await self._look()  # what waits on people now, before the clock moves past what they owe
            await self._schedule(self._record_new())
            if not await self._plan_timer():
                return StopReason.AGENT_FAILED
            await self._holding.settled()  # a call the agent made is held or answered at this moment, not the next
            items = self._dues.items
            # a call held to the end of its wait changes nothing by being answered empty: with nothing else due,
            # nothing more is
            waiting = any(not (isinstance(p, PendingCall) and p.ends) for p in items)
            jump = next_jump(self._clock.now(), [p.due for p in items]) if waiting else None
            if jump is None:
                await self._run_on_to(deadline)
                # a world-only run whose window was set watched to its end, however quiet the end was
                quiet_to_the_end = self._scenario.runs_for is not None and not self._scenario.is_task
                return StopReason.WINDOW_ENDED if quiet_to_the_end else StopReason.NOTHING_PENDING
            if deadline is not None and jump.now > deadline:
                await self._run_on_to(deadline)
                return ended
            await self._jump(jump.now)
            dispatched = self._dues.dispatch(jump.firing)
            for item in dispatched.withheld:
                if isinstance(item, PendingWake) and item.reason is WakeReason.TICK:
                    self._schedule_tick()  # the rhythm goes on from the tick the scheduler held back
            for item in dispatched.finished:
                # a dropped occurrence of a booking is over undelivered: the schedule books its next, or completes
                await self._services.schedulers[item.provider].advance_booking(item.ref, self._store, self._clock)
            fired = dispatched.delivered
            if not await self._machine([p for p in fired if isinstance(p, PendingMachine)]):
                self._failure = self._machine_failed
                return StopReason.ENVIRONMENT_FAILED
            fired = await self._ready([p for p in fired if not isinstance(p, PendingMachine)])
            if not fired:
                continue
            if all(self._unheard(p) for p in fired):
                await self._release()
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
            if all(isinstance(p, PendingTimer) for p in fired) and not await self._timer_did_something(wake):
                continue
            requests, reason = self._requests(fired)
            settle = [d for d, _ in requests] or self._delivered_to(fired)

            async def fire(due: list[Pending] = fired) -> None:
                await self._release()
                await self._fire(due)
                await self._notify(self._watched(due))
                await self._taken(due)

            stop = await self._wake(wake, reason, fire, requests, settle)
            if stop is not None:
                return stop

    async def _jump(self, to: datetime) -> None:
        """Move the run's clock. A contained agent's sandbox is owed the same step, released (`_release`) once the
        wake it may start has begun, so what the agent's timers do is that wake's."""
        was = self._clock.now()
        self._clock.jump(to)
        if self._reach.sandbox is not None and to > was:
            self._sandbox_owed += int((to - was).total_seconds() * 1e9)

    async def _release(self) -> None:
        """Move the sandbox's clock by what the run's has moved since, so the two agree again."""
        if self._reach.sandbox is not None and self._sandbox_owed > 0:
            owed, self._sandbox_owed = self._sandbox_owed, 0
            await self._reach.sandbox.advance(owed)

    async def _timer_did_something(self, wake: int) -> bool:
        """Release the sandbox's clock to a timer and let it settle. A timer that wrote nothing and called nothing
        (a runtime's own housekeeping: an HTTP server's poll, a garbage collector's tick) is no wake of the agent's,
        and `wake` is withdrawn; one that did something is a wake, played on as one."""
        head, calls = self._store.head(), len(self._store.calls())
        await self._release()
        sandbox = self._reach.sandbox
        assert sandbox is not None
        await sandbox.settle(self._traffic)
        if self._store.head() == head and len(self._store.calls()) == calls:
            self._clock.withdraw(wake)
            # a task whose timer fired and did nothing is a runtime's own (an HTTP server's poll): planned no more,
            # it fires once and re-arms whenever the clock is released past it
            for task in self._timer_tasks:
                self._quiet_tasks[task] = max(self._quiet_tasks.get(task, 0), self._timer_ns)
            return False
        return True

    async def _plan_timer(self) -> bool:
        """For a contained agent, its earliest timer, read from the sandbox once it is idle and nothing of its is in
        flight, is the wake it asked for, in place of the one read before. False when it never fell idle."""
        sandbox = self._reach.sandbox
        if sandbox is None:
            return True
        await self._release()  # a deadline is read from the sandbox's clock, which must be the run's
        found = await sandbox.settle(self._traffic)
        if found is None:
            self._failure = "the agent's sandbox did not fall idle within its settle limit"
            return False
        earliest = found.earliest(self._quiet_tasks)
        self._timer_tasks = found.tasks_at(earliest)
        self._timer_ns = earliest
        if earliest < 0:
            self._dues.cancel(_timer)
            return True
        at = self._clock.now() + timedelta(microseconds=earliest / 1000)
        self._dues.replace(_timer, PendingTimer(due=Due(at=at, kind=DueKind.AGENT_WAKE, ref="timer")))
        return True

    async def _run_on_to(self, deadline: datetime | None) -> None:
        """The world does not stop when the agent goes quiet: with nothing more due before it, the clock runs on to
        the end of the scenario's window (or an older scenario's deadline), and a checkpoint there records the moment
        the run reached. Without it a run would end where the agent stopped, and a wait it abandoned would never be
        seen to expire."""
        if deadline is None or self._clock.now() >= deadline:
            return
        await self._jump(deadline)
        await self._release()
        write_checkpoint(
            self._store,
            Checkpoint(
                wake=self._clock.wake(),
                now=self._clock.now(),
                replies=len(self._store.replies()),
                untaken=self._untaken,
                commitments=self._commitments,
                pending=self._dues.items,
                agent=self._remembered(),
            ),
        )
        self._record_new()

    async def _fire(self, fired: list[Pending]) -> None:
        """Change the world for what is due, in a fixed order: people's moves on what is pending on them (an answer
        to a message among them), a declared service's own moves, tickets, then what people do unprompted, then
        directions sent by message, then bookings."""
        for item in fired:
            if isinstance(item, PendingTransition):
                await self._move(item.pending)
        for item in fired:
            if isinstance(item, PendingService):
                assert self._desk is not None
                await self._desk.fire(item, self._store, self._clock)
        for item in fired:
            if isinstance(item, PendingHappening):
                await self._happen(self._scenario.happenings[item.happening])
        by_message = self._by_message
        for item in fired:
            if isinstance(item, PendingDirection) and by_message is not None:
                await self._say(by_message, item.text)
        for item in fired:
            if isinstance(item, PendingBooking):
                scheduler = self._services.schedulers[item.provider]
                if item.deliver:
                    await scheduler.deliver_booking(item.ref, self._store, self._clock)
                if item.advance:
                    await scheduler.advance_booking(item.ref, self._store, self._clock)
        for item in fired:
            if isinstance(item, PendingWake) and item.reason is WakeReason.TICK and not item.repeat:
                self._schedule_tick()
        # the world as it now stands may answer a call held on it: one whose moment came, or one what fired brought
        # what it waits for (a delivery to the queue it polls)
        await self._holding.look()

    async def _machine(self, due: list[PendingMachine]) -> bool:
        """Run what the scenario does to the agent's machine at this moment, before anything else due then, and
        record each. A change to the machine wakes nobody: the agent finds it when it next looks. False when one
        failed, which stops the run as the environment's failure, before anyone is woken."""
        for item in due:
            command = self._scenario.machine[item.command]
            ran = await run_machine(command, self._clock.now())
            record_machine(self._store, item.command, ran)
            self._watcher.record(self._store, Actor.SCENARIO)
            if ran.exit_code != 0:
                self._machine_failed = (
                    f"the scenario's machine command {command.said!r} exited {ran.exit_code}: "
                    f"{ran.output.strip()[-300:]}"
                )
                return False
        return True

    async def _happen(self, happening: Happening) -> None:
        """What a person does by themselves: a ticket happening is a transition through the ticket's provider, as any
        person's move is (`People.happen`); a document happening lands through the document provider's
        `ChangesDocuments`, a messaging happening is pushed through `PushesEvents`."""
        if isinstance(happening, TicketHappening):
            await self._engine.happen(happening, self._store, self._clock)
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

    async def _taken(self, fired: list[Pending]) -> None:
        """Wait until the agent has taken each booking just delivered from its own queue, when the scheduler can
        tell, or `Booked.take_limit` passes: an agent that reports IDLE before its poll has run would otherwise be
        moved past the wake its booking was for."""
        bookings = [p for p in fired if isinstance(p, PendingBooking)]
        limit = next((w.take_limit for w in self._agent.wakes if isinstance(w, Booked)), None)
        if not bookings or limit is None:
            return
        give_up = time.monotonic() + limit.total_seconds()
        for booking in bookings:
            scheduler = self._services.schedulers[booking.provider]
            if not isinstance(scheduler, ConfirmsDelivery):
                continue
            while not scheduler.taken(booking.ref, self._store) and time.monotonic() < give_up:
                await asyncio.sleep(TAKEN_EVERY)

    async def _ready(self, fired: list[Pending]) -> list[Pending]:
        """What is due, without each person's move that is not ready to land (`People.ready`): an answer that, read
        now, needs none is passed and nobody hears of it; one a model failed to write is owed still, and booked again
        once the next wake has been played."""
        ready: list[Pending] = []
        for item in fired:
            if isinstance(item, PendingTransition):
                readied = await self._engine.ready(item.pending, self._store, self._clock)
                if readied is Ready.FAILED and item.pending not in self._untaken:
                    self._untaken.append(item.pending)
                if readied is not Ready.READY:
                    continue
            ready.append(item)
        return ready

    async def _move(self, pending: EntityRef) -> None:
        """A person acts on an item pending on them; a move whose words a model failed to write is owed still, and
        tried again on the run's next turn."""
        acted = await self._engine.act(pending, self._store, self._clock)
        if acted.failure is not None and pending not in self._untaken:
            self._untaken.append(pending)

    def _unheard(self, pending: Pending) -> bool:
        """Something due the agent is not told of as it lands: a happening on a ticket or a document, which the
        agent finds on its next read (a document's provider may then tell a watching agent, `_watched`), and a
        person's move that the service tells the agent nothing of (`heard_of`): an email back, an answer to an
        invitation on a calendar nobody watches, a ticket moved. A messaging happening is pushed to the agent, and
        that push is a wake, as a pushed answer is, and so is the service's own notice of an answer that landed."""
        if isinstance(pending, PendingTransition):
            return not self._engine.heard_of(pending.pending, self._store, self._clock)
        if isinstance(pending, PendingService):
            assert self._desk is not None
            return not self._desk.heard(pending, self._store)
        if isinstance(pending, PendingCall):
            return pending.ends  # answered empty at the end of its wait; sooner, it is answered with what came
        return isinstance(pending, PendingHappening) and isinstance(
            self._scenario.happenings[pending.happening], TicketHappening | DocumentHappening
        )

    def _delivered_to(self, fired: list[Pending]) -> list[AgentDriver]:
        """A wake made only of bookings sends no request, since the scheduler's delivery is the wake, but the agent
        still acts on what was delivered: the loop waits on its main driver until it is no longer working."""
        if self._reach.main is None:
            return []
        if not any(_delivers(p) for p in fired) and not self._watched(fired):
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
            if isinstance(item, PendingHappening | PendingTransition | PendingService) and not self._unheard(item):
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
            stop = await self._played(wake, reason, fire, requests, settle)
        finally:
            self._store.wake_ended(wake)
        if self._environment is not None and self._environment.failure() is not None:
            return StopReason.ENVIRONMENT_FAILED
        return stop

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
            if self._reach.sandbox is not None and await self._reach.sandbox.settle(self._traffic) is None:
                raise AgentFailed("the agent's sandbox did not fall idle within its settle limit")
            for driver in settle:
                report = await driver.settled()
                done = done or report.status is AgentStatus.DONE
                if driver is self._reach.main:
                    commitments_changed = self._adopt(report)
        except AgentFailed as e:
            failed = True
            self._failure = str(e)
        if not failed:
            self._watcher.record(self._store, Actor.AGENT)
            await self._look()
        new = self._record_new()
        if not failed:
            self._marked(new, wake)
            await self._schedule(new, retry=True)
        mine = [e for e in new if e.wake == wake and e.actor is Actor.AGENT]
        self._wakes.append(
            WakeRecord(
                index=wake,
                sim_time=self._clock.now(),
                reason=reason.value,
                world_changes=sum(
                    1 for e in mine if e.operation not in _NOT_CHANGES and e.entity.kind not in _NOT_THE_WORLD
                ),
                commitments_changed=commitments_changed,
                memory_reads=self._mounts.memory_reads(wake) if isinstance(self._mounts, HoldsMemory) else 0,
                memory_writes=sum(
                    1 for e in mine if e.entity.kind is EntityKind.MEMORY and e.operation not in _NOT_CHANGES
                ),
            )
        )
        if self._telemetry is not None:
            self._telemetry.wake_ended(wake)
        if failed:
            return StopReason.AGENT_FAILED
        self._checkpoint()
        if done and self._scenario.is_task:
            return StopReason.AGENT_DONE  # only an older scenario's task ends on done: a world runs its window
        if len(self._wakes) >= self._limit.wakes:
            return StopReason.WAKE_LIMIT
        return None

    def _adopt(self, report: AgentReport) -> bool:
        """Take the agent's next wake, replacing the one it named before. Answers whether its commitments changed."""
        if report.next_wake is None:
            self._dues.cancel(_reported_wake)
        else:
            self._dues.replace(
                _reported_wake,
                PendingWake(
                    due=Due(at=report.next_wake, kind=DueKind.AGENT_WAKE, ref="next_wake"), reason=WakeReason.DUE
                ),
            )
        changed = report.commitments != self._commitments
        self._commitments = report.commitments
        self._last_report = report
        return changed

    def _marked(self, new: list[WorldEvent], wake: int) -> None:
        """The last next wake the agent marked in this wake (`minutehand.agent.wake`), taken as its next wake: it
        replaces the one it marked or reported before, and a mark of none cancels it."""
        said = [e.after for e in new if e.wake == wake and isinstance(e.after, NextWakeSnapshot)]
        if not said:
            return
        at = said[-1].at
        if at is None:
            self._dues.cancel(_reported_wake)
        else:
            self._dues.replace(
                _reported_wake,
                PendingWake(due=Due(at=at, kind=DueKind.AGENT_WAKE, ref="next_wake"), reason=WakeReason.DUE),
            )

    def _schedule_tick(self) -> None:
        assert self._reach.every is not None
        self._dues.enter(
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

    async def _schedule(self, new: list[WorldEvent], *, retry: bool = False) -> None:
        """What the world owes back for what the agent just did: a declared service's own moves, what people do about
        what waits on them now (the people engine's), and the fates of its tickets.

        A person answers a message as it reads when the engine looks at the end of the wake: an agent that posts a
        placeholder and edits it into its question in the same wake is answered about the question, once, and never
        about the placeholder. An edit in a later wake that changes what an ask says is put to the person again
        unless they have already answered it: an answer still on its way is planned afresh on the new text."""
        if self._desk is not None:
            for event in new:
                for owed in self._desk.bookings(event, self._store):
                    self._dues.enter(owed)
        await self._transitions(retry=retry)

    async def _transitions(self, *, retry: bool) -> None:
        """What waits on people, looked at now: each new item booked at its person's moment, each one whose moment
        moved (a follow-up, an edit) booked again, each gone undone taken out of the table, and each move a model
        failed to write booked again now: once, and again only after a wake (`retry`), so a model that stays down
        does not hold the clock still."""
        if retry:
            self._retried = []
        untaken = [p for p in self._untaken if p not in self._retried]
        self._untaken = [p for p in self._untaken if p in self._retried]
        if untaken:
            self._retried += untaken
            for pending in untaken:
                conversation = self._engine.pending(pending, self._store).conversation
                self._dues.enter(
                    PendingTransition(
                        due=Due(
                            at=self._clock.now(),
                            kind=DueKind.PERSON_REPLY if conversation else DueKind.TRANSITION,
                            ref=f"transition:{pending.external_id}",
                        ),
                        pending=pending,
                    )
                )
        looked = await self._engine.look(self._store, self._clock)
        for gone in looked.gone:
            self._dues.cancel(lambda p, gone=gone: isinstance(p, PendingTransition) and p.pending == gone)
        for moved in looked.moved:
            self._dues.cancel(lambda p, moved=moved: isinstance(p, PendingTransition) and p.pending == moved.pending)
        for booked in [*looked.booked, *looked.moved]:
            due = booked_due(booked, self._clock.now())
            if due is not None:
                self._dues.enter(due, drawn=booked.drawn)

    async def _look(self) -> None:
        """Read every inbox in the agent's own product as each person: an item seen first is the agent asking them,
        and the engine plans its answer when the new events are scheduled; one gone undecided is no longer theirs to
        answer, and whatever they had planned for it never reaches anyone."""
        if self._inboxes is not None:
            await self._inboxes.look(self._store, self._clock)

    async def _say(self, provider: ProviderKey, text: str) -> None:
        """An older scenario's owner messages the agent: its goal, or a direction."""
        sender = self._scenario.acting(None, "who sends the scenario's goal and directions (owner)")
        message = PersonMessage(person=sender, text=text, at=self._clock.now())
        await self._pushes(provider).say(
            message, self._inbound(provider), self._store, self._clock, secret=self._secret(provider)
        )

    def _checkpoint(self) -> None:
        write_checkpoint(
            self._store,
            Checkpoint(
                wake=self._clock.wake(),
                now=self._clock.now(),
                replies=len(self._store.replies()),
                untaken=self._untaken,
                commitments=self._commitments,
                pending=self._dues.items,
                agent=self._remembered(),
            ),
        )
        self._record_new()

    def _remembered(self) -> Remembered:
        """The agent's state at this moment as the run holds it: its memory, its last report, and what the run can
        see of state it keeps elsewhere."""
        return Remembered(
            report=self._last_report,
            memory=memory.store_digest(self._store),
            outside=self._outside.outside() if self._outside is not None else [],
        )

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


def acting(provider: object) -> object:
    """What people act through on `provider`: itself, or, for one that pushes their answers to an agent, its
    transitions as they would be taken for any agent (`TalksToAgent`)."""
    return provider.talking(None, None) if isinstance(provider, TalksToAgent) else provider


def booked_due(booked: Booking, now: datetime) -> PendingTransition | None:
    """What the run owes for a person's booked move: due at its moment, or at once when that is past; as a reply when
    it answers an ask in words. None when they never act on it."""
    if booked.at is None:
        return None
    return PendingTransition(
        due=Due(
            at=max(booked.at, now),
            kind=DueKind.PERSON_REPLY if booked.conversation else DueKind.TRANSITION,
            ref=f"transition:{booked.pending.external_id}",
        ),
        pending=booked.pending,
    )


def transition_ports(
    services: Services,
    agent: AgentUnderTest,
    signing: Mapping[ProviderKey, str],
    channels: Mapping[ProviderKey, TakesReplies],
    inboxes: Inboxes | None,
    desk: ServiceDesk | None,
    scenario: Scenario,
) -> dict[ProviderKey, object]:
    """Every provider of the run people act through (`ports.transitions`), by key: each provider in the run, one
    that pushes answers to the agent bound to its inbound target and signing secret (`TalksToAgent`); each of the
    agent's captured channels; each inbox of its own product; each declared service."""
    ports: dict[ProviderKey, object] = {}
    for provider in services.providers:
        key = provider.manifest.key
        if isinstance(provider, TalksToAgent):
            target = next((t for t in agent.inbound if t.provider == key), None)
            ports[key] = provider.talking(target, signing[key] if key in signing else None)
        else:
            ports[key] = provider
    ports.update(channels)
    if inboxes is not None:
        for key in inboxes.reaches:
            ports[key] = inboxes
    if desk is not None:
        for service in scenario.services:
            ports[service.key] = desk.provider(service.key)
    return ports


def _refuse_unlanded_happenings(scenario: Scenario, agent: AgentUnderTest, services: Services) -> None:
    """Every happening lands through a port its provider has, checked before anything is seeded: a ticket happening
    needs the ticket's provider in the run holding seeded tickets (`HoldsSeeded`), a document happening the document's provider
    implementing `ChangesDocuments`; a messaging happening needs its provider
    pushing events and the agent declaring an inbound target for it."""
    for n, happening in enumerate(scenario.happenings, start=1):
        provider = scenario.happening_provider(happening)
        found = next((p for p in services.providers if p.manifest.key == provider), None)
        if isinstance(happening, TicketHappening):
            if isinstance(acting(found), HoldsSeeded):
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


def _held(pending: Pending, ref: str) -> bool:
    return isinstance(pending, PendingCall) and pending.due.ref == ref


def _delivers(pending: Pending) -> bool:
    """Something due that hands the agent what it acts on without a wake request: a booking's delivery, or a held
    call answered when what it waits for came."""
    return isinstance(pending, PendingBooking) or (isinstance(pending, PendingCall) and not pending.ends)


def _timer(pending: Pending) -> bool:
    """The agent's earliest timer as last read from its sandbox, which the next reading replaces."""
    return isinstance(pending, PendingTimer)


def _planned_by_agent(pending: Pending) -> bool:
    """A wake the agent planned from what it remembers: its reported or marked next wake, its own timer, a booking. A
    late or second delivery is the scenario's dispatch, and a polled tick its declared rhythm."""
    return _reported_wake(pending) or isinstance(pending, PendingTimer | PendingBooking)


def _reported_wake(pending: Pending) -> bool:
    """The wake the agent last named in its report, which the next one it names replaces. A late or second delivery
    of one is already on its way, as a real scheduler's is, and a new report does not take it back."""
    return isinstance(pending, PendingWake) and pending.reason is WakeReason.DUE and not pending.repeat


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
    signing: Mapping[ProviderKey, str] | None = None,
    traffic: Traffic | None = None,
    channels: Mapping[ProviderKey, TakesReplies] | None = None,
    environment: Environment | None = None,
    inboxes: Inboxes | None = None,
    outside: OutsideState | None = None,
    model: LanguageModel | None = None,
    desk: ServiceDesk | None = None,
) -> RunRecord:
    """Run one scenario from its start. `signing` holds the secret each provider signs its pushed events with;
    `traffic` sees the agent's outbound calls, which a sandbox whose clock Minutehand owns needs to fall idle;
    `channels` delivers people's answers to the agent's captured sends; `outside` says what the agent keeps
    beside its memory at each checkpoint."""
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
        signing=signing,
        traffic=traffic,
        channels=channels,
        environment=environment,
        inboxes=inboxes,
        outside=outside,
        model=model,
        desk=desk,
    ).run()
