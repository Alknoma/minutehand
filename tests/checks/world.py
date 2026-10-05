"""A small hand-built world for check tests: people, a log of events, and the view over them.

Times are hours after `START`. Events are WorldEvents as a provider would record
them; the obligations come from the real ledger, never written by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from minutehand.checks.ledger import build
from minutehand.domain.checks import RunView, WakeRecord
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import (
    Absence,
    Answers,
    DelayRange,
    Expectation,
    Person,
    ReplyBehaviour,
    Scenario,
    TicketFate,
    TicketState,
)
from minutehand.domain.world import (
    Actor,
    DocumentSnapshot,
    EntityKind,
    EntityRef,
    Exchange,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
    Snapshot,
    TicketSnapshot,
    WorldEvent,
)

START = datetime(2026, 9, 7, 9, tzinfo=UTC)
QUICK = Answers(delay=DelayRange(shortest=timedelta(hours=1), longest=timedelta(hours=2)))


def at(hours: float) -> datetime:
    return START + timedelta(hours=hours)


def person(key: str, reply: ReplyBehaviour = QUICK, absences: list[Absence] | None = None) -> Person:
    return Person(key=key, name=key.title(), email=f"{key}@example.com", reply=reply, absences=absences or [])


def scenario(
    *people: Person,
    deadline_after: timedelta | None = None,
    fates: list[TicketFate] | None = None,
    expect: list[Expectation] | None = None,
) -> Scenario:
    return Scenario(
        name="hand_built",
        goal="Get the contract signed.",
        owner=people[0].key,
        starts_at=START,
        deadline_after=deadline_after,
        people=list(people),
        ticket_fates=fates or [],
        expect=expect or [],
    )


class Log:
    """The run's events in the order they happened."""

    def __init__(self) -> None:
        self.events: list[WorldEvent] = []

    def _add(
        self,
        hours: float,
        actor: Actor,
        operation: Operation,
        entity: EntityRef,
        after: Snapshot | None,
        wake: int,
        wall: datetime | None = None,
    ) -> WorldEvent:
        moment = at(hours)
        event = WorldEvent(
            seq=len(self.events) + 1,
            run_id="hand",
            wake=wake,
            sim_time=moment,
            wall_time=wall or moment,
            actor=actor,
            operation=operation,
            entity=entity,
            after=after,
        )
        self.events.append(event)
        return event

    def message(
        self,
        to: list[Person],
        hours: float,
        text: str = "Could you send the signed contract?",
        *,
        actor: Actor = Actor.AGENT,
        channel: str = "dm",
        thread_of: str | None = None,
        wake: int = 1,
        wall: datetime | None = None,
    ) -> WorldEvent:
        ref = EntityRef(provider="chat", kind=EntityKind.MESSAGE, external_id=f"m{len(self.events) + 1}")
        snapshot = MessageSnapshot(
            text=text,
            channel=channel,
            recipient_emails=[p.email for p in to if p.email is not None],
            recipients=[p.key for p in to],
            thread_of=thread_of,
        )
        return self._add(hours, actor, Operation.CREATE, ref, snapshot, wake, wall)

    def email(
        self, to: list[Person], hours: float, text: str = "The contract is with legal.", *, wake: int = 1
    ) -> WorldEvent:
        """A send through a captured host (`adapters.proxy.capture`): a message nobody can answer where it went."""
        ref = EntityRef(provider="mail", kind=EntityKind.MESSAGE, external_id=f"e{len(self.events) + 1}")
        addresses = [p.email for p in to if p.email is not None]
        channel = "to:" + ",".join(sorted(addresses))
        snapshot = MessageSnapshot(
            text=text, channel=channel, recipient_emails=addresses, recipients=[p.key for p in to], answerable=False
        )
        return self._add(hours, Actor.AGENT, Operation.CREATE, ref, snapshot, wake)

    def ticket(
        self,
        title: str,
        assignee: Person | None,
        hours: float,
        *,
        project: str = "Legal",
        actor: Actor = Actor.AGENT,
        operation: Operation = Operation.CREATE,
        state: TicketState = TicketState.OPEN,
        external_id: str | None = None,
        wake: int = 1,
    ) -> WorldEvent:
        ref = EntityRef(
            provider="tracker", kind=EntityKind.TICKET, external_id=external_id or f"t{len(self.events) + 1}"
        )
        snapshot = TicketSnapshot(
            title=title,
            project=project,
            state=state,
            assignee_email=assignee.email if assignee else None,
            assignee=assignee.key if assignee else None,
        )
        return self._add(hours, actor, operation, ref, snapshot, wake)

    def record(self, resource: str, text: str, hours: float, *, wake: int = 1) -> WorldEvent:
        """An agent write to a resource nobody has mapped to a ticket, message or document."""
        ref = EntityRef(provider="generated", kind=EntityKind.RECORD, external_id=f"r{len(self.events) + 1}")
        return self._add(hours, Actor.AGENT, Operation.CREATE, ref, RecordSnapshot(resource=resource, text=text), wake)

    def document(self, title: str, text: str | None, hours: float, *, wake: int = 1) -> WorldEvent:
        """An agent write to a document, with its content flattened to text when its provider gives it."""
        ref = EntityRef(provider="docs", kind=EntityKind.DOCUMENT, external_id=f"d{len(self.events) + 1}")
        return self._add(hours, Actor.AGENT, Operation.UPDATE, ref, DocumentSnapshot(title=title, text=text), wake)

    def edit(self, message: WorldEvent, hours: float, text: str, *, wake: int = 1) -> WorldEvent:
        """The agent rewrites a message it sent, in place."""
        assert isinstance(message.after, MessageSnapshot)
        snapshot = message.after.model_copy(update={"text": text})
        return self._add(hours, Actor.AGENT, Operation.UPDATE, message.entity, snapshot, wake)

    def read(self, entity: EntityRef, hours: float, *, wake: int = 1) -> WorldEvent:
        return self._add(hours, Actor.AGENT, Operation.READ, entity, None, wake)


def reply(who: Person, to: WorldEvent, hours: float) -> PersonReply:
    return PersonReply(person=who.key, in_reply_to=to.entity, text="Here it is.", at=at(hours))


def view(
    world: Scenario,
    log: Log,
    replies: list[PersonReply] | None = None,
    *,
    wakes: list[WakeRecord] | None = None,
    unmatched: list[Exchange] | None = None,
) -> RunView:
    return RunView(
        scenario=world,
        events=log.events,
        wakes=wakes or [],
        obligations=build(world, log.events, replies or []),
        replies=replies or [],
        unmatched_calls=unmatched,
    )
