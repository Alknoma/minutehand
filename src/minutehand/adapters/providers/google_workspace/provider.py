"""The Google Workspace provider: Drive v3, Docs v1, Slides v1, Gmail v1, Calendar v3, Google's sign-in, and what
the scenario seeds of each.

It pushes no message to the agent the way Slack does, so it is not `PushesEvents`. It is `LandsReplies`: a person's
reply to the agent's email lands in the agent's mailbox, and a guest's answer to its invitation on the event, at
their moment, where the agent finds them on its next read. Gmail pushes nothing (`users.watch`, which delivers
through Pub/Sub, answers 501), so a reply email is never heard; a guest's answer is heard when a live
`events.watch` channel watches a calendar the event is on, and landing it tells that channel's address. It is
`ChangesDocuments`: a person's change to a seeded document (a `DocumentHappening`) lands at its moment. It is
`NotifiesChanges`: when the agent has asked Drive to be told of changes (`changes.watch`), Drive tells it, the way
Drive's push notifications do. Both kinds of channel are `channels.py`'s.
"""

from __future__ import annotations

from pydantic import TypeAdapter, ValidationError

from minutehand.adapters.providers.google_workspace import calendar_wire, wire
from minutehand.adapters.providers.google_workspace.app import DriveApi, build_app
from minutehand.adapters.providers.google_workspace.calendars import (
    CalendarApi,
    CalendarWorld,
    calendars_of,
    event_ref,
    invitation,
)
from minutehand.adapters.providers.google_workspace.channels import Channels
from minutehand.adapters.providers.google_workspace.gmail import GmailApi, MailWorld
from minutehand.adapters.providers.google_workspace.manifest import MANIFEST
from minutehand.adapters.providers.google_workspace.seed import WorkspaceSeed, seed, write_faults
from minutehand.adapters.providers.google_workspace.state import DriveWorld
from minutehand.domain.errors import Rendered
from minutehand.domain.people import PersonReply
from minutehand.domain.provider import Manifest, fault_fragment
from minutehand.domain.scenario import DocumentHappening, Person, Scenario
from minutehand.domain.transitions import Offer, OfferField, Transition, Waiting, transition_change
from minutehand.domain.world import Actor, EntityKind, EntityRef, MessageSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class GoogleWorkspaceProvider:
    manifest: Manifest = MANIFEST
    seed_model = WorkspaceSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        channels = Channels(world, clock)
        return build_app(DriveApi(world, clock, channels), GmailApi(world, clock), CalendarApi(world, clock, channels))

    def error(self, status: int, code: str, message: str) -> Rendered:
        return wire.error_answer(status, code, message)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    def lands(self, reply: PersonReply, world: Store) -> bool:
        """Every reply on Google Workspace lands: it pushes no message to the agent."""
        del reply, world
        return True

    def heard(self, reply: PersonReply, world: Store, clock: Clock) -> bool:
        """Whether a guest's answer lands on a calendar a live `events.watch` channel watches. A reply email is never
        heard: Gmail's `users.watch` answers 501, and the agent polls its mailbox."""
        return self.heard_of(reply.in_reply_to, world, clock)

    def heard_of(self, item: EntityRef, world: Store, clock: Clock) -> bool:
        """Whether a move of `item` (a guest's answer to an invitation) is told to the agent: the event is on a
        calendar a live `events.watch` channel watches."""
        held = world.get(item)
        if held is None:
            return False
        event = calendar_wire.KEPT.validate_json(held.body)
        if not isinstance(event, calendar_wire.StoredEvent):
            return False
        return bool(Channels(world, clock).calendars(calendars_of(event)))

    async def land(self, reply: PersonReply, world: Store, clock: Clock) -> None:
        """A reply to an email lands as the person's email in the asking mailbox; an answer to an invitation as the
        guest's response on the event."""
        held = world.get(reply.in_reply_to)
        if held is None:
            return
        if isinstance(calendar_wire.KEPT.validate_json(held.body), calendar_wire.StoredEvent):
            await Channels(world, clock).tell_calendars(CalendarWorld(world).land(reply, clock))
        else:
            MailWorld(world).land(reply, clock)

    # -- transitions (`ProvidesTransitions`): an invitation waits on each guest until they answer it ----------------

    def items_for(self, person: Person, world: Store) -> list[Waiting]:
        """Every event the person is a guest of, not its organizer, whose invitation they have not answered
        (`responseStatus` `needsAction`) and that is not cancelled, shown as its invitation reads."""
        calendars = CalendarWorld(world)
        waiting: list[Waiting] = []
        for event, _ in calendars.on(person.email):
            attendee = calendars.attendee(event, person.key)
            cancelled = event.status == "cancelled"  # enum-lint: exempt Calendar's own event status
            if attendee is None or cancelled or attendee.organizer:
                continue
            if event.organizer.email.lower() == person.email.lower() or attendee.responseStatus != NEEDS_ACTION:
                continue
            shown = invitation(event)
            text = shown.text if isinstance(shown, MessageSnapshot) else ""
            waiting.append(Waiting(item=event_ref(event.id), state=NEEDS_ACTION, shown=text))
        return waiting

    def legal(self, item: EntityRef, by: Actor, who: Person | None, world: Store) -> list[Offer]:
        """What the invitation offers its guest: Yes, Maybe and No, each by the `responseStatus` it sets, with a
        response comment; the one they already gave is not offered again. Nothing for anyone not on it."""
        del by
        event = _event(item, world)
        calendars = CalendarWorld(world)
        attendee = calendars.attendee(event, who.key) if who is not None else None
        if attendee is None:
            return []
        return [
            Offer(
                name=status,
                to_state=status,
                description=f'"{label}" on the invitation',
                fields=[OfferField(name=COMMENT, description="A note to the organizer with the answer")],
            )
            for status, label in calendar_wire.ANSWERS.items()
            if status != attendee.responseStatus
        ]

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        """The guest answers the invitation as Calendar's own does: their `responseStatus` and comment on the event,
        recorded as theirs, and every live `events.watch` channel on a calendar it is on told."""
        event = _event(item, world)
        calendars = CalendarWorld(world)
        attendee = calendars.attendee(event, who.key) if who is not None else None
        if who is None or attendee is None:
            raise ValueError(f"only a guest answers the invitation to {event.id}")
        if offer not in calendar_wire.ANSWERS or offer == attendee.responseStatus:
            raise ValueError(f"the invitation to {event.id} does not offer {who.key} {offer!r}")
        given = _content(content)
        unknown = sorted(set(given) - {COMMENT})
        if unknown:
            raise ValueError(f"an answer to an invitation takes a comment, not {', '.join(unknown)}")
        comment = given[COMMENT] if COMMENT in given and given[COMMENT].strip() else None
        changed = calendars.respond(event.id, who.key, offer, comment, clock)
        recorded = world.apply(
            transition_change(
                transition := Transition(
                    provider=MANIFEST.key,
                    item=item,
                    name=offer,
                    from_state=attendee.responseStatus,
                    to_state=offer,
                    by=by,
                    who=who.key,
                    content=content,
                    at=clock.now(),
                ),
                at_seq=world.head() + 1,
            )
        )
        await Channels(world, clock).tell_calendars(changed)
        return transition.model_copy(update={"seq": recorded.seq})

    def change(self, happening: DocumentHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        DriveApi(world, clock, Channels(world, clock)).person_change(happening)

    def watched(self, world: Store, clock: Clock) -> bool:
        """Whether a live Drive channel watches: what a `DocumentHappening` is told to. A Calendar channel is told of
        a guest's answer as it lands (`heard`, `land`)."""
        return bool(Channels(world, clock).drive())

    async def notify(self, world: Store, clock: Clock) -> None:
        await DriveApi(world, clock, Channels(world, clock)).notify()

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`WorkspaceSeed.faults`, on a world already open."""
        write_faults(
            DriveWorld(world),
            fault_fragment(WorkspaceSeed, faults, frozenset({"faults"})).faults,
            clock.now(),
            declared=True,
        )


NEEDS_ACTION = "needsAction"
"""An attendee who has not answered: Calendar's own `responseStatus`."""
COMMENT = "comment"
"""What a guest's answer takes besides the answer: Calendar's attendee `comment`."""

_CONTENT: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])


def _content(content: str) -> dict[str, str]:
    try:
        return _CONTENT.validate_json(content)
    except ValidationError as e:
        raise ValueError(f"a transition's content is a JSON object of text fields: {content!r}") from e


def _event(item: EntityRef, world: Store) -> calendar_wire.StoredEvent:
    if item.provider != MANIFEST.key or item.kind is not EntityKind.MESSAGE:
        raise ValueError(f"{item.provider} {item.kind.value} {item.external_id} is not a Calendar event")
    found = CalendarWorld(world).event(item.external_id)
    if found is None:
        raise LookupError(f"no Calendar event {item.external_id} in this run")
    return found[0]


def build() -> GoogleWorkspaceProvider:
    return GoogleWorkspaceProvider()
