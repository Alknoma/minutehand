"""The Google Workspace provider: Drive v3, Docs v1, Slides v1, Gmail v1, Calendar v3, Google's sign-in, and what
the scenario seeds of each.

It pushes no message to the agent the way Slack does, so it is not `PushesEvents`. It is `LandsAnswers`: a person's
reply to the agent's email lands in the agent's mailbox, and a guest's answer to its invitation on the event, at
their moment, where the agent finds them on its next read. Gmail pushes nothing (`users.watch`, which delivers
through Pub/Sub, answers 501), so a reply email is never heard; a guest's answer is heard when a live
`events.watch` channel watches a calendar the event is on, and landing it tells that channel's address. It is
`ChangesDocuments`: a person's change to a seeded document (a `DocumentHappening`) lands at its moment. It is
`NotifiesChanges`: when the agent has asked Drive to be told of changes (`changes.watch`), Drive tells it, the way
Drive's push notifications do. Both kinds of channel are `channels.py`'s.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

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
from minutehand.domain.items import ItemKind, TypedItem, read_as
from minutehand.domain.people import PersonReply
from minutehand.domain.provider import Manifest, fault_fragment
from minutehand.domain.scenario import DocumentHappening, Person, Scenario
from minutehand.domain.transitions import (
    REPLY,
    TEXT,
    Offer,
    OfferField,
    Transition,
    Waiting,
    answer_transition,
    answered,
    content_of,
    conversations,
    message_offers,
    transition_change,
)
from minutehand.domain.world import Actor, EntityKind, EntityRef, MessageSnapshot, RecordSnapshot, WorldEvent
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
        return self.heard_of(reply.in_reply_to, None, world, clock)

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

    # -- transitions (`ProvidesTransitions`): an email or an invitation waits on its person until they answer ------

    def items_for(self, person: Person, world: Store) -> list[Waiting]:
        """Every email the agent sent the person, and every invitation to an event of the agent's they are a guest
        of, still there: each an ask they answer, in the order sent. Then every other invitation they have not
        answered (`responseStatus` `needsAction`), not theirs and not cancelled: an item that waits on them, played
        where the scenario names Google in `transitions_on`."""
        asks = conversations(person.email, MANIFEST.key, world.events())
        mine = [w.item for w in asks]
        calendars = CalendarWorld(world)
        for event, _ in calendars.on(person.email):
            attendee = calendars.attendee(event, person.key)
            cancelled = event.status == "cancelled"  # enum-lint: exempt Calendar's own event status
            if attendee is None or cancelled or attendee.organizer or event_ref(event.id) in mine:
                continue
            if event.organizer.email.lower() == person.email.lower() or attendee.responseStatus != NEEDS_ACTION:
                continue
            shown = invitation(event)
            text = shown.text if isinstance(shown, MessageSnapshot) else ""
            asks.append(Waiting(item=event_ref(event.id), state=NEEDS_ACTION, shown=text))
        return asks

    def legal(self, item: EntityRef, by: Actor, who: Person | None, world: Store) -> list[Offer]:
        """What the person can do with it: write an email back; or, on an invitation, write a response comment, or
        answer Yes, Maybe or No, each by the `responseStatus` it sets, with a comment; the answer they already gave
        is not offered again. Nothing for anyone it is not to."""
        del by
        event = CalendarWorld(world).event(item.external_id) if item.kind is EntityKind.MESSAGE else None
        if event is None:
            asked = _asked(item, world)
            if asked is None or who is None or who.email not in asked.recipient_emails:
                return []
            return message_offers(asked)
        attendee = CalendarWorld(world).attendee(event[0], who.key) if who is not None else None
        if attendee is None:
            return []
        comment = "A note to the organizer with the answer: the response comment"
        return [
            Offer(
                name=REPLY,
                to_state=attendee.responseStatus,
                description="A response comment, the answer left as it is",
                fields=[OfferField(name=TEXT, required=True, description=comment)],
            ),
            *(
                Offer(
                    name=status,
                    to_state=status,
                    label=label,
                    description=f'"{label}" on the invitation',
                    fields=[OfferField(name=TEXT, description=comment)],
                )
                for status, label in calendar_wire.ANSWERS.items()
                if status != attendee.responseStatus
            ),
        ]

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        """The person's answer, as Google's own does: an email back in the asking mailbox; on an invitation, the
        guest's `responseStatus` and response comment on the event, recorded as theirs, and every live
        `events.watch` channel on a calendar it is on told. Recorded once as a transition, an invitation's from
        the response the guest held to the one they hold now."""
        if who is None:
            raise ValueError(f"nobody answers {item.external_id}")
        calendars = CalendarWorld(world)
        event = calendars.event(item.external_id) if item.kind is EntityKind.MESSAGE else None
        if event is None:
            asked = _asked(item, world)
            if asked is None:
                raise ValueError(f"the email {item.external_id} is gone: there is nothing to answer")
            await self.land(answered(item, asked, offer, who.key, content, clock.now()), world, clock)
            moved = answer_transition(MANIFEST.key, item, offer, by, who.key, content, clock.now())
        else:
            attendee = calendars.attendee(event[0], who.key)
            if attendee is None:
                raise ValueError(f"only a guest answers the invitation to {event[0].id}")
            found = next((o for o in self.legal(item, by, who, world) if o.name == offer), None)
            if found is None:
                raise ValueError(f"the invitation to {event[0].id} does not offer {who.key} {offer!r}")
            given = content_of(content, found, who.key)
            comment = given[TEXT] if TEXT in given and given[TEXT].strip() else None
            status = offer if offer in calendar_wire.ANSWERS else None
            await Channels(world, clock).tell_calendars(calendars.respond(event[0].id, who.key, status, comment, clock))
            moved = Transition(
                provider=MANIFEST.key,
                item=item,
                name=offer,
                from_state=attendee.responseStatus,
                to_state=status or attendee.responseStatus,
                by=by,
                who=who.key,
                content=content,
                at=clock.now(),
            )
        recorded = world.apply(transition_change(moved, at_seq=world.head() + 1))
        return moved.model_copy(update={"seq": recorded.seq})

    def heard_of(self, item: EntityRef, who: Person | None, world: Store, clock: Clock) -> bool:
        """Whether an answer to `item` is told to the agent: an invitation's, when the event is on a calendar a live
        `events.watch` channel watches. An email back never is: Gmail's `users.watch` answers 501."""
        del who
        held = world.get(item)
        if held is None:
            return False
        event = calendar_wire.KEPT.validate_json(held.body)
        if not isinstance(event, calendar_wire.StoredEvent):
            return False
        return bool(Channels(world, clock).calendars(calendars_of(event)))

    def change(self, happening: DocumentHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        DriveApi(world, clock, Channels(world, clock)).person_change(happening)

    def watched(self, world: Store, clock: Clock) -> bool:
        """Whether a live Drive channel watches: what a `DocumentHappening` is told to. A Calendar channel is told of
        a guest's answer as it lands (`heard`, `land`)."""
        return bool(Channels(world, clock).drive())

    async def notify(self, world: Store, clock: Clock) -> None:
        await DriveApi(world, clock, Channels(world, clock)).notify()

    def typed(self, event: WorldEvent, world: Store) -> TypedItem | None:
        """`TypesItems`: a document, a comment, an email, or a calendar event, each from its own records: an event's
        times and guests read from the event as that write left it."""
        kind = event.entity.kind
        if kind is EntityKind.DOCUMENT:
            return read_as(event, ItemKind.DOCUMENT)
        if kind is EntityKind.COMMENT:
            return read_as(event, ItemKind.COMMENT)
        if kind is not EntityKind.MESSAGE:
            return None
        version = next((v for v in world.versions(event.entity) if v.seq == event.seq), None)
        held = _event(version.body) if version is not None else None
        if held is None:
            # gone since, or an email: an event with no guests is logged as a record, an email as a message
            return read_as(
                event, ItemKind.CALENDAR_EVENT if isinstance(event.after, RecordSnapshot) else ItemKind.EMAIL
            )
        typed = read_as(event, ItemKind.CALENDAR_EVENT)
        return typed.model_copy(
            update={
                "people": [a.email for a in held.attendees or [] if a.email.lower() != held.organizer.email.lower()],
                "starts": _moment(held.start),
                "ends": _moment(held.end),
                "conversation": held.id,
            }
        )

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


def _asked(item: EntityRef, world: Store) -> MessageSnapshot | None:
    """The message as it reads now: an event's invitation, or an email."""
    if item.provider != MANIFEST.key or item.kind is not EntityKind.MESSAGE:
        raise ValueError(f"{item.provider} {item.kind.value} {item.external_id} is no Google message")
    event = CalendarWorld(world).event(item.external_id)
    if event is not None:
        shown = invitation(event[0])
        return shown if isinstance(shown, MessageSnapshot) else None
    found = next((e.after for e in reversed(world.events()) if e.entity == item and e.after is not None), None)
    return found if isinstance(found, MessageSnapshot) else None


def _response(event: calendar_wire.StoredEvent, who: Person, world: Store) -> str | None:
    attendee = CalendarWorld(world).attendee(event, who.key)
    return attendee.responseStatus if attendee is not None else None


def _event(body: str) -> calendar_wire.StoredEvent | None:
    """The body as Calendar's event, when it is one; an email's body is not."""
    try:
        return calendar_wire.StoredEvent.model_validate_json(body)
    except ValidationError:
        return None


def _moment(when: calendar_wire.EventTime) -> datetime | None:
    """An event's start or end as an instant; None for an all-day date, which names no time."""
    if when.dateTime is None:
        return None
    moment = datetime.fromisoformat(when.dateTime)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=ZoneInfo(when.timeZone)) if when.timeZone else None
    return moment


def build() -> GoogleWorkspaceProvider:
    return GoogleWorkspaceProvider()
