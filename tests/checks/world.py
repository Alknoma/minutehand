"""A small hand-built world for check tests: people, a log of events, and the view over them.

Times are hours after `START`. Events are WorldEvents as a provider would record
them; the obligations come from the real ledger, never written by hand.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime, timedelta

import yaml
from pydantic import TypeAdapter

from minutehand.checks.ledger import build
from minutehand.domain.assessments import Rule, StoppedBy
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
    TicketState,
)
from minutehand.domain.world import (
    Actor,
    DocumentSnapshot,
    EntityKind,
    EntityRef,
    Exchange,
    MemorySnapshot,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
    Snapshot,
    StoredSnapshot,
    TicketSnapshot,
    TransitionSnapshot,
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
    expect: list[Expectation] | None = None,
) -> Scenario:
    return Scenario(
        name="hand_built",
        goal="Get the contract signed.",
        owner=people[0].key,
        starts_at=START,
        deadline_after=deadline_after,
        people=list(people),
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
            text=text, channel=channel, recipient_emails=[p.email for p in to], thread_of=thread_of
        )
        return self._add(hours, actor, Operation.CREATE, ref, snapshot, wake, wall)

    def email(
        self, to: list[Person], hours: float, text: str = "The contract is with legal.", *, wake: int = 1
    ) -> WorldEvent:
        """A send through a captured host (`adapters.proxy.capture`): a message nobody can answer where it went."""
        ref = EntityRef(provider="mail", kind=EntityKind.MESSAGE, external_id=f"e{len(self.events) + 1}")
        channel = "to:" + ",".join(sorted(p.email for p in to))
        snapshot = MessageSnapshot(text=text, channel=channel, recipient_emails=[p.email for p in to], answerable=False)
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
            title=title, project=project, state=state, assignee_email=assignee.email if assignee else None
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

    def transition(
        self,
        provider: str,
        item: str,
        hours: float,
        to: str,
        *,
        from_state: str | None = None,
        name: str | None = None,
        actor: Actor = Actor.AGENT,
        who: Person | None = None,
        kind: EntityKind = EntityKind.TICKET,
        wake: int = 1,
    ) -> WorldEvent:
        """One move of an item's state, recorded as a provider records it (`domain.transitions.transition_change`)."""
        moved = EntityRef(provider=provider, kind=kind, external_id=item)
        ref = EntityRef(provider=provider, kind=EntityKind.TRANSITION, external_id=f"{item}@{len(self.events) + 1}")
        snapshot = TransitionSnapshot(
            item=moved,
            name=name or to,
            from_state=from_state,
            to_state=to,
            who=who.key if who is not None else None,
            content="{}",
        )
        return self._add(hours, actor, Operation.CREATE, ref, snapshot, wake)

    def read(self, entity: EntityRef, hours: float, *, wake: int = 1) -> WorldEvent:
        return self._add(hours, Actor.AGENT, Operation.READ, entity, None, wake)

    def memory(self, key: str, value: object | None, hours: float, *, wake: int = 1) -> WorldEvent:
        """The agent writes one key of its memory (`minutehand.agent.store`); None deletes it."""
        ref = EntityRef(provider="memory", kind=EntityKind.MEMORY, external_id=f"default/{key}")
        operation = Operation.DELETE if value is None else Operation.UPDATE
        text = None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))
        return self._add(
            hours, Actor.AGENT, operation, ref, MemorySnapshot(collection="default", key=key, value=text), wake
        )

    def stored(
        self, collection: str, item_id: str, item: object | None, hours: float, *, host: str = "api.crm.test"
    ) -> WorldEvent:
        """The agent writes one item of a collection a `store` host keeps; None deletes it."""
        path = f"/v1/{collection}"
        ref = EntityRef(provider="crm", kind=EntityKind.STORED, external_id=f"{path}/{item_id}")
        earlier = [e for e in self.events if e.entity == ref and e.operation is not Operation.DELETE]
        operation = Operation.DELETE if item is None else Operation.UPDATE if earlier else Operation.CREATE
        text = None if item is None else json.dumps(item)
        snapshot = StoredSnapshot(host=host, collection=collection, path=path, id=item_id, item=text)
        return self._add(hours, Actor.AGENT, operation, ref, snapshot, 1)


def reply(who: Person, to: WorldEvent, hours: float) -> PersonReply:
    return PersonReply(person=who.key, in_reply_to=to.entity, text="Here it is.", at=at(hours))


ONE_WAKE = WakeRecord(index=1, sim_time=START, world_changes=1, commitments_changed=True)
"""What a hand-built view stands in for is a played run, and a played run has its wakes: one that did something,
so no check is blocked for want of one and none reads it as idle."""


_RULES: TypeAdapter[list[Rule]] = TypeAdapter(list[Rule])


def rules(written: str) -> list[Rule]:
    """Rules as a team writes them in YAML: the list under `assess:`."""
    return _RULES.validate_python(yaml.safe_load(textwrap.dedent(written)))


def view(
    world: Scenario,
    log: Log,
    replies: list[PersonReply] | None = None,
    *,
    wakes: list[WakeRecord] | None = None,
    unmatched: list[Exchange] | None = None,
    assess: list[Rule] | None = None,
    stopped: StoppedBy | None = None,
) -> RunView:
    return RunView(
        scenario=world,
        events=log.events,
        wakes=wakes if wakes is not None else [ONE_WAKE],
        obligations=build(world, log.events, replies or []),
        replies=replies or [],
        unmatched_calls=unmatched,
        rules=assess or [],
        stopped=stopped,
    )
