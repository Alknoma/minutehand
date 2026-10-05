"""What a provider is. Two protocols, because most real services push nothing, and
a method that returns nothing on their behalf would be a stub.

The optional ports are runtime-checkable, so whoever builds a run can hold a provider its manifest says
pushes events or books wakes to the port it claims, and refuse it loudly when it does not implement it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Protocol, runtime_checkable

from minutehand.domain.clock import Due
from minutehand.domain.people import InboundTarget, PersonMessage, PersonReply
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Happening, Scenario, TicketState
from minutehand.domain.world import EntityRef
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

Scope = MutableMapping[str, object]
Message = MutableMapping[str, object]
ASGIApp = Callable[[Scope, Callable[[], Awaitable[Message]], Callable[[Message], Awaitable[None]]], Awaitable[None]]
"""The fake API, as an ASGI application. A WSGI app is wrapped with asgiref's WsgiToAsgi."""


class Provider(Protocol):
    manifest: Manifest

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        """The service's API. It reads and writes only through `world` and stamps only from `clock`."""
        ...

    def seed(self, scenario: Scenario, world: Store) -> None:
        """Write the scenario's people, tickets and documents for this service, as actor SCENARIO."""
        ...


@runtime_checkable
class PushesEvents(Protocol):
    """A provider whose real service calls the agent: Slack events, Teams activities, webhooks."""

    async def deliver(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        """Record the reply in the world and push it to the agent the way the real service would, signed with
        `secret`: the value `target.secret` resolved to for this run."""
        ...

    async def say(
        self, message: PersonMessage, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        """The person messages the agent directly (in Slack, a DM to its bot), recorded as actor PERSON and
        pushed like any event, signed with `secret`. How a goal or a direction sent by message reaches the agent."""
        ...

    async def happen(
        self, happening: Happening, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        """Something a person does unprompted at a moment the scenario sets (posts, edits, deletes, reacts, joins a
        channel, opens the agent's page, runs one of its commands), recorded as actor PERSON and pushed as the real
        service pushes it. A happening this provider has no such thing for is refused loudly."""
        ...


@runtime_checkable
class PushesInteractions(Protocol):
    """A provider whose messages carry controls a person can use (Slack's buttons, Teams' card actions), and whose
    real service tells the agent when one is used."""

    async def press(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        """The person uses `reply.press` on the message `reply.in_reply_to`: recorded as actor PERSON, pushed to
        `target`'s interactivity URL signed with `secret`, and the agent's answer applied as the real service applies
        it. A form the agent opens in answer is filled with `reply.press.form` and submitted the same way. A
        reply with no press, or a control the message does not carry, is refused loudly."""
        ...


@runtime_checkable
class HoldsTickets(Protocol):
    """A provider with tickets a person can finish or cancel. This is how a `TicketFate` lands."""

    def transition(self, ticket: EntityRef, to: TicketState, world: Store, clock: Clock) -> None:
        """Move the ticket to `to` the way its assignee would, recorded as actor PERSON."""
        ...


@runtime_checkable
class EditsTickets(Protocol):
    """A provider whose tickets the scenario can rewrite: how a fork's `TicketEdit` lands."""

    def edit(
        self, ticket: EntityRef, *, state: TicketState | None, assignee_email: str | None, world: Store, clock: Clock
    ) -> None:
        """Change the ticket's state and/or assignee, recorded as actor SCENARIO. None leaves a field as it is."""
        ...


class Wakes(Protocol):
    """Where a scheduler provider registers what the agent booked. The orchestrator implements it."""

    def book(self, due: Due) -> None:
        """Book a wake. A booking already pending under the same `ref` is replaced."""
        ...

    def cancel(self, ref: str) -> None:
        """Drop the pending booking with this `ref`; no error when there is none."""
        ...


@runtime_checkable
class BooksWakes(Protocol):
    """A provider that is a scheduler: what the agent books here becomes a wake.

    The booking is in the run's log; what it delivers to (a queue) need not be, so a fork whose checkpoint
    holds a pending booking is refused (`application.rewind`).
    """

    def bind(self, wakes: Wakes) -> None:
        """Called once before the run; the provider books through `wakes` as the agent's calls arrive."""
        ...

    async def fire(self, ref: str, world: Store, clock: Clock) -> None:
        """The clock reached a booking: deliver it the way the real scheduler would.

        The booking has already left the pending set when this is called, so a recurring
        schedule books its next occurrence under the same `ref` from here.
        """
        ...
