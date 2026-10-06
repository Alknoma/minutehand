"""What a provider is. Two protocols, because most real services push nothing, and
a method that returns nothing on their behalf would be a stub.

The optional ports are runtime-checkable, so whoever builds a run can hold a provider its manifest says
pushes events or books wakes to the port it claims, and refuse it loudly when it does not implement it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from typing import Protocol, runtime_checkable

from minutehand.domain.clock import Due
from minutehand.domain.errors import Rendered
from minutehand.domain.people import (
    InboundCredential,
    InboundCredentialAsk,
    InboundTarget,
    PermissionGrant,
    PersonMessage,
    PersonReply,
)
from minutehand.domain.provider import Manifest, PersonChange
from minutehand.domain.scenario import (
    DocumentHappening,
    MessagingHappening,
    Model,
    Person,
    Scenario,
    TicketHappening,
    TicketState,
)
from minutehand.domain.world import Change, EntityRef
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
class RendersErrors(Protocol):
    """A provider whose service has an error shape for what Minutehand answers in its place: an operation the fake
    does not implement (501) and Minutehand's own error (500), rendered so the agent's client library raises its
    own error type with `message` intact. A provider without it is answered in a plain JSON body."""

    def error(self, status: int, code: str, message: str) -> Rendered:
        """The vendor's error answer for `status`, carrying `code` and `message` where its clients read them."""
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
        self, happening: MessagingHappening, target: InboundTarget, world: Store, clock: Clock, *, secret: str
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
class DeletesTickets(Protocol):
    """A provider whose tickets a person can delete: how a `TicketFate` that deletes lands, and how a person deletes
    a ticket the agent filed, in a world already open."""

    def delete_ticket(self, ticket: EntityRef, world: Store, clock: Clock) -> None:
        """Delete the ticket the way its assignee would (or, unassigned, the scenario's owner), recorded as actor
        PERSON; afterwards the service answers for it as for a ticket that never was. A ticket already gone
        raises `LookupError`."""
        ...


@runtime_checkable
class EditsTickets(Protocol):
    """A provider whose tickets the scenario can rewrite: how a fork's `TicketEdit` lands."""

    def edit(
        self, ticket: EntityRef, *, state: TicketState | None, assignee_email: str | None, world: Store, clock: Clock
    ) -> None:
        """Change the ticket's state and/or assignee, recorded as actor SCENARIO. None leaves a field as it is."""
        ...


@runtime_checkable
class ActsOnTickets(Protocol):
    """A provider whose seeded tickets people act on by themselves: how a `TicketHappening` lands."""

    def act(self, happening: TicketHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        """Do what the happening says to the seeded ticket it names (`Scenario.happening_ticket`), as its person
        would, recorded as actor PERSON. A ticket no longer there (the agent deleted it) is left alone and
        nothing is written: the person finds nothing to act on. An action the provider cannot express raises:
        the scenario asked for something this world cannot show."""
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

    async def deliver_booking(self, ref: str, world: Store, clock: Clock) -> None:
        """Deliver this occurrence of the booking the way the real scheduler would: put what it carries where the
        agent finds it. Called once when the clock reaches it, and again when the scenario delivers it twice
        (`DispatchFault.TWICE`); it leaves the booking as it was, so the second delivery carries the same."""
        ...

    async def advance_booking(self, ref: str, world: Store, clock: Clock) -> None:
        """This occurrence is over, delivered or dropped: book the next occurrence of a recurring schedule under the
        same `ref` (the first after now, as a real scheduler skips what it missed), or complete a one-off one. The
        booking has already left the pending set when this is called."""
        ...


@runtime_checkable
class ChangesDocuments(Protocol):
    """A provider whose seeded documents people change by themselves: how a `DocumentHappening` lands."""

    def change(self, happening: DocumentHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        """Do what the happening says to the seeded document it names (`Scenario.happening_document`), as its
        person would, recorded as actor PERSON. A document no longer there (the agent deleted it) is left alone
        and nothing is written."""
        ...


@runtime_checkable
class DeclaresFaults(Protocol):
    """A provider whose deliberate failures, typed in its own seed model, can be declared on a world already open
    (`minutehand serve`), as a scenario declares them before it starts."""

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`faults` is a fragment of this provider's own seed model as JSON text that sets only the fields that
        declare faults. Validate it as seeding does, and refuse loudly (`ValueError`) a fragment that sets anything
        else or names what the world does not hold; record each fault after those already recorded, as actor
        SCENARIO, as seeding records it, with its offsets counted from `clock.now()`."""
        ...


@runtime_checkable
class NotifiesChanges(Protocol):
    """A provider the agent can ask to be told when its documents change (Drive's `changes.watch`): how a
    `DocumentHappening` becomes a wake."""

    def watched(self, world: Store, clock: Clock) -> bool:
        """Whether the agent has asked to be told of changes and has not stopped asking."""
        ...

    async def notify(self, world: Store, clock: Clock) -> None:
        """Tell the agent, the way the real service would, of every change it has not been told of."""
        ...


@runtime_checkable
class PlacesAdditions(Protocol):
    """A provider whose seeding numbers things within a container the world may have added to since it was seeded:
    a Jira or YouTrack project's issue numbers (`LAUNCH-3`). A further seed of an open world works out what the
    grown scenario seeds from the scenario alone, so a ticket added to a project the agent has filed in since would
    take a number the world has already handed out; the provider moves it to the next free one."""

    def place(self, additions: Sequence[Change], world: Store) -> list[Change]:
        """`additions`, in the order seeding wrote them, renumbered so none takes a number `world` has handed out,
        and everything among them that names a renumbered thing renamed with it. Raises ValueError for what it
        cannot place."""
        ...


@runtime_checkable
class DeliversInBackground(Protocol):
    """A provider's app (the object `Provider.app` answers) that pushes to the agent after the call that set it
    off has been answered: Notion's webhooks, Drive's channel notifications. A standing world is not quiet while
    one of these still awaits the agent's answer."""

    def delivering(self) -> int:
        """How many deliveries it has started whose answer from the agent has not come back yet."""
        ...


@runtime_checkable
class ConfirmsDelivery(Protocol):
    """A scheduler that can tell whether the agent has taken what a booking delivered (an SQS message deleted, a
    queued job acknowledged), which an agent does after acting on it: the run waits for that before it moves past
    the booking's wake. A delivery merely received is not taken; the agent may still be acting on it."""

    def taken(self, ref: str, world: Store) -> bool: ...


@runtime_checkable
class ChangesPeople(Protocol):
    """A provider whose accounts change while a world is open (`minutehand serve`): a person removed, deactivated or
    reactivated, each one of `manifest.people_changes`."""

    def change_person(self, change: PersonChange, person: Person, world: Store, clock: Clock) -> None:
        """Do `change` to the account `person` was seeded as, recorded as actor SCENARIO, so the service answers
        for it as the real one does after an administrator did it. Never asked for a change outside
        `manifest.people_changes`. An account it holds nothing of, or one already so, raises `ValueError`."""
        ...


@runtime_checkable
class GrantsPermissions(Protocol):
    """A provider whose permissions are named and held per person and project, and can change while a world is
    open: YouTrack's `jetbrains.youtrack.*`."""

    def permit(self, grant: PermissionGrant, person: Person, world: Store, clock: Clock) -> None:
        """Grant or withhold `grant.permission` for `person` on `grant.project`, recorded as actor SCENARIO, as an
        administrator would; the next call that needs it is answered accordingly. A permission or project the
        provider does not know raises `ValueError`."""
        ...


@runtime_checkable
class MintsInboundCredentials(Protocol):
    """A provider that pushes requests to the agent, and can sign one a test builds itself the way it signs its
    own: Slack's request signature, the Bot Framework's bearer token."""

    def credential(self, asked: InboundCredentialAsk, world: Store, clock: Clock, *, secret: str) -> InboundCredential:
        """The headers the provider's service would send with this request, signed with `secret` where its scheme
        uses a shared secret. A request missing what the scheme needs raises `ValueError`."""
        ...


@runtime_checkable
class OwnsSeed(Protocol):
    """A provider with a seed model of its own, which a scenario gives as its `ProviderSeed` for that provider:
    what a further seed's fragment for it is validated and merged with."""

    seed_model: type[Model]
