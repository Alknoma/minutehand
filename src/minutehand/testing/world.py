"""`OpenWorld`: one world a test opened, with what a test reads, does and asserts on it. An assertion that fails
says what the world last did, so the failure explains itself."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta

from minutehand.adapters.control.wire import (
    QUIET_AT_MOST,
    QUIET_FOR,
    Advanced,
    ChangePerson,
    Checked,
    CreateWorld,
    DecideNow,
    DecisionView,
    DeclareFaults,
    DeleteTicket,
    EditTicket,
    Fault,
    FurtherSeed,
    Happen,
    InboxesView,
    MintInbound,
    MoveTicket,
    Permit,
    PressControl,
    Quiet,
    Quieted,
    RawState,
    Reply,
    Say,
    Seeded,
    StepView,
    WorldView,
)
from minutehand.domain.agent import AgentReport
from minutehand.domain.people import InboundCredential, InboundCredentialAsk, PermissionGrant, Press
from minutehand.domain.provider import PersonChange
from minutehand.domain.scenario import Happening, Person, TicketState
from minutehand.domain.telemetry import StoredSpan
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    MessageSnapshot,
    Operation,
    RecordedCall,
    Stored,
    TicketSnapshot,
    WorldEvent,
)
from minutehand.testing.client import MinutehandClient

RECENT = 25
"""How many of the world's latest events a failed assertion prints."""


def describe(event: WorldEvent) -> str:
    """One line a person reads: who did what to which thing, and what it says."""
    after = event.after
    said = ""
    if isinstance(after, MessageSnapshot):
        said = f" in {after.channel}: {after.text!r}"
    elif isinstance(after, TicketSnapshot):
        said = f": {after.title!r} ({after.state.value}, {after.assignee_email or 'unassigned'})"
    return (
        f"#{event.seq} {event.sim_time:%Y-%m-%d %H:%M} {event.actor.value} {event.operation.value} "
        f"{event.entity.provider} {event.entity.kind.value} {event.entity.external_id}{said}"
    )


class ClosedWorld(Exception):
    """A handle used after its world was closed: whatever it was asked would have reached no world, or, had the
    id been handed out again, another test's."""


class OpenWorld:
    """One open world. Once `close()`d (the plugin closes it after its test), every use of the handle raises
    `ClosedWorld`, and the server never hands the id out again."""

    def __init__(self, client: MinutehandClient, view: WorldView) -> None:
        self._client = client
        self.view = view
        self.world_id = view.world_id
        self.closed: Checked | None = None

    @property
    def client(self) -> MinutehandClient:
        """The client, while the world is open; a closed world's handle reaches nothing through it."""
        if self.closed is not None:
            raise ClosedWorld(f"world {self.world_id} was closed; open a new world rather than reuse this one")
        return self._client

    def close(self, *, quiet: Quiet | bool = True) -> Checked:
        """Close the world (once it is quiet, unless `quiet` is False): its checks as it stood. A second close
        raises `ClosedWorld`."""
        found = self.client.close_world(self.world_id, quiet=quiet)
        self.closed = found
        return found

    # -- reading ------------------------------------------------------------------------------------------------

    def events(
        self,
        *,
        provider: str | None = None,
        kind: EntityKind | None = None,
        actor: Actor | None = None,
        operation: Operation | None = None,
        since: int | None = None,
        since_reset: bool = True,
    ) -> list[WorldEvent]:
        """Since the last reset; with `since_reset` False, every event the world has recorded, the stretch before
        each reset first (`EventsPage.resets` on the client's page says where each falls)."""
        page = self.client.events(
            self.world_id,
            provider=provider,
            kind=kind,
            actor=actor,
            operation=operation,
            since=since,
            since_reset=since_reset,
        )
        return page.events

    def entities(self, *, provider: str | None = None, kind: EntityKind | None = None) -> list[Stored]:
        return self.client.entities(self.world_id, provider=provider, kind=kind).entities

    def calls(self, *, since_reset: bool = True) -> list[RecordedCall]:
        """Every call of this world since its last reset; with `since_reset` False, every call it has had, so a test
        can count the calls made before a reset."""
        return self.client.calls(self.world_id, since_reset=since_reset).calls

    def unmatched_calls(self, *, since_reset: bool = True) -> list[RecordedCall]:
        """This world's calls to hosts no provider claims and no declaration captures, refused with 502."""
        return self.client.calls(self.world_id, unmatched=True, since_reset=since_reset).calls

    def captured_calls(self, *, since_reset: bool = True) -> list[RecordedCall]:
        """This world's calls to hosts declared as outbound (`CreateWorld.outbound`), captured and not refused."""
        return self.client.calls(self.world_id, captured=True, since_reset=since_reset).calls

    def spans(self, *, since_reset: bool = True) -> list[StoredSpan]:
        """Spans the services exported in traces this world's calls carried, and model calls it recorded."""
        return self.client.spans(self.world_id, since_reset=since_reset).spans

    def late_calls(self) -> list[RecordedCall]:
        """Calls that came for this world after it was closed, refused into the lobby (`Exchange.late_for`): the
        one read a closed world's handle still answers."""
        return self._client.unmatched(late_for=self.world_id).calls

    def assert_nothing_unclaimed(self) -> None:
        """Fail, listing each one, when a call of this world went to a host no provider claims and nothing the
        world declares captures (refused 502). Calls to model hosts and declared outbound hosts are not that."""
        found = self.unmatched_calls()
        if found:
            lines = [f"{c.exchange.method} {c.exchange.host}{c.exchange.path} -> {c.exchange.status}" for c in found]
            raise AssertionError(
                f"{len(found)} call(s) of world {self.world_id} reached no provider and no declaration:\n"
                + "\n".join(lines)
            )

    def quiet(self, *, quiet_for: timedelta = QUIET_FOR, at_most: timedelta = QUIET_AT_MOST) -> Quieted:
        """Return once no call has reached this world for `quiet_for` and nothing Minutehand pushed to the service
        still awaits its answer, or once `at_most` has passed; `Quieted.quiet` says which."""
        return self.client.quiet(self.world_id, Quiet(quiet_for=quiet_for, at_most=at_most))

    def now(self) -> datetime:
        return self.client.world(self.world_id).now

    def checks(self) -> Checked:
        """The world's checks as it stands; for a world of a case, the case's."""
        return self.client.checks(self.world_id)

    # -- what waits on people in the service's own product ------------------------------------------------------

    def inboxes(self) -> InboxesView:
        """Read the inboxes now: what waits on people, and the decisions they owe with when each falls due."""
        return self.client.read_inboxes(self.world_id)

    def perform_due(self) -> list[DecisionView]:
        """Make every decision due at the world's clock, as its person."""
        return self.client.perform_due_decisions(self.world_id).decisions

    def decide(
        self, person: str, item: EntityRef, decision: str, inputs: Mapping[str, str] | None = None
    ) -> DecisionView:
        """`person` decides `item` now, as the product's page would send it."""
        asked = DecideNow(person=person, item=item, decision=decision, inputs=dict(inputs or {}))
        return self.client.decide(self.world_id, asked)

    @property
    def case_id(self) -> str | None:
        """The case this world belongs to (`CreateWorld.case`), None for a world of its own."""
        return self.view.case_id

    def begin_step(self, *, at: datetime | None = None, reason: str | None = None) -> StepView:
        """A step of the agent begins (in every world of its case, for a world of one); the clock is not moved."""
        return self.client.begin_step(self.world_id, at=at, reason=reason)

    def end_step(self) -> StepView:
        return self.client.end_step(self.world_id)

    @contextmanager
    def step(self, *, at: datetime | None = None, reason: str | None = None) -> Iterator[StepView]:
        """One step of the agent, begun on entering and ended on leaving."""
        began = self.begin_step(at=at, reason=reason)
        try:
            yield began
        finally:
            if self.closed is None:
                self.end_step()

    # -- acting -------------------------------------------------------------------------------------------------

    def report(self, reported: AgentReport) -> Checked:
        """Relay what the agent says of its own work: see `MinutehandClient.report`."""
        return self.client.report(self.world_id, reported)

    def say(self, person: str, text: str, *, provider: str = "slack") -> WorldEvent:
        """`person` messages the agent directly; the event is pushed to the world's inbound target, signed."""
        return self.client.act(self.world_id, Say(person=person, text=text, provider=provider)).event

    def reply(self, person: str, text: str, *, to: EntityRef) -> WorldEvent:
        return self.client.act(self.world_id, Reply(person=person, text=text, to=to)).event

    def move_ticket(self, ticket: EntityRef, to: TicketState) -> WorldEvent:
        return self.client.act(self.world_id, MoveTicket(ticket=ticket, to=to)).event

    def edit_ticket(
        self, ticket: EntityRef, *, state: TicketState | None = None, assignee: str | None = None
    ) -> WorldEvent:
        return self.client.act(self.world_id, EditTicket(ticket=ticket, state=state, assignee=assignee)).event

    def happen(self, happening: Happening) -> WorldEvent:
        """A person does `happening` now, whatever its family, as the world lands one when its clock passes it."""
        return self.client.act(self.world_id, Happen(happening=happening)).event

    def press(self, person: str, on: EntityRef, press: Press) -> WorldEvent:
        """`person` uses a control on the message `on` now, pushed to the world's interactivity target."""
        return self.client.act(self.world_id, PressControl(person=person, on=on, press=press)).event

    def declare_faults(self, provider: str, seed: dict[str, object] | str) -> None:
        """Faults typed by `provider`, as a fragment of its own seed model (`{"faults": [...]}`)."""
        self.client.declare_faults(self.world_id, DeclareFaults.model_validate({"provider": provider, "seed": seed}))

    def delete_ticket(self, ticket: EntityRef) -> WorldEvent:
        """A person deletes the ticket now: one the agent filed, or one seeded."""
        return self.client.act(self.world_id, DeleteTicket(ticket=ticket)).event

    # -- changing the world from outside --------------------------------------------------------------------------

    def seed(self, added: FurtherSeed | dict[str, object]) -> Seeded:
        """More seeded into the open world: people, tickets, documents, spaces, sign-ins, channels, or a fragment of
        a provider's own seed (`{"provider_seeds": [{"provider": "notion", "body": {...}}]}`)."""
        further = added if isinstance(added, FurtherSeed) else FurtherSeed.model_validate(added)
        return self.client.further_seed(self.world_id, further)

    def add_person(self, person: Person | dict[str, object]) -> Seeded:
        """A person joins every provider the world holds that has people, as seeding would have written them."""
        joined = person if isinstance(person, Person) else Person.model_validate(person)
        return self.client.further_seed(self.world_id, FurtherSeed(people=[joined]))

    def remove_person(self, provider: str, person: str) -> WorldEvent:
        return self._person(provider, person, PersonChange.REMOVED)

    def deactivate_person(self, provider: str, person: str) -> WorldEvent:
        return self._person(provider, person, PersonChange.DEACTIVATED)

    def reactivate_person(self, provider: str, person: str) -> WorldEvent:
        return self._person(provider, person, PersonChange.REACTIVATED)

    def _person(self, provider: str, person: str, change: PersonChange) -> WorldEvent:
        asked = ChangePerson(provider=provider, person=person, change=change)
        return self.client.change_person(self.world_id, asked).event

    def grant(self, provider: str, person: str, permission: str, *, project: str | None = None) -> WorldEvent:
        held = PermissionGrant(person=person, permission=permission, project=project, held=True)
        return self.client.permit(self.world_id, Permit(provider=provider, grant=held)).event

    def withhold(self, provider: str, person: str, permission: str, *, project: str | None = None) -> WorldEvent:
        held = PermissionGrant(person=person, permission=permission, project=project, held=False)
        return self.client.permit(self.world_id, Permit(provider=provider, grant=held)).event

    def inbound_credential(self, provider: str, ask: InboundCredentialAsk) -> InboundCredential:
        """The headers `provider`'s service would send with a request this test builds itself."""
        return self.client.inbound_credential(self.world_id, MintInbound(provider=provider, ask=ask)).credential

    def reset(self) -> WorldView:
        """Back to the seed the world was opened with: the same id and claims, nothing that happened since."""
        self.view = self.client.reset(self.world_id)
        return self.view

    def raw_state(self, provider: str) -> RawState:
        """Everything the world holds of `provider`, every version: for reading by a person, not asserting on."""
        return self.client.raw_state(self.world_id, provider)

    def advance(self, by: timedelta | None = None, *, to: datetime | None = None) -> Advanced:
        return self.client.advance(self.world_id, by=by, to=to)

    def arm(self, fault: Fault) -> None:
        self.client.arm(self.world_id, fault)

    # -- asserting ----------------------------------------------------------------------------------------------

    def recent(self, count: int = RECENT) -> str:
        found = self.events()
        shown = [describe(e) for e in found[-count:] if e.operation not in (Operation.READ, Operation.SEARCH)]
        return "\n".join(shown) or "(the world holds no change)"

    def assert_events(
        self,
        where: Callable[[WorldEvent], bool],
        *,
        at_least: int = 1,
        at_most: int | None = None,
        what: str = "events matching the condition",
    ) -> list[WorldEvent]:
        """The events `where` holds for, counted against the bounds; the world's latest changes on failure."""
        found = [e for e in self.events() if where(e)]
        if len(found) < at_least or (at_most is not None and len(found) > at_most):
            bound = f"at least {at_least}" + (f" and at most {at_most}" if at_most is not None else "")
            raise AssertionError(
                f"wanted {bound} {what}, found {len(found)} in world {self.world_id}. Its latest changes:\n"
                f"{self.recent()}"
            )
        return found

    def assert_message(
        self,
        *,
        containing: str,
        by: Actor = Actor.AGENT,
        to: str | None = None,
        at_least: int = 1,
    ) -> list[WorldEvent]:
        """A message written by `by` holding `containing` (any case), sent to the person with email `to`."""

        def where(e: WorldEvent) -> bool:
            after = e.after
            return (
                e.actor is by
                and e.operation is Operation.CREATE
                and isinstance(after, MessageSnapshot)
                and containing.casefold() in after.text.casefold()
                and (to is None or to in after.recipient_emails)
            )

        recipient = f" to {to}" if to is not None else ""
        return self.assert_events(
            where, at_least=at_least, what=f"{by.value} messages{recipient} holding {containing!r}"
        )

    def assert_ticket(
        self,
        *,
        titled: str,
        state: TicketState | None = None,
        assignee: str | None = None,
        at_least: int = 1,
    ) -> list[WorldEvent]:
        """A ticket whose title holds `titled` (any case) was written with this state and assignee email."""

        def where(e: WorldEvent) -> bool:
            after = e.after
            return (
                isinstance(after, TicketSnapshot)
                and titled.casefold() in after.title.casefold()
                and (state is None or after.state is state)
                and (assignee is None or after.assignee_email == assignee)
            )

        return self.assert_events(where, at_least=at_least, what=f"ticket writes titled {titled!r}")


class OpenCase:
    """Several worlds a test opened under one case label (`CreateWorld.case`): one run, stepped and scored as one.
    `open_case` opens it; closing its last world closes it."""

    def __init__(self, client: MinutehandClient, case_id: str, worlds: Sequence[OpenWorld]) -> None:
        self._client = client
        self.case_id = case_id
        self.worlds = list(worlds)

    def begin_step(self, *, at: datetime | None = None, reason: str | None = None) -> StepView:
        """A step of the agent begins in every world of the case; the clocks are not moved."""
        return self._client.begin_case_step(self.case_id, at=at, reason=reason)

    def end_step(self) -> StepView:
        return self._client.end_case_step(self.case_id)

    @contextmanager
    def step(self, *, at: datetime | None = None, reason: str | None = None) -> Iterator[StepView]:
        """One step of the agent across the case, begun on entering and ended on leaving."""
        began = self.begin_step(at=at, reason=reason)
        try:
            yield began
        finally:
            if any(w.closed is None for w in self.worlds):
                self.end_step()

    def advance(self, by: timedelta | None = None, *, to: datetime | None = None) -> None:
        """Move every world's clock to the same moment: `to`, or `by` past the latest of them."""
        if to is None:
            assert by is not None, "give by or to"
            to = max(w.client.world(w.world_id).now for w in self.worlds) + by
        for world in self.worlds:
            world.advance(to=to)

    def inboxes(self) -> InboxesView:
        """Every world's inboxes read now, as one view: what waits on people, and the decisions owed, earliest
        first."""
        read = [w.inboxes() for w in self.worlds if w.closed is None]
        return InboxesView(
            pending=[p for r in read for p in r.pending],
            due=sorted((d for r in read for d in r.due), key=lambda d: d.at),
            unread=[u for r in read for u in r.unread],
        )

    def perform_due(self) -> list[DecisionView]:
        """Make every decision due at each world's clock, as its person."""
        return [d for w in self.worlds if w.closed is None for d in w.perform_due()]

    def decide(
        self, person: str, item: EntityRef, decision: str, inputs: Mapping[str, str] | None = None
    ) -> DecisionView:
        """`person` decides `item` now, in the world whose inbox it was seen in."""
        for world in self.worlds:
            if world.closed is None and any(p.item == item for p in world.inboxes().pending):
                return world.decide(person, item, decision, inputs)
        raise AssertionError(f"no open world of case {self.case_id} holds {item.external_id} pending")

    def checks(self) -> Checked:
        """The case scored as one run, as it stands."""
        return self._client.case_checks(self.case_id)

    def report(self, reported: AgentReport) -> Checked:
        """Relay what the agent says of its own work, for the case: see `MinutehandClient.report`."""
        for world in self.worlds:
            if world.closed is None:
                return world.report(reported)
        raise ClosedWorld(f"every world of case {self.case_id} was already closed")

    def close(self, *, quiet: Quiet | bool = True) -> Checked:
        """Close every world still open, in the order opened: the last close answers the case's final checks."""
        found: Checked | None = None
        for world in self.worlds:
            if world.closed is None:
                found = world.close(quiet=quiet)
        if found is None:
            raise ClosedWorld(f"every world of case {self.case_id} was already closed")
        return found


def open_case(client: MinutehandClient, case: str, specs: Sequence[CreateWorld]) -> OpenCase:
    """Open one world per spec under the case label `case` (each spec's own `case` is set to it): a case of them."""
    worlds = [OpenWorld(client, client.create_world(spec.model_copy(update={"case": case}))) for spec in specs]
    case_ids = {w.view.case_id for w in worlds}
    if len(case_ids) != 1 or None in case_ids:
        raise AssertionError(f"the worlds opened under {case!r} are in cases {sorted(map(str, case_ids))}")
    return OpenCase(client, next(iter(case_ids)) or "", worlds)
