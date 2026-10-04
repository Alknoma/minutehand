"""What a provider is. Two protocols, because most real services push nothing, and
a method that returns nothing on their behalf would be a stub."""

from __future__ import annotations

from typing import Awaitable, Callable, MutableMapping, Protocol

from minutehand.domain.clock import Due
from minutehand.domain.people import InboundTarget, PersonReply
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Scenario, TicketState
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


class PushesEvents(Protocol):
    """A provider whose real service calls the agent: Slack events, Teams activities, webhooks."""

    async def deliver(self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock) -> None:
        """Record the reply in the world and push it to the agent the way the real service would."""
        ...


class HoldsTickets(Protocol):
    """A provider with tickets a person can finish or cancel. This is how a `TicketFate` lands."""

    def transition(self, ticket: EntityRef, to: TicketState, world: Store, clock: Clock) -> None:
        """Move the ticket to `to` the way its assignee would, recorded as actor PERSON."""
        ...


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


class BooksWakes(Protocol):
    """A provider that is a scheduler: what the agent books here becomes a wake."""

    def bind(self, wakes: Wakes) -> None:
        """Called once before the run; the provider books through `wakes` as the agent's calls arrive."""
        ...

    async def fire(self, ref: str, world: Store, clock: Clock) -> None:
        """The clock reached a booking: deliver it the way the real scheduler would.

        The booking has already left the pending set when this is called, so a recurring
        schedule books its next occurrence under the same `ref` from here.
        """
        ...
