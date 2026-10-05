"""`OpenWorld`: one world a test opened, with what a test reads, does and asserts on it. An assertion that fails
says what the world last did, so the failure explains itself."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from minutehand.adapters.control.wire import Advanced, Checked, EditTicket, Fault, MoveTicket, Reply, Say, WorldView
from minutehand.domain.scenario import TicketState
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


class OpenWorld:
    def __init__(self, client: MinutehandClient, view: WorldView) -> None:
        self.client = client
        self.view = view
        self.world_id = view.world_id

    # -- reading ------------------------------------------------------------------------------------------------

    def events(
        self,
        *,
        provider: str | None = None,
        kind: EntityKind | None = None,
        actor: Actor | None = None,
        operation: Operation | None = None,
        since: int | None = None,
    ) -> list[WorldEvent]:
        page = self.client.events(
            self.world_id, provider=provider, kind=kind, actor=actor, operation=operation, since=since
        )
        return page.events

    def entities(self, *, provider: str | None = None, kind: EntityKind | None = None) -> list[Stored]:
        return self.client.entities(self.world_id, provider=provider, kind=kind).entities

    def calls(self) -> list[RecordedCall]:
        return self.client.calls(self.world_id).calls

    def unmatched_calls(self) -> list[RecordedCall]:
        """This world's calls to hosts no provider claims and no declaration captures, refused with 502."""
        return self.client.calls(self.world_id, unmatched=True).calls

    def captured_calls(self) -> list[RecordedCall]:
        """This world's calls to hosts declared as outbound (`CreateWorld.outbound`), captured and not refused."""
        return self.client.calls(self.world_id, captured=True).calls

    def now(self) -> datetime:
        return self.client.world(self.world_id).now

    def checks(self) -> Checked:
        return self.client.checks(self.world_id)

    # -- acting -------------------------------------------------------------------------------------------------

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
        self, *, containing: str, by: Actor = Actor.AGENT, to: str | None = None, at_least: int = 1
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
        self, *, titled: str, state: TicketState | None = None, assignee: str | None = None, at_least: int = 1
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
