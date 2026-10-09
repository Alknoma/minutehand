"""A standing world: seeded once, answered for as long as it is open, with no run loop and no agent wakes.

`minutehand serve` holds many of these at once, one per test. Nothing in one happens by itself: its clock
stands still until whoever opened it moves it (`advance`), and only then do the things the world owes fall
due, in order: what people do by themselves (the scenario's happenings, of every family, whether or not
scripted people speak), what a scripted person does about what waits on them (an answer to a message the agent
sent them, a decision in its product, a move on a ticket; the people engine's, as in a run), a ticket's fate,
the owner's directions. With `scripted` off, nobody answers but the test, which speaks for people itself (`say`,
`reply`, `move_ticket`, `edit_ticket`).

A world is read the way a finished run is: its log, its calls, and the same checks over it as of now.
What a scheduler provider booked is recorded in the log and is never fired here: a booking becomes a wake
only in the run loop.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from minutehand.application.checkpoint import PendingService
from minutehand.application.further_seed import Scratch, land
from minutehand.application.inboxes import Inboxes, Looked, items_in, refuse_clashing, refuse_undecided
from minutehand.application.model_calls import per_wake
from minutehand.application.moments import decision_text
from minutehand.application.orchestrator import refuse_fates_beside_the_engine
from minutehand.application.people import Booking, People, needs_model
from minutehand.application.refusals import RunRefused, refuse_unheld
from minutehand.application.replier import PeopleReplier, unspoken
from minutehand.application.run_clock import RunClock
from minutehand.application.services import ServiceDesk
from minutehand.application.steps import STEP, steps
from minutehand.checks.runner import RunResult, broken, contract_breaks, evaluate, view_of
from minutehand.domain.agent import AgentReport
from minutehand.domain.assessments import merged
from minutehand.domain.clock import Due
from minutehand.domain.people import (
    Decides,
    InboundCredential,
    InboundCredentialAsk,
    InboundTarget,
    PermissionGrant,
    PersonMessage,
    PersonReply,
    Plan,
    Press,
    Writing,
)
from minutehand.domain.provider import Manifest, PersonChange, merged_seed
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import (
    DocumentHappening,
    Happening,
    Person,
    ProviderKey,
    ProviderSeed,
    Scenario,
    SeededChannel,
    SeededDocument,
    SeededTicket,
    SharedSpace,
    SignIn,
    TicketHappening,
    TicketState,
)
from minutehand.domain.transitions import Transition
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    InboxItemSnapshot,
    ItemStatus,
    Operation,
    PendingSnapshot,
    TicketSnapshot,
    TransitionSnapshot,
    WorldEvent,
)
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.provider import (
    ActsOnTickets,
    ASGIApp,
    BooksWakes,
    ChangesDocuments,
    ChangesPeople,
    DeclaresFaults,
    DeletesTickets,
    DeliversInBackground,
    EditsTickets,
    GrantsPermissions,
    HoldsTickets,
    ListensForAgent,
    MintsInboundCredentials,
    NotifiesChanges,
    OwnsSeed,
    Provider,
    PushesEvents,
    PushesInteractions,
)
from minutehand.ports.store import Store
from minutehand.ports.transitions import TalksToAgent

TRANSITION = "transition"
"""How an owed move of a person's is written, as `OwedByPerson.writing` says it: the people engine's."""

FIRST_WAKE = 1
"""What happens after the seed and before any step is in this wake: the seed is wake 0, as in a run. A step marked
before anything else adopts it; each later step is a wake of its own (`application.steps`)."""


class WorldRefused(RunRefused):
    """What was asked of a standing world cannot be done, and nothing was changed."""


class Unsupported(WorldRefused):
    """What was asked is a capability the provider does not have, in any world."""


class NotFound(WorldRefused):
    """What was asked names something the world does not hold: a ticket, a message, a person's chat."""


class UnknownWorld(NotFound):
    """No open world has the id asked for."""


class UnknownCase(NotFound):
    """No open case has the id asked for."""


@contextmanager
def _refusing() -> Iterator[None]:
    """A port method refuses what the world cannot do with `LookupError` (it names nothing the world holds) or
    `ValueError` (it cannot be done); here each becomes the typed refusal the control API answers, its words kept.
    A `KeyError` or `IndexError` is a lookup that failed inside Minutehand, and a `ValidationError` a body that is
    not the model: neither is a refusal of the world's, and each goes on as it is."""
    try:
        yield
    except (KeyError, IndexError, ValidationError, RunRefused):
        raise
    except LookupError as e:
        raise NotFound(str(e.args[0]) if e.args else str(e)) from e
    except ValueError as e:
        raise WorldRefused(str(e)) from e


@dataclass(frozen=True)
class Fired:
    """One thing the world owed that fell due while the clock moved."""

    at: datetime
    what: str
    events: list[int]


class _Unfired:
    """`ports.provider.Wakes` for a scheduler provider in a standing world: what the agent books is the provider's
    own record in the log, and it never fires, since a booking becomes a wake only in the run loop
    (docs/serve.md, "Booked wakes")."""

    def book(self, due: Due) -> None:
        """Booked: the provider has recorded it; a standing world fires no booking."""

    def cancel(self, ref: str) -> None:
        """Cancelled: nothing was pending to drop."""


@dataclass
class _Owed:
    at: datetime
    what: str
    failed: str | None = None
    fate: tuple[EntityRef, TicketState | None] | None = None
    direction: str | None = None
    happening: Happening | None = None
    service: PendingService | None = None
    """A declared service's timer or system move on one of its items."""
    transition: EntityRef | None = None
    """The people engine's record of an item pending on a person, who acts on it then (`application.people`)."""


@dataclass(frozen=True)
class OwedByPerson:
    """An answer or decision a person owes, as a reader of the world is shown it: when it lands, what it answers,
    how its words are written, and why the last try to write them failed."""

    at: datetime
    person: str
    answers: EntityRef
    decision: bool
    writing: str
    failed: str | None


class StandingWorld:
    def __init__(
        self,
        *,
        scenario: Scenario,
        store: Store,
        clock: RunClock,
        provider: Callable[[ProviderKey], Provider],
        inbound: Sequence[InboundTarget],
        signing: Mapping[ProviderKey, str],
        scripted: bool,
        inboxes: Inboxes | None = None,
        model: LanguageModel | None = None,
        desk: ServiceDesk | None = None,
    ) -> None:
        """`provider` builds a provider by key; it is seeded into this world the first time it is had.
        `signing` is the secret each inbound target's events are signed with. `scripted` lets the scenario's
        people answer and decide, its tickets meet their fates and its directions be said, as the clock passes
        each. `model` writes what people say, as in a run; a world whose people speak in a model's words with no
        model is refused, naming them."""
        if scripted:
            needing = unspoken(scenario, [r.declared for r in inboxes.reaches.values()] if inboxes else [])
            needing += needs_model(scenario)
            if needing and model is None:
                raise WorldRefused(f"a model writes what {'; '.join(needing)} say, and no model is configured")
        if inboxes is not None:
            try:
                if scripted:
                    refuse_undecided(scenario, list(inboxes.reaches.values()))
                refuse_clashing(list(inboxes.reaches.values()), [t.provider for t in inbound])
            except RunRefused as e:
                raise WorldRefused(str(e)) from e
        unsigned = sorted({t.provider for t in inbound} - set(signing))
        if unsigned:
            raise WorldRefused(f"no signing secret for the inbound target on {', '.join(unsigned)}")
        self.scenario = scenario
        self.store = store
        self.clock = clock
        self.scripted = scripted
        self._provider = provider
        self._inbound = {t.provider: t for t in inbound}
        self._signing = dict(signing)
        self._built: dict[ProviderKey, Provider] = {}
        self._apps: dict[ProviderKey, ASGIApp] = {}
        self._people = {p.email: p for p in scenario.people}
        self.inboxes = inboxes
        self._model = model
        self.desk = desk
        """The world's declared services (`application.services`): what the proxy answers them from."""
        self._replier = self._people_for(scenario) if scripted else None
        self._engine: People | None = None
        self._named: list[ProviderKey] = []
        self._owed: list[_Owed] = []
        self._fated: set[EntityRef] = set()
        self._seen = 0
        self._acted: list[Happening] = []
        self._pushing: dict[int, str] = {}
        self._ended: set[int] = set()

    def _people_for(self, scenario: Scenario) -> PeopleReplier:
        try:
            return PeopleReplier(
                scenario, self._model, [r.declared for r in self.inboxes.reaches.values()] if self.inboxes else []
            )
        except RunRefused as e:
            raise WorldRefused(str(e)) from e

    # -- the world as the proxy answers it --------------------------------------------------------------------

    def open(self, named: Sequence[ProviderKey], *, wake: int = FIRST_WAKE) -> None:
        """Seed every provider in `named`, then begin the wake everything after the seed happens in until the next
        step: the first, or, for a world joining a case, the case's step in progress."""
        for key in named:
            self.provider(key)
        self._named = list(named)
        if self.scripted:
            try:
                refuse_fates_beside_the_engine(self.scenario, [self.provider(k).manifest for k in named])
            except RunRefused as e:
                raise WorldRefused(str(e)) from e
            self._engine = self._engine_for(self.scenario)
        for n, happening in enumerate(self.scenario.happenings, start=1):
            self._lands(happening, n)
            at = self.scenario.starts_at + happening.after
            self._owed.append(
                _Owed(at=at, what=f"happening {n}: {happening.person} {_doing(happening)}", happening=happening)
            )
        if self.scripted:
            for direction in self.scenario.directions:
                at = self.scenario.starts_at + direction.after
                self._owed.append(_Owed(at=at, what="the owner's direction", direction=direction.text))
        self.clock.enter(wake)
        self.store.wake_began(wake)
        self._seen = self.store.head()

    def close(self) -> None:
        self.end_wake()

    def _engine_for(self, scenario: Scenario) -> People:
        """The people engine over every provider of the world people act through: each one opened (one that pushes
        answers to the agent bound to its inbound target), each inbox of the agent's product, each declared
        service."""
        assert self._replier is not None
        ports: dict[ProviderKey, object] = {}
        for key in self._named:
            found = self.provider(key)
            if isinstance(found, TalksToAgent):
                target = self._inbound[key] if key in self._inbound else None
                ports[key] = found.talking(target, self._signing[key] if key in self._signing else None)
            else:
                ports[key] = found
        if self.inboxes is not None:
            for key in self.inboxes.reaches:
                ports[key] = self.inboxes
        if self.desk is not None:
            for service in scenario.services:
                ports[service.key] = self.desk.provider(service.key)
        try:
            return People(scenario, ports, self._replier, self._model)
        except RunRefused as e:
            raise WorldRefused(str(e)) from e

    def enter_wake(self, wake: int) -> None:
        """A step begins: the wake in progress ends and `wake`'s window opens."""
        self.end_wake()
        self.clock.enter(wake)
        self.store.wake_began(wake)

    def end_wake(self) -> None:
        """The wake in progress ends, once: what happens after it and before the next step still carries its number,
        as between two wakes of a run."""
        if self.clock.wake() not in self._ended:
            self._ended.add(self.clock.wake())
            self.store.wake_ended(self.clock.wake())

    def delivering(self) -> list[str]:
        """Each delivery to the agent's service still awaiting its answer: an event, reply, press or happening this
        world is pushing now, and what a provider's app pushes in the background (`DeliversInBackground`)."""
        found = list(self._pushing.values())
        for key, app in self._apps.items():
            if isinstance(app, DeliversInBackground) and (count := app.delivering()):
                found.append(f"{count} delivery(ies) {key} is pushing in the background")
        return found

    @asynccontextmanager
    async def _push(self, what: str) -> AsyncIterator[None]:
        """`what` is being delivered to the agent's service until the block ends."""
        token = object()
        self._pushing[id(token)] = what
        try:
            yield
        finally:
            del self._pushing[id(token)]

    def provider(self, key: ProviderKey) -> Provider:
        """The provider, built and seeded with the scenario the first time this world has it, unless the world
        already holds anything of it."""
        if key not in self._built:
            found = self._provider(key)
            if isinstance(found, BooksWakes):
                found.bind(_Unfired())
            if isinstance(found, ListensForAgent):
                found.listen(
                    self._inbound[key] if key in self._inbound else None,
                    self._signing[key] if key in self._signing else None,
                )
            if not any(e.entity.provider == key for e in self.store.events()):
                with _refusing():
                    found.seed(self.scenario, self.store)
            self._built[key] = found
        return self._built[key]

    def app_for(self, manifest: Manifest) -> ASGIApp:
        if manifest.key not in self._apps:
            self._apps[manifest.key] = self.provider(manifest.key).app(self.store, self.clock)
        return self._apps[manifest.key]

    # -- time ---------------------------------------------------------------------------------------------------

    async def advance(self, to: datetime) -> list[Fired]:
        """Move the clock to `to`, firing in order everything the world owes up to it."""
        if to < self.clock.now():
            raise WorldRefused(
                f"the clock only moves forward: {to.isoformat()} is before {self.clock.now().isoformat()}"
            )
        await self.look()
        fired: list[Fired] = []
        tried: set[int] = set()
        while True:
            due = sorted((o for o in self._owed if o.at <= to and id(o) not in tried), key=lambda o: o.at)
            if not due:
                break
            owed = due[0]
            self._owed.remove(owed)
            self.clock.jump(max(owed.at, self.clock.now()))
            before = self.store.head()
            async with self._push(owed.what):
                with _refusing():
                    done = await self._fire(owed)
            if not done:
                tried.add(id(owed))
                self._owed.append(owed)
                fired.append(Fired(at=self.clock.now(), what=f"{owed.what}: not written ({owed.failed})", events=[]))
                continue
            fired.append(
                Fired(at=self.clock.now(), what=owed.what, events=list(range(before + 1, self.store.head() + 1)))
            )
            await self.look()
        self.clock.jump(to)
        return fired

    def owed(self) -> list[tuple[datetime, str]]:
        """What will fall due as the clock moves, earliest first."""
        return [(o.at, o.what) for o in sorted(self._owed, key=lambda o: o.at)]

    def owed_by_people(self) -> list[OwedByPerson]:
        """Every answer, decision and move people owe, earliest first: when, to what, how its words are written."""
        found: list[OwedByPerson] = []
        for o in sorted(self._owed, key=lambda o: o.at):
            if o.transition is None or self._engine is None:
                continue
            held = self._engine.pending(o.transition, self.store)
            plan = Plan.model_validate_json(held.plan) if held.plan is not None else None
            found.append(
                OwedByPerson(
                    at=o.at,
                    person=held.person,
                    answers=held.item,
                    decision=held.item.kind is EntityKind.INBOX_ITEM,
                    writing=plan.writing.value if plan is not None else TRANSITION,
                    failed=o.failed if o.failed is not None else held.failure,
                )
            )
        return found

    async def look(self) -> Looked:
        """Read every inbox of the agent's own product as each person (`application.inboxes`), then observe what
        the agent did: an item gone undecided no longer waits on its person, and what they owed on it is dropped."""
        looked = Looked()
        if self.inboxes is not None:
            looked = await self.inboxes.look(self.store, self.clock)
        await self.observe()
        return looked

    def transitions(self) -> tuple[list[tuple[EntityRef, PendingSnapshot, datetime]], list[Transition]]:
        """Every item the people engine has held pending on a person (its record as it stands, and when it began to
        wait), and every transition of any item by anyone, in order."""
        events = self.store.events()
        since: dict[EntityRef, datetime] = {}
        held: dict[EntityRef, PendingSnapshot] = {}
        moves: list[Transition] = []
        for e in events:
            if isinstance(e.after, PendingSnapshot):
                since.setdefault(e.entity, e.sim_time)
                held[e.entity] = e.after
            elif isinstance(e.after, TransitionSnapshot):
                moves.append(Transition.of(e))
        return [(ref, item, since[ref]) for ref, item in held.items()], moves

    def pending_items(self) -> list[tuple[EntityRef, InboxItemSnapshot, datetime]]:
        """Every item still waiting on a person, with the moment it was first seen, in the order seen."""
        events = self.store.events()
        held = items_in(events)
        first = {
            e.entity: e.sim_time
            for e in reversed(events)
            if isinstance(e.after, InboxItemSnapshot) and e.operation is Operation.CREATE
        }
        return [
            (ref, item, first[ref]) for ref, item in held.items() if item.status is ItemStatus.PENDING and ref in first
        ]

    def due_decisions(self) -> list[tuple[datetime, str, EntityRef, Decides | None]]:
        """Every decision people owe, earliest first, with when it falls due, whose it is, the item, and the
        decision when it is known before its moment (a script's exact one); None: a model writes it then."""
        found: list[tuple[datetime, str, EntityRef, Decides | None]] = []
        for o, held in self._deciding():
            plan = Plan.model_validate_json(held.plan) if held.plan is not None else None
            known = (
                Decides(decision=plan.decision.decision, inputs=plan.decision.inputs)
                if plan is not None and plan.decision is not None and plan.writing is Writing.VERBATIM
                else None
            )
            found.append((o.at, held.person, held.item, known))
        return found

    def _deciding(self) -> list[tuple[_Owed, PendingSnapshot]]:
        """Every move owed on an item of the agent's own product, earliest first, with the engine's record of it."""
        if self._engine is None:
            return []
        found: list[tuple[_Owed, PendingSnapshot]] = []
        for o in sorted(self._owed, key=lambda o: o.at):
            if o.transition is None:
                continue
            held = self._engine.pending(o.transition, self.store)
            if held.item.kind is EntityKind.INBOX_ITEM:
                found.append((o, held))
        return found

    async def perform_due(self, now: datetime) -> list[WorldEvent]:
        """Read the inboxes, then make every decision due by `now` (the world's clock, or its case's when that is
        later), as its person, earliest first, each at the moment it fell due: the world's clock moves to it, and
        nothing else owed is fired."""
        await self.look()
        done: list[WorldEvent] = []
        for owed, _ in self._deciding():
            if owed.at > max(now, self.clock.now()):
                continue
            self._owed.remove(owed)
            self.clock.jump(max(owed.at, self.clock.now()))
            before = self.store.head()
            async with self._push(owed.what):
                with _refusing():
                    written = await self._fire(owed)
            if not written:
                self._owed.append(owed)
                continue
            made = [e for e in self.store.events(since=before) if isinstance(e.after, InboxItemSnapshot)]
            if made:
                done.append(made[-1])
        await self.look()
        return done

    async def decide_now(self, person: str, item: EntityRef, decides: Decides) -> WorldEvent:
        """`person` makes `decides` on `item` now, as the product's own page would send it: recorded as theirs,
        refused with nothing called when the item is not pending on them or the decision is not offered on it."""
        found = self._person(person)
        if self.inboxes is None or not self.inboxes.holds(item):
            raise NotFound(f"this world declares no inbox {item.provider}")
        held = items_in(self.store.events())
        if item not in held:
            raise NotFound(f"no item {item.external_id} was seen in inbox {item.provider}")
        snapshot = held[item]
        if snapshot.status is not ItemStatus.PENDING or snapshot.person != found.key:
            raise WorldRefused(
                f"item {item.external_id} is {snapshot.status.value} and waits on {snapshot.person or snapshot.waits_on}"
            )
        decision = self.inboxes.declared(item.provider).decision(decides.decision)
        if decision is None or decides.decision not in snapshot.decisions:
            raise WorldRefused(f"{decides.decision!r} is not offered on item {item.external_id}: {snapshot.decisions}")
        try:
            decision.refuse_inputs(decides.inputs, person)
        except ValueError as e:
            raise WorldRefused(str(e)) from e
        self.store.remember(
            PersonReply(
                person=person,
                in_reply_to=item,
                text=decision_text(decides.decision, decides.inputs),
                at=self.clock.now(),
                decides=decides,
            )
        )
        before = self.store.head()
        async with self._push(f"{person} decides {decides.decision} on {item.external_id}"):
            with _refusing():
                await self.inboxes.apply(
                    item, decides.decision, Actor.PERSON, found, json.dumps(decides.inputs), self.store, self.clock
                )
        await self.look()  # what the person owed on it is theirs no longer
        return next(e for e in self.store.events(since=before) if isinstance(e.after, InboxItemSnapshot))

    async def observe(self) -> None:
        """Read what the agent did since the last look and schedule what the world owes back: a declared service's
        own moves, what people do about what waits on them now (the people engine's), and the fate of each ticket it
        handed to a person who has one."""
        new = self.store.events(since=self._seen)
        if new:
            self._seen = new[-1].seq
        if self.desk is not None and self.scripted:
            for event in new:
                for owed in self.desk.bookings(event, self.store):
                    what = f"{owed.service} item {owed.item}: {owed.transition}"
                    self._owed.append(_Owed(at=owed.due.at, what=what, service=owed))
        if self._engine is None:
            return
        await self._transitions(self._engine)
        for event in new:
            after = event.after
            if (
                event.actor is Actor.AGENT
                and event.operation in (Operation.CREATE, Operation.UPDATE)
                and isinstance(after, TicketSnapshot)
                and after.assignee_email in self._people
            ):
                self._fate(self._people[after.assignee_email], event)

    def _writing(self, engine: People, booked: Booking) -> str:
        """How the words of an answer are written, as its owed line says it: its plan's, or the engine's pick."""
        held = engine.pending(booked.pending, self.store)
        return Plan.model_validate_json(held.plan).writing.value if held.plan is not None else TRANSITION

    async def _transitions(self, engine: People) -> None:
        """What waits on people, looked at now: each new item owed at its person's moment, each one whose moment
        moved (a follow-up, an edit) owed then instead, each gone undone dropped."""
        looked = await engine.look(self.store, self.clock)
        dropped = [*looked.gone, *(b.pending for b in looked.moved)]
        self._owed = [o for o in self._owed if o.transition is None or o.transition not in dropped]
        for booked in [*looked.booked, *looked.moved]:
            if booked.at is None:
                continue
            kind = booked.item.kind
            what = (
                f"{booked.person} decides on {booked.item.provider} item {booked.item.external_id}"
                if kind is EntityKind.INBOX_ITEM
                else f"{booked.person}'s reply ({self._writing(engine, booked)})"
                if booked.conversation
                else f"{booked.person} acts on {booked.item.provider} {booked.item.external_id}"
            )
            self._owed.append(_Owed(at=max(booked.at, self.clock.now()), what=what, transition=booked.pending))

    def _fate(self, person: Person, assigned: WorldEvent) -> None:
        fate = next((f for f in self.scenario.ticket_fates if f.assignee == person.key), None)
        if fate is None or assigned.entity in self._fated:
            return
        self._fated.add(assigned.entity)
        self._owed.append(
            _Owed(
                at=assigned.sim_time + fate.after,
                what=f"{person.key} "
                + (
                    f"moves {assigned.entity.external_id} to {fate.becomes.value}"
                    if fate.becomes is not None
                    else f"deletes {assigned.entity.external_id}"
                ),
                fate=(assigned.entity, fate.becomes),
            )
        )

    async def _fire(self, owed: _Owed) -> bool:
        """Carry out one thing owed; False when a person's words could not be written, and they still owe them."""
        if owed.service is not None:
            assert self.desk is not None
            await self.desk.fire(owed.service, self.store, self.clock)
            return True
        if owed.transition is not None:
            assert self._engine is not None
            acted = await self._engine.act(owed.transition, self.store, self.clock)
            owed.failed = acted.failure
            return acted.failure is None
        if owed.happening is not None:
            await self._happen(owed.happening)
        elif owed.fate is not None:
            ticket, becomes = owed.fate
            if becomes is None:
                self._deletes(ticket.provider).delete_ticket(ticket, self.store, self.clock)
            else:
                self._holds(ticket.provider).transition(ticket, becomes, self.store, self.clock)
        elif owed.direction is not None:
            await self.say(self.scenario.owner, owed.direction, provider=self._only_inbound())
        return True

    def _lands(self, happening: Happening, n: int) -> None:
        """Refuse, before the world opens, a happening whose provider lacks the port its family lands through."""
        provider = self.scenario.happening_provider(happening)
        found = self.provider(provider)
        if isinstance(happening, TicketHappening):
            port, ok = "ActsOnTickets", isinstance(found, ActsOnTickets)
        elif isinstance(happening, DocumentHappening):
            port, ok = "ChangesDocuments", isinstance(found, ChangesDocuments)
        else:
            port, ok = "PushesEvents", isinstance(found, PushesEvents) and provider in self._inbound
        if not ok:
            raise WorldRefused(
                f"happening {n} ({happening.person} {_doing(happening)}) lands on {provider}, which has no "
                f"{port}" + ("" if port != "PushesEvents" else " or no inbound target in this world")
            )

    async def _happen(self, happening: Happening) -> None:
        provider = self.scenario.happening_provider(happening)
        found = self.provider(provider)
        if isinstance(happening, TicketHappening):
            assert isinstance(found, ActsOnTickets)
            found.act(happening, self.scenario, self.store, self.clock)
        elif isinstance(happening, DocumentHappening):
            assert isinstance(found, ChangesDocuments)
            found.change(happening, self.scenario, self.store, self.clock)
            if isinstance(found, NotifiesChanges) and found.watched(self.store, self.clock):
                await found.notify(self.store, self.clock)
        else:
            await self._pushes(provider).happen(
                happening, self._target(provider), self.store, self.clock, secret=self._signing[provider]
            )

    # -- a person acting ----------------------------------------------------------------------------------------

    async def say(self, person: str, text: str, *, provider: ProviderKey) -> WorldEvent:
        """`person` messages the agent directly (in Slack, a DM to its bot), pushed to its inbound target."""
        self._person(person)
        before = self.store.head()
        message = PersonMessage(person=person, text=text, at=self.clock.now())
        async with self._push(f"{person} says {text[:40]!r} on {provider}"):
            pushes, target = self._pushes(provider), self._target(provider)
            with _refusing():
                await pushes.say(message, target, self.store, self.clock, secret=self._signing[provider])
        return self._written(before)

    async def reply(self, person: str, text: str, *, to: EntityRef) -> WorldEvent:
        """`person` answers a message: in its thread in a channel, as a new message in a DM."""
        self._person(person)
        before = self.store.head()
        answer = PersonReply(person=person, in_reply_to=to, text=text, at=self.clock.now())
        self.store.remember(answer)
        async with self._push(f"{person} replies {text[:40]!r} on {to.provider}"):
            pushes, target = self._pushes(to.provider), self._target(to.provider)
            with _refusing():
                await pushes.deliver(answer, target, self.store, self.clock, secret=self._signing[to.provider])
        return self._written(before)

    async def happen_now(self, happening: Happening) -> WorldEvent:
        """`happening`, of any family, done now rather than at its offset: checked as a scenario's would be (its
        person, its ticket, document, channel or post, the port it lands through), then landed the way `advance`
        lands one."""
        try:
            checked = type(self.scenario).model_validate(
                {**self.scenario.model_dump(), "happenings": [h.model_dump() for h in [*self._acted, happening]]}
            )
            key = checked.happening_provider(happening)
            refuse_unheld(checked, {key: self.provider(key).manifest})
        except (ValueError, RunRefused) as e:
            if isinstance(e, NotFound):
                raise
            raise WorldRefused(f"this happening cannot land here: {e}") from e
        self._lands(happening, 1)
        before = self.store.head()
        async with self._push(f"{happening.person} {_doing(happening)}"):
            with _refusing():
                await self._happen(happening)
        self._acted.append(happening)
        return self._written(before, reads=True)

    async def press(self, person: str, on: EntityRef, press: Press) -> WorldEvent:
        """`person` uses a control on the message `on` now (a button, a pick, a form filled), pushed to the
        agent's interactivity target the way the provider's service pushes it."""
        self._person(person)
        found = self.provider(on.provider)
        if not isinstance(found, PushesInteractions):
            raise WorldRefused(f"{on.provider} carries no controls a person can use")
        before = self.store.head()
        answer = PersonReply(person=person, in_reply_to=on, text=press.label, at=self.clock.now(), press=press)
        self.store.remember(answer)
        async with self._push(f"{person} presses {press.label!r} on {on.provider}"):
            target = self._target(on.provider)
            with _refusing():
                await found.press(answer, target, self.store, self.clock, secret=self._signing[on.provider])
        return self._written(before)

    def declare_faults(self, provider: ProviderKey, faults: str) -> None:
        """A fragment of `provider`'s own seed model declaring faults, validated and recorded by the provider."""
        found = self.provider(provider)
        if not isinstance(found, DeclaresFaults):
            raise WorldRefused(f"{provider} declares no faults of its own")
        with _refusing():
            try:
                found.declare(faults, self.store, self.clock)
            except ValueError as e:
                raise WorldRefused(f"{provider} cannot declare these faults: {e}") from e

    def delete_ticket(self, ticket: EntityRef) -> WorldEvent:
        """A person deletes the ticket now: one the agent filed, or one seeded."""
        before = self.store.head()
        deletes = self._deletes(ticket.provider)
        try:
            deletes.delete_ticket(ticket, self.store, self.clock)
        except LookupError as e:
            if isinstance(e, (KeyError, IndexError)):
                raise
            raise WorldRefused(str(e.args[0]) if e.args else str(e)) from e
        except ValueError as e:
            if isinstance(e, ValidationError):
                raise
            raise WorldRefused(str(e)) from e
        return self._written(before)

    # -- the world changed from outside, while open ---------------------------------------------------------------

    def extend(
        self,
        *,
        people: Sequence[Person] = (),
        tickets: Sequence[SeededTicket] = (),
        documents: Sequence[SeededDocument] = (),
        spaces: Sequence[SharedSpace] = (),
        sign_ins: Sequence[SignIn] = (),
        channels: Sequence[SeededChannel] = (),
        provider_seeds: Sequence[ProviderSeed] = (),
        directory: Path,
        scratch: Scratch,
    ) -> dict[ProviderKey, int]:
        """Seed more into the world as it stands (`application.further_seed`): the scenario grows by the addition,
        each provider the world holds is given what the addition means for it, and a provider it does not hold yet
        is seeded from the grown scenario when it is first had. Refused with nothing written when the grown scenario
        is not one, or the world cannot take it."""
        before = self.scenario
        seeds = {s.provider: s for s in before.provider_seeds}
        try:
            for fragment in provider_seeds:
                found = self.provider(fragment.provider)
                if not isinstance(found, OwnsSeed):
                    raise Unsupported(f"{fragment.provider} has no seed of its own to add to")
                held = seeds[fragment.provider].body if fragment.provider in seeds else None
                body = merged_seed(found.seed_model, held, fragment.body)
                seeds[fragment.provider] = ProviderSeed(provider=fragment.provider, body=body)
            after = type(before).model_validate(
                {
                    **before.model_dump(),
                    "people": [p.model_dump() for p in [*before.people, *people]],
                    "tickets": [t.model_dump() for t in [*before.tickets, *tickets]],
                    "documents": [d.model_dump() for d in [*before.documents, *documents]],
                    "spaces": [x.model_dump() for x in [*before.spaces, *spaces]],
                    "sign_ins": [x.model_dump() for x in [*before.sign_ins, *sign_ins]],
                    "channels": [c.model_dump() for c in [*before.channels, *channels]],
                    "provider_seeds": [x.model_dump() for x in seeds.values()],
                }
            )
            named = {t.provider for t in tickets} | {d.provider for d in documents} | {c.provider for c in channels}
            named |= {x.provider for x in [*spaces, *sign_ins, *provider_seeds]}
            refuse_unheld(after, {k: self.provider(k).manifest for k in named | set(self._built)})
            held = [k for k in self._built if any(e.entity.provider == k for e in self.store.events())]
            written = land(lambda k: self._built[k], held, before, after, self.store, directory, scratch)
        except (ValueError, RunRefused) as e:
            if isinstance(e, WorldRefused):
                raise
            raise WorldRefused(f"this addition cannot land here: {e}") from e
        except LookupError as e:
            if isinstance(e, (KeyError, IndexError)):
                raise
            raise NotFound(str(e.args[0]) if e.args else str(e)) from e
        self.scenario = after
        self._people = {p.email: p for p in after.people}
        if self._replier is not None:
            self._replier = self._people_for(after)
            self._engine = self._engine_for(after)
        for key in sorted(named - set(held)):
            self._built.pop(key, None)
            self._apps.pop(key, None)
            self.provider(key)
        self._seen = self.store.head()
        return dict(written)

    def change_person(self, provider: ProviderKey, person: str, change: PersonChange) -> WorldEvent:
        """Something happens to `person`'s account in `provider` now, as an administrator does it."""
        found = self.provider(provider)
        if not isinstance(found, ChangesPeople) or change not in found.manifest.people_changes:
            can = ", ".join(c.value for c in found.manifest.people_changes) or "nothing"
            raise Unsupported(f"{provider} cannot show a person {change.value}; it can show: {can}")
        before = self.store.head()
        try:
            found.change_person(change, self._person(person), self.store, self.clock)
        except (ValueError, LookupError) as e:
            raise WorldRefused(f"{provider} cannot do that to {person}: {e}") from e
        return self._written(before)

    def permit(self, provider: ProviderKey, grant: PermissionGrant) -> WorldEvent:
        """A named permission granted or withheld for a person now."""
        found = self.provider(provider)
        if not isinstance(found, GrantsPermissions):
            raise Unsupported(f"{provider} names no permissions that can be granted or withheld")
        before = self.store.head()
        try:
            found.permit(grant, self._person(grant.person), self.store, self.clock)
        except (ValueError, LookupError) as e:
            raise WorldRefused(f"{provider} cannot change that permission: {e}") from e
        return self._written(before)

    def credential(self, provider: ProviderKey, ask: InboundCredentialAsk) -> InboundCredential:
        """What the provider's service would send with a request a test builds itself, signed with this world's
        secret for the provider's inbound target (or a fresh one when it has none and its scheme needs none)."""
        found = self.provider(provider)
        if not isinstance(found, MintsInboundCredentials):
            raise Unsupported(f"{provider} signs nothing it pushes")
        secret = self._signing[provider] if provider in self._signing else ""
        with _refusing():
            try:
                return found.credential(ask, self.store, self.clock, secret=secret)
            except ValueError as e:
                raise WorldRefused(f"{provider} cannot sign that: {e}") from e

    def move_ticket(self, ticket: EntityRef, to: TicketState) -> WorldEvent:
        """The ticket's assignee moves it, as actor PERSON."""
        before = self.store.head()
        holds = self._holds(ticket.provider)
        with _refusing():
            holds.transition(ticket, to, self.store, self.clock)
        return self._written(before)

    def edit_ticket(self, ticket: EntityRef, *, state: TicketState | None, assignee: str | None) -> WorldEvent:
        """The ticket is rewritten from outside the agent (reassigned, reopened), as actor SCENARIO."""
        email = self._person(assignee).email if assignee is not None else None
        provider = self.provider(ticket.provider)
        if not isinstance(provider, EditsTickets):
            raise WorldRefused(f"{ticket.provider} holds no tickets that can be rewritten")
        before = self.store.head()
        with _refusing():
            provider.edit(ticket, state=state, assignee_email=email, world=self.store, clock=self.clock)
        return self._written(before)

    # -- reading ------------------------------------------------------------------------------------------------

    async def checks(self, *, stop: StopReason | None, reported: AgentReport | None = None) -> RunResult:
        """Every deterministic check and the scorecard over the world as it stands now."""
        await self.look()
        return score(self.scenario, self.store, stop=stop, ended=self.clock.now(), reported=reported)

    # -- lookups that refuse loudly -----------------------------------------------------------------------------

    def _written(self, before: int, *, reads: bool = False) -> WorldEvent:
        """The first change since `before`; with `reads`, a read counts too (a person opening the agent's page
        changes nothing and is recorded as what they read)."""
        written = [e for e in self.store.events(since=before) if reads or e.operation is not Operation.READ]
        if not written:
            raise WorldRefused("the provider recorded nothing for this act")
        return written[0]

    def _person(self, key: str) -> Person:
        found = next((p for p in self.scenario.people if p.key == key), None)
        if found is None:
            raise WorldRefused(
                f"no person {key} in this world; it has {', '.join(p.key for p in self.scenario.people)}"
            )
        return found

    def _pushes(self, provider: ProviderKey) -> PushesEvents:
        found = self.provider(provider)
        if not isinstance(found, PushesEvents):
            raise WorldRefused(f"{provider} pushes no events to the agent")
        return found

    def _target(self, provider: ProviderKey) -> InboundTarget:
        if provider not in self._inbound:
            raise WorldRefused(f"this world declares no inbound target for {provider}: where would the event go?")
        return self._inbound[provider]

    def _only_inbound(self) -> ProviderKey:
        if len(self._inbound) != 1:
            raise WorldRefused(
                "a direction is said on the world's one inbound target, and it declares " + str(len(self._inbound))
            )
        return next(iter(self._inbound))

    def _deletes(self, provider: ProviderKey) -> DeletesTickets:
        found = self.provider(provider)
        if not isinstance(found, DeletesTickets):
            raise Unsupported(f"{provider} holds no tickets a person can delete")
        return found

    def _holds(self, provider: ProviderKey) -> HoldsTickets:
        found = self.provider(provider)
        if not isinstance(found, HoldsTickets):
            raise WorldRefused(f"{provider} holds no tickets a person can move")
        return found


def score(
    scenario: Scenario,
    world: Store,
    *,
    stop: StopReason | None,
    ended: datetime,
    reported: AgentReport | None = None,
) -> RunResult:
    """Every deterministic check, the scenario's own rules and the scorecard over a standing world's record, or a
    case's merged one, as it stands: its steps are its wakes, and `reported` is the agent's own report as whoever
    drives it last relayed it. A standing world is opened without an agent file, so only the scenario's rules
    judge it."""
    wakes = steps(world)
    calls = world.calls()
    view = view_of(
        scenario,
        [e for e in world.events() if e.entity != STEP],
        wakes,
        world.replies(),
        commitments=reported.commitments if reported is not None else None,
        unmatched_calls=[c.exchange for c in calls if c.refused],
        model_calls=per_wake(world.spans(), [w.index for w in wakes]),
        broken_calls=broken(calls),
        contract_breaks=contract_breaks(calls),
        rules=merged([], scenario.assess, scenario.assess_off),
        stop=stop,
    )
    return evaluate(view, stop=stop, ended=ended)


def _doing(happening: Happening) -> str:
    """What the happening has its person do, in a few words, for a refusal or the list of what is owed."""
    if isinstance(happening, TicketHappening):
        return f"{happening.action.kind} the seeded ticket {happening.ticket!r}"
    if isinstance(happening, DocumentHappening):
        return f"{happening.action.kind} the seeded document {happening.document!r}"
    return happening.kind
