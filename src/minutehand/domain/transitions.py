"""Transitions: everything a person, the agent, a system or time does to the world, as one move of one item's state
(`docs/design-transitions.md`).

A provider that implements `ports.transitions.ProvidesTransitions` says what waits on a person (`Waiting`), what
anyone may do to an item now (`Offer`), and takes a move through its own code path, recording it once in the run's
log as an event of `EntityKind.TRANSITION` beside its own entity versions (`transition_change`). Minutehand states
it; the team's rules judge it (`count: {transitions: ...}`, `each: transition`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from pydantic import AwareDatetime, Field, TypeAdapter, ValidationError

from minutehand.domain.people import PersonReply, Press, Writing
from minutehand.domain.scenario import Comments, Deletes, FormInput, Model, Moves, ProviderKey, Reassigns, TicketState
from minutehand.domain.world import (
    Actor,
    Change,
    ControlKind,
    DocumentSnapshot,
    EntityKind,
    EntityRef,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
    ServiceEventSnapshot,
    StoredSnapshot,
    TicketSnapshot,
    TransitionSnapshot,
    WorldEvent,
)

AWAITING = "awaiting"
"""A conversation's state while the person owes its answer."""
REPLIED = "replied"
"""A conversation's state once the person answered it."""
REPLY = "reply"
"""The move that answers a conversation in words."""
AUTOMATIC_REPLY = "automatic reply"
"""A person's automatic reply while away: no answer, so the conversation still awaits them. Never offered."""
TEXT = "text"
"""What a conversation's answer carries: the words written back, or what a form holds."""
EXISTS = "exists"
"""The state of an item with no state of its own (a message, a document, a stored record) once it is written."""
DELETED = "deleted"
"""The state of any item once it is deleted."""
WORLD_ITEMS = frozenset(
    {
        EntityKind.MESSAGE,
        EntityKind.TICKET,
        EntityKind.COMMENT,
        EntityKind.DOCUMENT,
        EntityKind.CHANNEL,
        EntityKind.RECORD,
        EntityKind.INBOX_ITEM,
        EntityKind.STORED,
        EntityKind.SERVICE_EVENT,
    }
)
"""The kinds of entity that are items in the world, whose writes are moves. Not: the run's own bookkeeping (what is
due, what is pending on a person, a declared service's records and its item as each transition left it, which the
transition itself records), the agent's own memory and next wake, files on its machine, its tool calls, and pushes."""
PICKS = "picks"
"""What a control that picks a person carries: the person's key."""
FORM = "form"
"""What a control that opens a form carries: what is typed into it, as a JSON list of `FormInput`."""
COMMENT = "comment"
"""A ticket's comment: the offer that adds one without moving it, and the field its words fill beside a move."""
REASSIGN = "reassign"
"""The offer that hands a ticket to someone else, or to nobody."""
ASSIGNEE = "assignee"
"""What a reassignment carries: the person's email address, or nothing for nobody."""
DELETE = "delete"
"""The offer that deletes a ticket."""
DELETED = "deleted"
"""A deleted ticket's state."""


class OfferField(Model):
    """One thing an offer takes besides choosing it: a comment, a reason, a field the service's screen asks for."""

    name: str = Field(min_length=1, description="The provider's own name for it")
    required: bool = False
    description: str = Field(description="What it is, as the person is shown it")


class Offer(Model):
    """A transition an actor may take on an item now, by the provider's own semantics: a Jira workflow transition
    from the issue's status, an attendee's response, a conversation's reply."""

    name: str = Field(min_length=1, description="The provider's own name for it: 'Start Progress', 'accepted'")
    to_state: str = Field(description="The state the item is in after it, in the provider's own words")
    fields: list[OfferField] = Field(default=[], description="What it takes, in the order the person is shown it")
    description: str | None = Field(default=None, description="What it does, as the service shows it")
    label: str | None = Field(
        default=None, description="What the person sees and uses: a button's label, an invitation's 'No'"
    )
    means: TicketState | None = Field(
        default=None,
        description="For a ticket, what the state it reaches is in Minutehand's terms: open, done, cancelled",
    )
    note: bool = Field(
        default=False,
        description="Words left on the item that leave it as it is, its first field theirs: where an automatic reply "
        "goes on an item that is no message",
    )
    unprompted: bool = Field(
        default=True,
        description="Whether a person may take it with nothing pinning it: False for what only the scenario has "
        "them do (delete a ticket, hand it to someone else), never a model's pick",
    )

    def field(self, name: str) -> OfferField | None:
        return next((f for f in self.fields if f.name == name), None)

    def refuse_content(self, given: dict[str, str], who: str) -> None:
        """Refuse what fills a field this offer does not take, or leaves out one it requires."""
        unknown = sorted(set(given) - {f.name for f in self.fields})
        if unknown:
            raise ValueError(f"{who} gave {', '.join(unknown)} with {self.name!r}, which takes no such field")
        missing = [f.name for f in self.fields if f.required and not (given[f.name] if f.name in given else "").strip()]
        if missing:
            raise ValueError(f"{who} took {self.name!r} without {', '.join(missing)}, which it requires")


class Waiting(Model):
    """An item that waits on a person in a provider now: assigned, invited, addressed, requested."""

    item: EntityRef
    state: str = Field(description="Its state as it bears on the person, in the provider's own words")
    shown: str = Field(description="The item as the person sees it in the service, in plain text")
    conversation: bool = Field(
        default=False,
        description="An ask the person answers in words: a message to them, an item in the agent's own product. "
        "Their script and voice say what they answer (`application.replier`); a pinned take wins over both",
    )


class Transition(Model):
    """One move of one item's state, by whoever made it, recorded once in the run's log."""

    provider: ProviderKey
    item: EntityRef = Field(description="The ticket, invitation, conversation or request it moved")
    name: str = Field(description="The provider's own name for it: 'Start Progress', 'accepted', 'reply'")
    from_state: str | None = Field(description="None when the transition creates the item")
    to_state: str
    by: Actor
    who: str | None = Field(description="A person's key; None for the agent, the scenario or time")
    content: str = Field(default="{}", description="What it carried, as a JSON object: a comment, the reasons")
    at: AwareDatetime
    seq: int | None = Field(default=None, description="The event it was recorded as; None before it is")
    wake: int = Field(default=0, ge=0, description="The wake it was recorded in; 0 is setup, or not yet recorded")

    @classmethod
    def of(cls, event: WorldEvent) -> Transition:
        """The transition an event of `EntityKind.TRANSITION` records."""
        after = event.after
        if not isinstance(after, TransitionSnapshot):
            raise ValueError(f"event {event.seq} records no transition")
        return cls(
            provider=event.entity.provider,
            item=after.item,
            name=after.name,
            from_state=after.from_state,
            to_state=after.to_state,
            by=event.actor,
            who=after.who,
            content=after.content,
            at=event.sim_time,
            seq=event.seq,
            wake=event.wake,
        )


def item_parent(item: EntityRef) -> str:
    """What an item's transitions are listed under, by `Store.children`."""
    return f"{item.kind.value}:{item.external_id}"


def transition_change(transition: Transition, *, at_seq: int) -> Change:
    """The change that records `transition` in the log, as the event at `at_seq` (the head of the log plus one, the
    seq it will take): one entity per transition, listed under its item, its own id ordering it among them."""
    return Change(
        entity=EntityRef(
            provider=transition.provider,
            kind=EntityKind.TRANSITION,
            external_id=f"{item_parent(transition.item)}@{at_seq:010d}",
        ),
        operation=Operation.CREATE,
        actor=transition.by,
        body=transition.model_dump_json(exclude={"seq"}),
        parent=item_parent(transition.item),
        after=TransitionSnapshot(
            item=transition.item,
            name=transition.name,
            from_state=transition.from_state,
            to_state=transition.to_state,
            who=transition.who,
            content=transition.content,
        ),
    )


_TEXTS: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])
_FORM: TypeAdapter[list[FormInput]] = TypeAdapter(list[FormInput])


def content_of(content: str, offer: Offer, who: str) -> dict[str, str]:
    """An answer's content as its fields, refused when it is not a JSON object of text or does not fit `offer`."""
    try:
        given = _TEXTS.validate_json(content)
    except ValidationError as e:
        raise ValueError(f"an answer's content is a JSON object of text fields: {content!r}") from e
    offer.refuse_content(given, who)
    return given


def conversations(person_email: str, provider: ProviderKey, events: Sequence[WorldEvent]) -> list[Waiting]:
    """Every message the agent sent in `provider` that the person can answer where it went, still there: each an
    ask, in the order sent, as it reads now. Which of them are asks and which follow-ups on an answer the person
    already owes is the people engine's to say (decision 2: one item per conversation)."""
    latest: dict[str, WorldEvent] = {}
    gone: set[str] = set()
    opened: list[str] = []
    for event in events:
        if event.entity.provider != provider or event.entity.kind is not EntityKind.MESSAGE:
            continue
        key = event.entity.external_id
        if event.operation is Operation.DELETE:
            gone.add(key)
        elif isinstance(event.after, MessageSnapshot) and event.operation in (Operation.CREATE, Operation.UPDATE):
            if event.operation is Operation.CREATE and event.actor is Actor.AGENT:
                opened.append(key)
            latest[key] = event
    found: list[Waiting] = []
    for key in opened:
        event = latest[key]
        after = event.after
        if key in gone or not isinstance(after, MessageSnapshot) or not after.answerable:
            continue
        if person_email not in after.recipient_emails:
            continue
        found.append(Waiting(item=event.entity, state=AWAITING, shown=after.text, conversation=True))
    return found


def message_offers(asked: MessageSnapshot) -> list[Offer]:
    """What a person can do with a message to them: write back, or use one of its controls (a button, a person
    picker), each named by its own id and described by the label they see; a link is no answer."""
    offers = [
        Offer(name=REPLY, to_state=REPLIED, fields=[OfferField(name=TEXT, required=True, description="your reply")])
    ]
    for action in asked.actions:
        if action.control is ControlKind.LINK:
            continue
        fields = [
            OfferField(name=TEXT, description="what your answer says, as the service shows it"),
            OfferField(name=FORM, description="what you type into the form it opens"),
        ]
        if action.control is ControlKind.USER_SELECT:
            fields.append(OfferField(name=PICKS, required=True, description="the person you pick"))
        offers.append(
            Offer(
                name=action.action_id,
                to_state=action.value or action.action_id,
                description=f'"{action.label}" on the message',
                label=action.label,
                fields=fields,
            )
        )
    return offers


def answered(
    asked: EntityRef, snapshot: MessageSnapshot, offer: str, who: str, content: str, at: datetime
) -> PersonReply:
    """The person's answer to a message as its provider delivers it: words written back, or a control used, with
    what its form holds. Refused when the message does not offer it."""
    if offer == AUTOMATIC_REPLY:
        given = _TEXTS.validate_json(content)
        text = given[TEXT] if TEXT in given else ""
        return PersonReply(person=who, in_reply_to=asked, text=text, at=at, writing=Writing.AUTOMATIC)
    found = next((o for o in message_offers(snapshot) if o.name == offer), None)
    if found is None:
        raise ValueError(f"the message {asked.external_id} offers no {offer!r}")
    given = content_of(content, found, who)
    if offer == REPLY:
        return PersonReply(person=who, in_reply_to=asked, text=given[TEXT], at=at)
    control = next(a for a in snapshot.actions if a.action_id == offer)
    form = _FORM.validate_json(given[FORM]) if FORM in given and given[FORM].strip() else []
    press = Press(
        action_id=control.action_id,
        label=control.label,
        value=control.value,
        picks=given[PICKS] if PICKS in given else None,
        form=form,
    )
    text = given[TEXT] if TEXT in given else "\n".join(f.value for f in form) or control.label
    return PersonReply(person=who, in_reply_to=asked, text=text, at=at, press=press)


def answer_transition(
    provider: ProviderKey, asked: EntityRef, offer: str, by: Actor, who: str, content: str, at: datetime
) -> Transition:
    """The move a person's answer to a conversation is: `reply` or the control used, from awaiting to replied; an
    automatic reply leaves it awaiting."""
    to = AWAITING if offer == AUTOMATIC_REPLY else REPLIED
    return Transition(
        provider=provider,
        item=asked,
        name=offer,
        from_state=AWAITING,
        to_state=to,
        by=by,
        who=who,
        content=content,
        at=at,
    )


def ticket_acts(state: str) -> list[Offer]:
    """What a person can do to a ticket besides moving it: comment, hand it on, delete it. Only what the scenario has
    them do (a take, a happening), never a model's pick."""
    return [
        Offer(
            name=COMMENT,
            to_state=state,
            fields=[OfferField(name=COMMENT, required=True, description="The comment")],
            unprompted=False,
        ),
        Offer(
            name=REASSIGN,
            to_state=state,
            fields=[OfferField(name=ASSIGNEE, description="Who it goes to, by their email address; empty: nobody")],
            unprompted=False,
        ),
        Offer(name=DELETE, to_state=DELETED, unprompted=False),
    ]


def ticket_move(action: Moves | Reassigns | Comments | Deletes, emails: dict[str, str]) -> tuple[str, dict[str, str]]:
    """The offer a ticket happening's action takes, and what it carries: a move by what its state means, a comment,
    a reassignment (to the address `emails` gives the person's key), a deletion."""
    if isinstance(action, Moves):
        return action.to.value, {}
    if isinstance(action, Reassigns):
        return REASSIGN, {ASSIGNEE: emails[action.to] if action.to is not None else ""}
    if isinstance(action, Comments):
        return COMMENT, {COMMENT: action.text}
    return DELETE, {}


def written_state(event: WorldEvent) -> str | None:
    """The state a write leaves its item in, in the provider's words where its snapshot has them (a ticket's state),
    else `EXISTS`, or `DELETED`; None for a read or a write the log keeps no snapshot of."""
    if event.operation is Operation.DELETE:
        return DELETED
    after = event.after
    if event.operation not in (Operation.CREATE, Operation.UPDATE) or after is None:
        return None
    if isinstance(after, TransitionSnapshot):
        return after.to_state
    if isinstance(after, TicketSnapshot):
        return after.state.value
    return EXISTS


def _carried(event: WorldEvent) -> str:
    """What a write carried, as a JSON object of its snapshot's own words."""
    after = event.after
    said: dict[str, object]
    if isinstance(after, MessageSnapshot):
        said = {
            "text": after.text,
            "channel": after.channel,
            "to": after.recipient_emails,
            "thread_of": after.thread_of,
        }
    elif isinstance(after, TicketSnapshot):
        said = {"title": after.title, "body": after.body, "project": after.project, "assignee": after.assignee_email}
    elif isinstance(after, DocumentSnapshot):
        said = {"title": after.title, "text": after.text}
    elif isinstance(after, RecordSnapshot):
        said = {"resource": after.resource, "text": after.text}
    elif isinstance(after, StoredSnapshot):
        said = {"collection": after.collection, "id": after.id, "item": after.item}
    elif isinstance(after, ServiceEventSnapshot):
        said = {"event": after.event.value, "item": after.item, "content": after.content}
    else:
        said = {}
    return json.dumps({k: v for k, v in said.items() if v not in (None, "", [])}, ensure_ascii=False)


def move_of(event: WorldEvent, was: str | None) -> Transition | None:
    """Any write in the log read as the move of its item's state it is: a recorded transition as recorded, and every
    other write (a message posted, a ticket edited, a document changed, a record stored) as its item going from `was`,
    the state the item's last write left it in (None: it did not exist), to the state this write leaves it in, named
    by the write's operation and carrying its snapshot's words. None for a read, or a write the log keeps no
    snapshot of."""
    if isinstance(event.after, TransitionSnapshot):
        return Transition.of(event)
    if event.entity.kind not in WORLD_ITEMS:
        return None
    to = written_state(event)
    if to is None:
        return None
    if isinstance(event.after, ServiceEventSnapshot) and event.operation is not Operation.DELETE:
        to = was or EXISTS  # a write to a declared service that moves no item leaves its state as it was
    return Transition(
        provider=event.entity.provider,
        item=event.entity,
        name=event.operation.value,
        from_state=was,
        to_state=to,
        by=event.actor,
        who=None,
        content=_carried(event),
        at=event.sim_time,
        seq=event.seq,
        wake=event.wake,
    )


def moves(events: Sequence[WorldEvent]) -> list[Transition]:
    """Every move in `events`, in order: each write read as one (`move_of`) from the state its item's previous write
    left it in. Not moves, though the state they leave is kept: the scenario setting the world up before the first
    wake, and a write to an item its provider also recorded as a transition by the same actor at the same moment,
    which the transition, in the provider's own words, stands for."""
    recorded = {(e.after.item, e.sim_time, e.actor) for e in events if isinstance(e.after, TransitionSnapshot)}
    state: dict[EntityRef, str] = {}
    found: list[Transition] = []
    for event in events:
        if isinstance(event.after, TransitionSnapshot):
            move = Transition.of(event)
        else:
            move = move_of(event, state.get(event.entity))
        if move is None:
            continue
        covered = not isinstance(event.after, TransitionSnapshot) and (move.item, move.at, move.by) in recorded
        if not covered:
            state[move.item] = move.to_state
        if covered or (event.wake == 0 and event.actor is Actor.SCENARIO):
            continue
        found.append(move)
    return found
