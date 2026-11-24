"""The people engine (`docs/design-transitions.md`): one way every person acts on what waits on them, in a run and
in a standing world alike, through any provider that implements `ports.transitions.ProvidesTransitions`: every ask
a person answers in words (a message to them, an item in the agent's own product) on every provider that has them,
and every other item (a ticket, a declared service's request) on the providers the scenario plays
(`Scenario.played()`).

1. **Look.** After every wake (and every `advance` of a standing world) each provider is asked what waits on each
   person (`items_for`). An item not yet held is pending on that person: written to the log as a `PendingSnapshot`
   (actor SCENARIO), so a fork reads what was owed at its checkpoint. One held that no longer waits on them is gone.
   A person acts once per turn on an item they are asked about: after their own move it waits on them again only
   once someone else moves it. Work they carry is theirs to move on, one move after another, until it is finished
   (`_carry`).
2. **Book.** Its moment is pinned by the scenario (`Person.takes` with `after`) or drawn as their answers are
   (`moments.draw_for`): from the run's seed, the person and the item, in their available time. A person who never
   acts (`Silent`, or a script that goes silent with nothing pinned) holds the item pending with no moment.
3. **Act.** At the moment the provider is asked for the legal offers. A pinned offer is taken (its words exactly as
   pinned, or a model's from its facts); otherwise a model picks one and writes what it carries, from what the person
   can see: the item as the service shows it, their facts at that moment, their conversations
   (`person-transition/2`). A pick that is not offered is asked again once; then the failure is recorded and the item
   stays owed, tried again later. The provider applies it through its own code path.
4. **Kept.** Every model call is kept and replayed by its context, as the replier's are (`PersonCall`).

**Conversations** (decision 2: one item per conversation) are planned and worded by the person's script and voice
(`application.replier`): a new message to them is an ask (`replier.plan`: whether and when they answer it, from
their script's steps, or conversing), unless it follows up an answer they already owe in the same conversation, which
then moves sooner when they are `reminded`; an ask edited before its answer is planned again from its new words, at the moment already drawn; a
person away while a delegate covers sends their automatic reply at once (a record of its own, due then). The answer's
words are written at its moment (`ready`), from everything the person can see by then — the whole conversation,
every follow-up that arrived while the answer was owed included — as a person writes when they sit down to reply, not
when the message lands; they are kept on the record (`PendingSnapshot.answer`), so a fork replays them, delivered by
the provider and kept as said. An ask that, read so, needs no answer passes (`PendingStatus.PASSED`): nobody hears of
anything. A
pinned take (`Person.takes`) wins over the script for the nth item it names, and on a provider the scenario names in
`transitions_on` the model picks among the message's offers (step 3).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from pydantic import Field, TypeAdapter

from minutehand.application.moments import (
    after_available,
    automatic_reply,
    available_between,
    decision_text,
    draw_for,
    drawn_span,
    follow_up_of,
    messages_to,
    pinned_at,
    sooner,
)
from minutehand.application.refusals import RunRefused, Unheard
from minutehand.application.replier import (
    HELPFULNESS,
    PeopleReplier,
    believed_part,
    bulleted,
    voice_rule,
    who_is,
    work_part,
)
from minutehand.domain.clock import Drawn, DrawnFrom
from minutehand.domain.common import Window
from minutehand.domain.conversation import ModelMessage, Provenance, Speaker, Wrote
from minutehand.domain.experiment import ReplyAt
from minutehand.domain.people import Decides, PersonReply, Plan, Press, Writing
from minutehand.domain.scenario import (
    FINISHED,
    AfterScript,
    Answers,
    DelayRange,
    FormInput,
    Model,
    Moves,
    Person,
    ProviderKey,
    Scenario,
    Scripted,
    Silent,
    Take,
    TicketHappening,
    TicketState,
)
from minutehand.domain.transitions import (
    AUTOMATIC_REPLY,
    FORM,
    PICKS,
    REPLY,
    TEXT,
    Offer,
    Transition,
    Waiting,
    item_parent,
    ticket_move,
)
from minutehand.domain.world import (
    OWN,
    Actor,
    Change,
    CourseSnapshot,
    CourseStatus,
    EffortFrom,
    EntityKind,
    EntityRef,
    InboxItemSnapshot,
    MessageSnapshot,
    Operation,
    PendingSnapshot,
    PendingStatus,
    TicketSnapshot,
    WorldEvent,
)
from minutehand.ports.clock import Clock
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.model import ModelFailed
from minutehand.ports.people import Replier
from minutehand.ports.store import Store
from minutehand.ports.transitions import CarriesWork, HoldsSeeded, ProvidesTransitions, SteersPeople

TRANSITION_PROMPT_VERSION = "person-transition/2"
"""Changes whenever TRANSITION_PROMPT or what the person is shown changes a word."""

TRANSITION_PROMPT = """\
You are {who}. You are at work. Something in a tool your team uses waits on you. You are shown it as you see it \
there, what you can do with it now and what each takes, and what you can see of your conversations.

What you know:
{known}
{believed}{work}{decided}
Rules:
- Pick exactly one of the things you can do, by its name exactly as listed, in "take".
- Fill "fields" with one entry per field you fill, each by its name exactly as listed: a required field always, an \
optional one only when you have something to say in it. Write each value as you would type it, from what you know.
- Stay consistent with what you said before.
- You decide only from what you know{or_believe}. {helpfulness}
- {voice}
- Today is {today}.
"""

ESTIMATE_PROMPT_VERSION = "person-estimate/1"
"""Changes whenever ESTIMATE_PROMPT or what the person is shown changes a word."""

JUDGED_HOURS_AT_MOST = 480.0
"""The most working hours a person judges one piece of work to take them: twelve weeks of forty."""

ESTIMATE_PROMPT = """\
You are {who}. You are at work, taking up a piece of work of yours now. You are shown it as you see it in the \
tool your team tracks it in, and what you can see of your conversations.

What you know:
{known}
{believed}
Say in "hours" how much of your own working time it takes you from now until it is done:
- Judge it as you would in your role, from what it asks and what you know: the time it really takes you, not \
the least it could take.
- Hours of work, not of the calendar.
- Today is {today}.
"""

_SCAN = 500


class WrittenField(Model):
    name: str = Field(description="The field's name, exactly as listed")
    value: str = Field(description="What you write in it")


class WrittenTransition(Model):
    """What the model answers for one item pending on one person: the transition taken, and what it carries."""

    take: str = Field(description="The name of what you do, exactly as listed")
    fields: list[WrittenField] = Field(default=[], description="One per field you fill")


class JudgedEffort(Model):
    """How long a person judges a piece of work they take up takes them."""

    hours: float = Field(
        gt=0,
        le=JUDGED_HOURS_AT_MOST,
        description="The hours of your own working time it takes you from now until it is done",
    )


@dataclass(frozen=True)
class Booking:
    """An item that became pending on a person: when they act on it, if they ever do."""

    pending: EntityRef
    """The engine's record of it (`EntityKind.PENDING`)."""
    person: str
    item: EntityRef
    at: datetime | None
    drawn: Drawn | None
    conversation: bool = False
    """An ask answered in words: due as a reply."""


@dataclass(frozen=True)
class Looked:
    booked: list[Booking]
    gone: list[EntityRef]
    moved: list[Booking]
    """Items already booked whose moment moved: a follow-up brought it sooner, an edit planned it again."""


class Ready(StrEnum):
    """Whether a person's move is ready to land, asked before anyone is told of it (`People.ready`)."""

    READY = "ready"
    PASSED = "passed"  # an answer that, read when they came to it, needed none: nobody hears of anything
    FAILED = "failed"  # a model failed to write its words: still owed, tried again on the run's next turn


@dataclass(frozen=True)
class Acted:
    """What came of a person's moment: the transition they took, or why there was none."""

    transition: Transition | None
    failure: str | None = None
    """Set when they still owe the move: a model failed, or picked what is not offered twice. Tried again later."""


@dataclass(frozen=True)
class Held:
    ref: EntityRef
    pending: PendingSnapshot


@dataclass(frozen=True)
class Carried:
    """What one look booked for the work a person carries: moves newly booked, and booked moves whose moment moved
    (None when it was held back and is booked for no moment until it is free again)."""

    booked: list[Booking]
    moved: list[Booking]


@dataclass(frozen=True)
class _Work:
    """A piece of work waiting on a person, as one provider sees it now."""

    port: ProvidesTransitions
    carrier: CarriesWork
    waiting: Waiting
    played: bool
    """Whether the scenario plays its provider (`Scenario.played`): unplayed work is carried only where a take pins it."""


_LIVE = frozenset({CourseStatus.QUEUED, CourseStatus.WORKING, CourseStatus.HELD})
_MEANINGS = frozenset(s.value for s in TicketState)
_TAKING_UP = "You are taking this up now: you start work on it."
_FINISHING = "You have done the work this needs."


def needs_model(scenario: Scenario) -> list[str]:
    """Each person on a provider the engine plays whose moves a model writes, and why: refused before anything runs
    when no model is configured."""
    found: list[str] = []
    for person in scenario.people:
        written = [t for t in person.takes if t.facts]
        if written:
            found.append(f"{person.key} (a model writes what they take {written[0].take!r} with)")
            continue
        if not _picks(person):
            continue
        plays = [*scenario.transitions_on, *(s.key for s in scenario.services if person.key in s.responders)]
        unpinned = [p for p in plays if not any(t.provider == p and t.nth is None for t in person.takes)]
        if unpinned:
            found.append(f"{person.key} (a model picks what they do on {', '.join(unpinned)})")
    return found


def _picks(person: Person) -> bool:
    """Whether a model picks what this person does where nothing is pinned: anyone who speaks for themselves."""
    behaviour = person.reply
    if isinstance(behaviour, Silent):
        return False
    if isinstance(behaviour, Answers):
        return True
    return behaviour.then is AfterScript.ANSWERS


class People:
    """The engine over `ports`, every provider of the run's that implements `ProvidesTransitions` (one that pushes
    to the agent bound to it, `TalksToAgent`), with `replier` planning and wording what people answer in words;
    refused before anything runs when a model writes what someone does and none is configured, or when a provider
    the scenario plays is not among them."""

    def __init__(
        self,
        scenario: Scenario,
        ports: Mapping[ProviderKey, object],
        replier: Replier,
        model: LanguageModel | None,
        pins: Sequence[ReplyAt] = (),
    ) -> None:
        needing = needs_model(scenario)
        if needing and model is None:
            raise RunRefused(f"a model writes what {'; '.join(needing)} do, and no model is configured")
        self._scenario = scenario
        self._ports: dict[ProviderKey, ProvidesTransitions] = {}
        for key, found in ports.items():
            if isinstance(found, ProvidesTransitions):
                self._ports[key] = found
        self._replier = replier
        self._words = PeopleReplier(scenario, model) if model is not None else None
        self._people = {p.key: p for p in scenario.people}
        self._item_pins = {(p.person, p.provider, p.to_ask): p.after for p in pins if p.provider is not None}
        for key in scenario.played():
            self.port(key)
        for person in scenario.people:
            for take in person.takes:
                if take.provider is not None and take.provider not in self._ports:
                    raise RunRefused(
                        f"{person.key} takes {take.take!r} on {take.provider}, which nobody can act through in this "
                        f"run: {', '.join(sorted(self._ports)) or 'none'}"
                    )

    @property
    def scenario(self) -> Scenario:
        return self._scenario

    @property
    def replier(self) -> Replier:
        return self._replier

    def port(self, key: ProviderKey) -> ProvidesTransitions:
        if key not in self._ports:
            raise RunRefused(f"the scenario has people act through transitions on {key}, which offers none")
        return self._ports[key]

    # -- what is pending ----------------------------------------------------------------------------------------

    def held(self, person: str, world: Store) -> list[Held]:
        """Every item the engine has held pending on `person`, as each record last stood, in the order they began."""
        found: list[Held] = []
        after: str | None = None
        while True:
            page = world.children(OWN, EntityKind.PENDING, person, after=after, limit=_SCAN)
            found += [Held(ref=s.entity, pending=PendingSnapshot.model_validate_json(s.body)) for s in page]
            if len(page) < _SCAN:
                return sorted(found, key=lambda h: h.ref.external_id.rsplit("|", 1)[1])
            after = page[-1].entity.external_id

    def holds(self, person: str, item: EntityRef, world: Store) -> bool:
        """Whether the engine has ever held `item` pending on `person`: the replier leaves it to the engine."""
        return any(h.pending.item == item for h in self.held(person, world))

    async def look(self, world: Store, clock: Clock) -> Looked:
        """What waits on each person now, against what the engine holds: each new item pending on them, written and
        booked; each held one that no longer waits on them, gone; each conversation followed up or edited, moved."""
        booked: list[Booking] = []
        gone: list[EntityRef] = []
        moved: list[Booking] = []
        played = self._scenario.played()
        events = world.events()
        for person in self._scenario.people:
            held = self.held(person.key, world)
            asks: list[tuple[ProviderKey, Waiting]] = []
            works: list[_Work] = []
            for key, port in self._ports.items():
                waiting = port.items_for(person, world)
                here = [w.item for w in waiting]
                for h in held:
                    if h.pending.item.provider != key or h.pending.status is not PendingStatus.PENDING:
                        continue
                    if h.pending.item not in here:
                        self._close(world, h, PendingStatus.GONE)
                        gone.append(h.ref)
                for item in waiting:
                    if item.conversation:
                        asks.append((key, item))
                    elif item.work and isinstance(port, CarriesWork):
                        works.append(_Work(port=port, carrier=port, waiting=item, played=key in played))
                    elif (
                        key in played
                        or self._pins(person, port, item.item, held, world, conversation=False) is not None
                    ):
                        made = self._pend(person, item, held, world, clock)
                        if made is not None:
                            booked.append(made)
            if asks:
                await self._converse(person, asks, events, world, clock, booked, moved)
            carried = self._carry(person, works, held, events, world, clock)
            booked += carried.booked
            moved += carried.moved
            moved += self._items_reminded(person, events, world, clock)
        return Looked(booked=booked, gone=gone, moved=moved)

    async def _converse(
        self,
        person: Person,
        asks: list[tuple[ProviderKey, Waiting]],
        events: list[WorldEvent],
        world: Store,
        clock: Clock,
        booked: list[Booking],
        moved: list[Booking],
    ) -> None:
        """Every ask of `person` not yet held, in the order asked: an automatic reply when they are away while
        someone covers; a follow-up on an answer they owe, which moves sooner when they are reminded; otherwise an
        ask of its own, planned by their script (`replier.plan`). Then every ask they owe an answer to that was
        edited since, planned again from its new words."""
        latest: dict[EntityRef, WorldEvent] = {}
        created: dict[EntityRef, WorldEvent] = {}
        for e in events:
            if isinstance(e.after, MessageSnapshot | InboxItemSnapshot) and e.operation is not Operation.DELETE:
                latest[e.entity] = e
                if e.operation is Operation.CREATE:
                    created.setdefault(e.entity, e)
        ordered = sorted(asks, key=lambda a: created[a[1].item].seq if a[1].item in created else 0)
        held = self.held(person.key, world)
        seen = [h.pending.item for h in held] + [
            EntityRef(provider=h.pending.item.provider, kind=EntityKind.MESSAGE, external_id=f)
            for h in held
            for f in h.pending.follow_ups
        ]
        tried = [ask.item for _, ask in ordered if ask.item not in seen]  # written just now: again on the next look
        for key, ask in ordered:
            if ask.item in seen or ask.item not in latest:
                continue
            asked = latest[ask.item]
            opened = created[ask.item] if ask.item in created else asked
            history = [e for e in events if e.seq <= asked.seq]
            automatic = automatic_reply(self._scenario, person, opened, history, world.replies())
            port = self.port(key)
            noted = ask.item.kind is not EntityKind.INBOX_ITEM or any(
                o.note for o in port.legal(ask.item, Actor.PERSON, person, world)
            )
            waiting = any(
                h.pending.take == AUTOMATIC_REPLY and h.pending.status is PendingStatus.PENDING
                for h in self.held(person.key, world)
            )  # one already on its way: once per absence
            if automatic is not None and noted and not waiting:
                booked.append(self._automatic(person, ask, automatic, asked, world, clock))
            owed = self._owed(world)
            reminded = (
                follow_up_of(opened, messages_to(person, history), owed)
                if isinstance(opened.after, MessageSnapshot) and opened.operation is Operation.CREATE
                else None
            )
            if reminded is not None:
                owing = next(
                    (
                        h
                        for h in self.held(person.key, world)
                        if h.pending.item == reminded.entity
                        and h.pending.status is PendingStatus.PENDING
                        and h.pending.take != AUTOMATIC_REPLY
                    ),
                    None,
                )
                if owing is not None:
                    later = self._reminded(person, owing, opened, ask.item, history, world, clock)
                    if later is not None:
                        moved.append(later)
                    continue
            everyone = self.held(person.key, world)
            picked = key in self._scenario.transitions_on or ask.item.kind is EntityKind.INBOX_ITEM
            if picked or self._pins(person, self.port(key), ask.item, everyone, world, conversation=True) is not None:
                made = self._pend(person, ask, everyone, world, clock)
                if made is not None:
                    booked.append(made)
                continue
            nth, _ = _places(everyone, ask.item, True)
            booked_now = await self._ask(person, ask, asked, history, owed, nth, world, clock)
            if booked_now is not None:
                booked.append(booked_now)
        for h in self.held(person.key, world):
            p = h.pending
            unwritten = p.status is PendingStatus.PENDING and p.plan is not None and p.answer is None
            if unwritten and p.failure is not None and p.item in latest and p.item not in tried:
                worded = await self._worded(person, p, latest[p.item], world, clock)
                self._write(world, h.ref, worded, Operation.UPDATE)
                if worded.status is PendingStatus.PASSED:
                    moved.append(Booking(pending=h.ref, person=person.key, item=p.item, at=None, drawn=None))
        for h in self.held(person.key, world):
            p = h.pending
            open_ = p.status in (PendingStatus.PENDING, PendingStatus.PASSED)
            if not p.conversation or not open_ or p.asked is None or p.take is not None:
                continue
            now = latest[p.item] if p.item in latest else None
            if now is None or now.seq <= p.asked or not _reworded(events, p.item, p.asked, now):
                continue
            history = [e for e in events if e.seq <= now.seq]
            moved.append(await self._replanned(person, h, now, history, world, clock, keep_moment=True))

    def _owed(self, world: Store) -> list[tuple[EntityRef, datetime]]:
        """Every answer people gave or owe, by the ask it answers: what tells a follow-up from a new ask. An
        automatic reply is no answer."""
        given = [(r.in_reply_to, r.at) for r in world.replies() if r.answers]
        owing: list[tuple[EntityRef, datetime]] = []
        for person in self._scenario.people:
            for h in self.held(person.key, world):
                p = h.pending
                if p.conversation and p.status is PendingStatus.PENDING and p.due_at and p.take != AUTOMATIC_REPLY:
                    owing.append((p.item, p.due_at))
        return [*given, *owing]

    def _automatic(
        self, person: Person, ask: Waiting, automatic: PersonReply, asked: WorldEvent, world: Store, clock: Clock
    ) -> Booking:
        """A person away while a delegate covers sends their automatic reply at once: a record of its own, its words
        fixed (`take` is the automatic reply), due now. It answers nothing, and the ask stays theirs."""
        pending = PendingSnapshot(
            person=person.key,
            item=ask.item,
            nth=1,
            state=ask.state,
            turn=0,
            status=PendingStatus.PENDING,
            due_at=clock.now(),
            take=AUTOMATIC_REPLY,
            drawn=automatic.drawn,
            conversation=True,
            asked=asked.seq,
            answer=automatic.model_dump_json(),
        )
        ref = self._ref(person, ask.item, world)
        self._write(world, ref, pending, Operation.CREATE)
        return Booking(
            pending=ref, person=person.key, item=ask.item, at=clock.now(), drawn=automatic.drawn, conversation=True
        )

    async def _ask(
        self,
        person: Person,
        ask: Waiting,
        asked: WorldEvent,
        history: list[WorldEvent],
        owed: list[tuple[EntityRef, datetime]],
        nth: int,
        world: Store,
        clock: Clock,
    ) -> Booking | None:
        """An ask of its own: what the person's script makes of it (`replier.plan`), held with its plan; its words
        are written at its moment (`ready`)."""
        plan = self._replier.plan(person, asked, history, owed)
        pending = PendingSnapshot(
            person=person.key,
            item=ask.item,
            nth=nth,
            state=ask.state,
            turn=0,
            status=PendingStatus.PENDING,
            due_at=max(plan.at, clock.now()) if plan is not None else None,
            drawn=plan.drawn if plan is not None else None,
            conversation=True,
            plan=plan.model_dump_json() if plan is not None else None,
            asked=asked.seq,
        )
        ref = self._ref(person, ask.item, world)
        self._write(world, ref, pending, Operation.CREATE)
        if pending.due_at is None or pending.status is not PendingStatus.PENDING:
            return None
        return Booking(
            pending=ref, person=person.key, item=ask.item, at=pending.due_at, drawn=pending.drawn, conversation=True
        )

    def _reminded(
        self,
        person: Person,
        owing: Held,
        follow_up: WorldEvent,
        message: EntityRef,
        history: list[WorldEvent],
        world: Store,
        clock: Clock,
    ) -> Booking | None:
        """A follow-up on an answer the person owes: kept on the ask it follows up, and the answer moved there when
        their `reminded` draws it sooner; a moment pinned stays."""
        p = owing.pending
        followed = p.model_copy(update={"follow_ups": [*p.follow_ups, message.external_id]})
        drawn = p.drawn
        if p.due_at is None or p.plan is None or drawn is None or drawn.source is DrawnFrom.PINNED:
            self._write(world, owing.ref, followed, Operation.UPDATE)
            return None
        sooner_drawn = sooner(self._scenario, person, follow_up, history, p.due_at)
        if sooner_drawn is None:
            self._write(world, owing.ref, followed, Operation.UPDATE)
            return None
        plan = Plan.model_validate_json(p.plan).model_copy(update={"drawn": sooner_drawn})
        due = max(sooner_drawn.lands_at, clock.now())
        changed = followed.model_copy(update={"due_at": due, "drawn": sooner_drawn, "plan": plan.model_dump_json()})
        self._write(world, owing.ref, changed, Operation.UPDATE)
        return Booking(pending=owing.ref, person=person.key, item=p.item, at=due, drawn=sooner_drawn, conversation=True)

    def _items_reminded(self, person: Person, events: list[WorldEvent], world: Store, clock: Clock) -> list[Booking]:
        """Each message the agent sent a person who owes a decision in its own product, sent after the decision began
        to wait on them, reminds them of it: the decision moves sooner when their `reminded` draws it so (a moment
        pinned stays), and the message is kept on the record as a follow-up, read once."""
        found: list[Booking] = []
        if person.reminded is None:
            return found
        sent = [
            e
            for e in events
            if e.actor is Actor.AGENT
            and e.operation is Operation.CREATE
            and isinstance(e.after, MessageSnapshot)
            and person.email in e.after.recipient_emails
        ]
        for h in self.held(person.key, world):
            p = h.pending
            if p.status is not PendingStatus.PENDING or p.item.kind is not EntityKind.INBOX_ITEM or p.asked is not None:
                continue
            began = next((e.seq for e in events if e.entity == h.ref), None)
            if began is None:
                continue  # it began to wait in this very look: nothing has reminded them of it yet
            moment: Booking | None = None
            for message in [m for m in sent if m.seq > began and m.entity.external_id not in p.follow_ups]:
                p = p.model_copy(update={"follow_ups": [*p.follow_ups, message.entity.external_id]})
                pinned = p.drawn is not None and p.drawn.source is DrawnFrom.PINNED
                drawn = (
                    sooner(self._scenario, person, message, [e for e in events if e.seq <= message.seq], p.due_at)
                    if p.due_at is not None and not pinned
                    else None
                )
                if drawn is not None:
                    p = p.model_copy(update={"due_at": max(drawn.lands_at, clock.now()), "drawn": drawn})
                    moment = Booking(
                        pending=h.ref, person=person.key, item=p.item, at=p.due_at, drawn=drawn, conversation=True
                    )
            if p != h.pending:
                self._write(world, h.ref, p, Operation.UPDATE)
            if moment is not None:
                found.append(moment)
        return found

    async def _replanned(
        self,
        person: Person,
        held: Held,
        asked: WorldEvent,
        history: list[WorldEvent],
        world: Store,
        clock: Clock,
        *,
        keep_moment: bool = False,
    ) -> Booking:
        """An ask whose words changed before its answer, or whose person changed: planned again from the message as
        it reads now, with nothing it was owed before counted; any words already written are dropped, and written
        afresh at its moment (`ready`). An edit (`keep_moment`) keeps the moment already drawn: the person answers
        when they were going to, and reads the message as it is then. Drawing again from the edit put every reply
        behind each rewrite of the message, so an agent that edits as it works pushed its answers ever later."""
        owed = [(ref, at) for ref, at in self._owed(world) if ref != held.pending.item]
        plan = self._replier.plan(person, asked, history, owed)
        kept = held.pending.drawn
        if keep_moment and plan is not None and kept is not None:
            plan = plan.model_copy(update={"drawn": kept})
        changed = held.pending.model_copy(
            update={
                "status": PendingStatus.PENDING,
                "due_at": max(plan.at, clock.now()) if plan is not None else None,
                "drawn": plan.drawn if plan is not None else None,
                "plan": plan.model_dump_json() if plan is not None else None,
                "asked": asked.seq,
                "answer": None,
                "failure": None,
            }
        )
        self._write(world, held.ref, changed, Operation.UPDATE)
        at = changed.due_at if changed.status is PendingStatus.PENDING else None
        return Booking(
            pending=held.ref, person=person.key, item=held.pending.item, at=at, drawn=changed.drawn, conversation=True
        )

    async def replan(self, world: Store, clock: Clock, people: set[str]) -> list[Booking]:
        """Every ask `people` still owe, planned again under the scenario as it stands (a fork that changed them, or
        pinned a moment): each moved to its new moment, or to none."""
        events = world.events()
        latest: dict[EntityRef, WorldEvent] = {}
        for e in events:
            if isinstance(e.after, MessageSnapshot | InboxItemSnapshot) and e.operation is not Operation.DELETE:
                latest[e.entity] = e
        moved: list[Booking] = []
        for key in sorted(people):
            person = self._people[key]
            for h in self.held(key, world):
                p = h.pending
                if p.status is not PendingStatus.PENDING or p.take == AUTOMATIC_REPLY:
                    continue
                if p.asked is None:
                    course = self._live_course(key, p.item, world)
                    if course is None:
                        moved.append(self._repended(person, h, world, clock))
                    elif course[1].started_at is None and p.due_at is not None:
                        # work not yet picked up is planned again under the person as they now are; begun work
                        # keeps its plan
                        moved.append(self._replanned_work(person, h, course[0], course[1], world, clock))
                    continue
                if p.item not in latest:
                    continue
                asked = latest[p.item]
                history = [e for e in events if e.seq <= asked.seq]
                moved.append(await self._replanned(person, h, asked, history, world, clock))
        return moved

    def _pins(
        self,
        person: Person,
        port: ProvidesTransitions,
        item: EntityRef,
        held: list[Held],
        world: Store,
        *,
        conversation: bool,
    ) -> int | None:
        """Which of `person`'s takes pins `item`, by its place in `Person.takes` (`pinned`). A take for every item
        in any provider that the item does not offer pins nothing there: the person answers it, or leaves it, as they
        otherwise would. One naming the provider holds there, offered or not."""
        nth, nth_any = _places(held, item, conversation)
        moved = sum(1 for h in held if h.pending.item == item and h.pending.status is PendingStatus.ACTED)
        found = pinned(person, item.provider, nth, nth_any, moved)
        if found is None:
            return None
        take = person.takes[found]
        anywhere = take.provider is None and take.nth is None
        if anywhere and _matching(take.take, port.legal(item, Actor.PERSON, person, world)) is None:
            return None
        return found

    def _ref(self, person: Person, item: EntityRef, world: Store) -> EntityRef:
        return EntityRef(
            provider=OWN,
            kind=EntityKind.PENDING,
            external_id=f"{person.key}|{item.provider}|{item.kind.value}|{item.external_id}|{world.head() + 1:010d}",
        )

    def _pend(self, person: Person, item: Waiting, held: list[Held], world: Store, clock: Clock) -> Booking | None:
        """An item the person moves rather than answers in words (a ticket, an invitation or a message where the
        scenario plays the provider, an item of the agent's own product), or one a take pins: booked at the pinned
        moment or as their answers are drawn, and moved by the pin or a model's pick. `held` is everything held on
        them, in every provider."""
        mine = [h for h in held if h.pending.item == item.item and h.pending.take != AUTOMATIC_REPLY]
        if any(h.pending.status is PendingStatus.PENDING for h in mine):
            return None
        turn = last_turn(world, item.item, person.key)
        if any(h.pending.status is PendingStatus.ACTED and h.pending.turn == turn for h in mine):
            return None
        nth, _ = _places(held, item.item, item.conversation)
        found = self._pins(
            person, self.port(item.item.provider), item.item, held, world, conversation=item.conversation
        )
        take = person.takes[found] if found is not None else None
        asked = next((e for e in world.events() if e.entity == item.item), None) if item.conversation else None
        began = asked.sim_time if asked is not None else clock.now()
        drawn = self._drawn(person, item.item, nth, take, began, world)
        held_why: str | None = None
        if take is None and not item.conversation:
            offered = self.port(item.item.provider).legal(item.item, Actor.PERSON, person, world)
            if not any(o.unprompted for o in offered):
                # nothing they would do with it unprompted (a draft only its author marks ready, where a take says):
                # held with no moment, as a person who never acts holds it, never booked to be dropped and again
                drawn = None
                held_why = "nothing they would do with it is theirs to do unprompted"
        pending = PendingSnapshot(
            person=person.key,
            item=item.item,
            nth=nth,
            state=item.state,
            turn=turn,
            status=PendingStatus.PENDING,
            due_at=drawn.lands_at if drawn is not None else None,
            take=take.take if take is not None else None,
            pinned=found,
            drawn=drawn,
            conversation=item.conversation,
            held=held_why,
        )
        ref = self._ref(person, item.item, world)
        self._write(world, ref, pending, Operation.CREATE)
        return Booking(
            pending=ref,
            person=person.key,
            item=item.item,
            at=pending.due_at,
            drawn=drawn,
            conversation=item.conversation,
        )

    def _drawn(
        self, person: Person, item: EntityRef, nth: int, take: Take | None, began: datetime, world: Store
    ) -> Drawn | None:
        """When the person acts on `item`, their nth in its provider, which began to wait on them at `began`: exactly
        as a fork pins it (`ReplyAt` with `provider`), or `after` it when a take says, else drawn as their answers are
        (within the take's window, or the provider's), or never for someone who neither is pinned nor picks."""
        if (person.key, item.provider, nth) in self._item_pins:
            return pinned_at(self._scenario, began, self._item_pins[(person.key, item.provider, nth)])
        if take is not None and take.after is not None:
            return pinned_at(self._scenario, began, take.after)
        if take is None and not _picks(person):
            return None
        port = self.port(item.provider)
        within = take.within if take is not None and take.within is not None else None
        if within is None and isinstance(port, SteersPeople):
            within = port.within(item, world)
        behaviour = person.reply
        assert isinstance(behaviour, Answers | Scripted)
        sent = messages_to(person, world.events())
        return draw_for(
            self._scenario,
            person,
            item,
            began,
            first_asked=sent[0].sim_time if sent else began,
            within=within,
            delay=behaviour.delay,
        )

    def _repended(self, person: Person, held: Held, world: Store, clock: Clock) -> Booking:
        """An item a person moves, pinned and drawn again under the scenario as it stands (a fork that changed them):
        from the moment it began to wait on them, or the fork's when that is already past."""
        everyone = self.held(person.key, world)
        p = held.pending
        found = self._pins(person, self.port(p.item.provider), p.item, everyone, world, conversation=p.conversation)
        take = person.takes[found] if found is not None else None
        began = next(e.sim_time for e in world.events() if e.entity == held.ref)
        drawn = self._drawn(person, p.item, p.nth, take, began, world)
        due = max(drawn.lands_at, clock.now()) if drawn is not None else None
        changed = p.model_copy(
            update={"pinned": found, "take": take.take if take is not None else None, "drawn": drawn, "due_at": due}
        )
        self._write(world, held.ref, changed, Operation.UPDATE)
        return Booking(
            pending=held.ref, person=person.key, item=p.item, at=due, drawn=drawn, conversation=p.conversation
        )

    async def happen(self, happening: TicketHappening, world: Store, clock: Clock) -> Transition | None:
        """A person does something to a seeded ticket by themselves, at the scenario's moment: the move, comment,
        reassignment or deletion its action names, through the provider's `apply` like any other transition. Nothing
        when the ticket is gone, or already where the move would take it."""
        return await happen(
            self._scenario, self.port(self._scenario.happening_ticket(happening).provider), happening, world, clock
        )

    # -- work a person carries -------------------------------------------------------------------------------------

    def courses(self, person: str, world: Store) -> list[tuple[EntityRef, CourseSnapshot]]:
        """Every course the engine has planned for `person`, as each record last stood, in the order they began."""
        found: list[tuple[EntityRef, CourseSnapshot]] = []
        after: str | None = None
        while True:
            page = world.children(OWN, EntityKind.COURSE, person, after=after, limit=_SCAN)
            found += [(s.entity, CourseSnapshot.model_validate_json(s.body)) for s in page]
            if len(page) < _SCAN:
                return sorted(found, key=lambda f: f[0].external_id.rsplit("|", 1)[1])
            after = page[-1].entity.external_id

    def _carry(
        self,
        person: Person,
        works: list[_Work],
        held: list[Held],
        events: list[WorldEvent],
        world: Store,
        clock: Clock,
    ) -> Carried:
        """The work waiting on `person` (`Waiting.work`), against the courses the engine holds for them: new work is
        queued with the takes that pin it as they stand then (`_course_takes`), which it keeps; work no longer theirs
        is gone; work its service says is held back is paused, with what is left of it kept; and the one they are on
        has its next move booked. When they are on none, the first of the rest that nothing holds back becomes the
        one: work already under way first, then by the day it is due, then its priority, then when it became theirs.
        Work a take pins runs beside the rest, at its pinned moments, whatever else they do or holds it."""
        now = clock.now()
        starts_at = self._scenario.starts_at
        plans = self.courses(person.key, world)
        live = {c.item: (ref, c) for ref, c in plans if c.status in _LIVE}
        nths = _work_places(plans, [w.waiting.item for w in works])
        used = [t for _, c in plans for t in (c.start_take, c.finish_take) if t is not None and t.nth is not None]
        pins: dict[EntityRef, tuple[Take | None, Take | None]] = {}
        for work in works:
            item = work.waiting.item
            if item in live:
                kept = live[item][1]
                pins[item] = (kept.start_take, kept.finish_take)
                continue
            pins[item] = self._course_takes(person, work, nths[item], world, used)
            used += [t for t in pins[item] if t is not None and t.nth is not None]
        pinned_work = [item for item, (start, finish) in pins.items() if start is not None or finish is not None]
        here = {w.waiting.item: w for w in works if w.played or w.waiting.item in pinned_work}
        pending_on = {h.pending.item: h for h in held if h.pending.status is PendingStatus.PENDING}
        first = self._first_ask(person, events, plans, now)
        carried = Carried(booked=[], moved=[])
        for item, (ref, course) in list(live.items()):
            if item not in here:
                self._write_course(
                    world, ref, course.model_copy(update={"status": CourseStatus.GONE}), Operation.UPDATE
                )
                del live[item]
        for item, work in here.items():
            waiting = work.waiting
            turn = last_turn(world, item, person.key)
            existing = live.get(item)
            if existing is None:
                start, finish = pins[item]
                under_way = _meaning(item, world) is TicketState.STARTED
                course = CourseSnapshot(
                    person=person.key,
                    item=item,
                    status=CourseStatus.QUEUED,
                    since=now,
                    shown=waiting.shown,
                    state=waiting.state,
                    under_way=under_way,
                    under_way_at_start=under_way and _under_way_at_setup(item, world),
                    due=waiting.due,
                    rank=waiting.rank,
                    turn=turn,
                    start_take=start,
                    finish_take=finish,
                    pinned_finish=now + finish.after if finish is not None and finish.after is not None else None,
                )
                ref = self._course_ref(person, item, world)
                self._write_course(world, ref, course, Operation.CREATE)
                live[item] = (ref, course)
                continue
            ref, course = existing
            seen = (waiting.shown, waiting.state, waiting.due, waiting.rank)
            update: dict[str, object] = {}
            if (course.shown, course.state, course.due, course.rank) != seen:
                update.update(shown=waiting.shown, state=waiting.state, due=waiting.due, rank=waiting.rank)
            if turn != course.turn:
                update.update(turn=turn, moved_to=[], stuck=False)
            if update:
                course = course.model_copy(update=update)
                self._write_course(world, ref, course, Operation.UPDATE)
                live[item] = (ref, course)
        for item, (ref, course) in list(live.items()):
            if item in pinned_work:
                continue
            holding = here[item].carrier.holding(item, world)
            if course.status is CourseStatus.WORKING and holding:
                if course.started_at is None:
                    course = course.model_copy(update={"status": CourseStatus.QUEUED, "held_by": holding})
                else:
                    ends = course.finishes_at
                    left = (
                        available_between(now, ends, person, starts_at, first_ask=first)
                        if ends is not None
                        else timedelta(0)
                    )
                    course = course.model_copy(
                        update={
                            "status": CourseStatus.HELD,
                            "held_by": holding,
                            "held_at": now,
                            "left": left,
                            "finishes_at": None,
                        }
                    )
                booked_on = pending_on.get(item)
                if booked_on is not None:
                    carried.moved.append(self._unbook(booked_on, holding, world))
            elif holding != course.held_by:
                course = course.model_copy(update={"held_by": holding})
            else:
                continue
            self._write_course(world, ref, course, Operation.UPDATE)
            live[item] = (ref, course)
        for item in [i for i in live if i in pinned_work]:
            ref, course = live[item]
            if course.status is not CourseStatus.WORKING:
                course = course.model_copy(update={"status": CourseStatus.WORKING})
                self._write_course(world, ref, course, Operation.UPDATE)
            self._keep_booked(person, course, nths[item], first, pending_on.get(item), world, clock, carried)
        rest = {item: rc for item, rc in live.items() if item not in pinned_work}
        current = [item for item, (_, c) in rest.items() if c.status is CourseStatus.WORKING]
        if not current and (_picks(person) or any(t.after is not None for t in person.takes)):
            free = [
                rc
                for rc in rest.values()
                if rc[1].status is not CourseStatus.WORKING and not rc[1].held_by and not rc[1].stuck
            ]
            if free:
                ref, course = min(free, key=lambda rc: _order(rc[1]))
                update = {"status": CourseStatus.WORKING}
                if course.started_at is not None:
                    left = course.left if course.left is not None else timedelta(0)
                    update.update(
                        finishes_at=after_available(now, left, person, starts_at, first_ask=first),
                        held_at=None,
                        left=None,
                    )
                course = course.model_copy(update=update)
                self._write_course(world, ref, course, Operation.UPDATE)
                live[course.item] = (ref, course)
                current = [course.item]
        for item in current:
            _, course = live[item]
            self._keep_booked(person, course, nths[item], first, pending_on.get(item), world, clock, carried)
        if not _picks(person):
            # a person who never acts holds every piece of their work pending with no moment, as every item before
            for item, (_, course) in live.items():
                if item not in pinned_work and item not in pending_on:
                    carried.booked.append(self._held_still(person, course, nths[item], world))
        return carried

    def _course_takes(
        self, person: Person, work: _Work, nth: int, world: Store, used: Sequence[Take]
    ) -> tuple[Take | None, Take | None]:
        """The takes that pin a piece of work, as they stand when it becomes theirs: of those naming it
        (`takes_naming`), the first that names a move beginning it pins their picking it up, and the first of the rest
        pins its finish. A take naming it by its place pins one piece of work, once (`used`); one for every item pins
        each it applies to."""
        named = [
            t
            for t in takes_naming(person, work.waiting.item.provider, nth)
            if (t.nth is None or t not in used) and self._applies(person, t, work, world)
        ]
        start = next((t for t in named if self._starts(person, t, work, world)), None)
        finish = next((t for t in named if t is not start), None)
        return start, finish

    def _first_ask(
        self,
        person: Person,
        events: Sequence[WorldEvent],
        plans: Sequence[tuple[EntityRef, CourseSnapshot]],
        now: datetime,
    ) -> datetime:
        """Where an absence that starts on a person's first ask starts: the first message sent to them, else when the
        first piece of work became theirs, else now. Never the moment of a move on work, so each move does not start
        the absence again."""
        sent = messages_to(person, events)
        if sent:
            return sent[0].sim_time
        return min((c.since for _, c in plans), default=now)

    def _first_asked(self, person: Person, world: Store, now: datetime) -> datetime:
        events = world.events(kinds=[EntityKind.MESSAGE, EntityKind.INBOX_ITEM])
        return self._first_ask(person, events, self.courses(person.key, world), now)

    def _held_still(self, person: Person, course: CourseSnapshot, nth: int, world: Store) -> Booking:
        """Work pending on a person who never acts (Silent, nothing pinned): held, with no moment."""
        pending = PendingSnapshot(
            person=person.key,
            item=course.item,
            nth=nth,
            state=course.state,
            turn=course.turn,
            status=PendingStatus.PENDING,
            due_at=None,
            held="they never act on it unprompted",
        )
        made = self._ref(person, course.item, world)
        self._write(world, made, pending, Operation.CREATE)
        return Booking(pending=made, person=person.key, item=course.item, at=None, drawn=None)

    def _keep_booked(
        self,
        person: Person,
        course: CourseSnapshot,
        nth: int,
        first: datetime,
        booked_on: Held | None,
        world: Store,
        clock: Clock,
        carried: Carried,
    ) -> None:
        """The next move on the work they are on, booked unless it already is. Nothing is booked for work they are
        stuck on, or, for a person who never acts (Silent), for a move no take pins."""
        if booked_on is not None and booked_on.pending.due_at is not None:
            return
        pinned_next = course.start_take if course.started_at is None else course.finish_take
        if course.stuck or (pinned_next is None and course.pinned_finish is None and not _picks(person)):
            return
        booking = self._step(person, course, nth, first, booked_on, world, clock)
        (carried.moved if booked_on is not None else carried.booked).append(booking)

    def _step(
        self,
        person: Person,
        course: CourseSnapshot,
        nth: int,
        first: datetime,
        booked_on: Held | None,
        world: Store,
        clock: Clock,
        *,
        began: datetime | None = None,
    ) -> Booking:
        """The person's next move on the work they are on: picking it up as soon as an answer of theirs would land
        (drawn from `began`, when that move was first booked, else now), where a fork pins it (`ReplyAt` with
        `provider`), or where its start take pins it, and never after a pinned finish; work they were on as the run
        began, from their first available moment; someone who never acts unprompted takes it up only as a take pins
        it. Then its finish at the moment its course says; or, past that
        moment and not finished (a step its workflow takes first, a review), its next step as soon as they come back to
        it, at once where a take pins the finish. The pending record names the take of the move it pins."""
        now = clock.now()
        behaviour = person.reply
        assert isinstance(behaviour, Answers | Scripted | Silent)
        delay = behaviour.delay if isinstance(behaviour, Answers | Scripted) else None
        drawn: Drawn
        take: Take | None = None
        if course.started_at is None:
            asked = began if began is not None else now
            forked = self._item_pins.get((person.key, course.item.provider, nth))
            if course.start_take is not None:
                take = course.start_take
                drawn = (
                    pinned_at(self._scenario, course.since, take.after)
                    if take.after is not None
                    else self._answering(person, course.item, asked, first, take.within, delay)
                )
            elif forked is not None:
                drawn = pinned_at(self._scenario, course.since, forked)
            elif course.pinned_finish is not None and not _picks(person):
                # someone who never acts unprompted moves it only as its take says: at its finish, in one move
                drawn = pinned_at(self._scenario, course.since, course.pinned_finish - course.since)
                take = course.finish_take
            elif course.under_way_at_start:
                # on it already as the run began: nothing reaches them to answer, so nothing is drawn
                resumes = after_available(asked, timedelta(0), person, self._scenario.starts_at, first_ask=first)
                drawn = Drawn(
                    source=DrawnFrom.CARRIED,
                    seed=self._scenario.seed,
                    asked_at=asked,
                    offset=resumes - asked,
                    lands_at=resumes,
                )
            else:
                drawn = self._answering(person, course.item, asked, first, None, delay)
            if take is None and course.pinned_finish is not None and drawn.lands_at >= course.pinned_finish:
                # they come to it no sooner than its pinned finish: taking it up is finishing it, as the take says
                drawn = pinned_at(self._scenario, course.since, course.pinned_finish - course.since)
                take = course.finish_take
        else:
            take = course.finish_take
            if course.finishes_at is not None and course.finishes_at > now:
                drawn = (
                    pinned_at(self._scenario, course.since, course.finishes_at - course.since)
                    if course.pinned_finish is not None
                    else Drawn(
                        source=DrawnFrom.WORK,
                        seed=self._scenario.seed,
                        asked_at=course.started_at,
                        offset=course.effort if course.effort is not None else timedelta(0),
                        lands_at=course.finishes_at,
                    )
                )
            elif course.pinned_finish is not None:
                drawn = pinned_at(self._scenario, now, timedelta(0))
            else:
                drawn = self._answering(person, course.item, now, first, None, delay)
        found = _place(person, take)
        said = take.take if take is not None else None
        due = max(drawn.lands_at, now)
        if booked_on is not None:
            changed = booked_on.pending.model_copy(
                update={"due_at": due, "drawn": drawn, "pinned": found, "take": said, "held": None}
            )
            self._write(world, booked_on.ref, changed, Operation.UPDATE)
            return Booking(pending=booked_on.ref, person=person.key, item=course.item, at=due, drawn=drawn)
        pending = PendingSnapshot(
            person=person.key,
            item=course.item,
            nth=nth,
            state=course.state,
            turn=course.turn,
            status=PendingStatus.PENDING,
            due_at=due,
            take=said,
            pinned=found,
            drawn=drawn,
        )
        made = self._ref(person, course.item, world)
        self._write(world, made, pending, Operation.CREATE)
        return Booking(pending=made, person=person.key, item=course.item, at=due, drawn=drawn)

    def _answering(
        self,
        person: Person,
        item: EntityRef,
        now: datetime,
        first: datetime,
        within: Window | None,
        delay: DelayRange | None,
    ) -> Drawn:
        """A moment drawn as this person's answers are: within `within` or their `reply_within`, else their reply's
        delay; at once for someone with neither (a Silent person whose move a take pins)."""
        if within is None and person.reply_within is None and delay is None:
            return pinned_at(self._scenario, now, timedelta(0))
        return draw_for(
            self._scenario,
            person,
            item,
            now,
            first_asked=first,
            within=within,
            delay=delay if delay is not None else DelayRange(),
        )

    def _starts(self, person: Person, take: Take, work: _Work, world: Store) -> bool:
        """Whether a take names a move that begins the work: a move to a started state, by its name, the state it
        reaches, its label or what that state means."""
        said = take.take.strip().casefold()
        if said == TicketState.STARTED.value:
            return True
        offer = _matching(take.take, work.port.legal(work.waiting.item, Actor.PERSON, person, world))
        return offer is not None and offer.means is TicketState.STARTED

    def _unbook(self, booked_on: Held, holding: Sequence[EntityRef], world: Store) -> Booking:
        """A booked move on work that is now held back: kept owed for no moment until it is free again, saying what
        holds it."""
        held_by = ", ".join(f"{r.provider} {r.kind.value} {r.external_id}" for r in holding)
        changed = booked_on.pending.model_copy(update={"due_at": None, "held": f"held back by {held_by}"})
        self._write(world, booked_on.ref, changed, Operation.UPDATE)
        return Booking(
            pending=booked_on.ref, person=booked_on.pending.person, item=booked_on.pending.item, at=None, drawn=None
        )

    def _replanned_work(
        self, person: Person, held: Held, ref: EntityRef, course: CourseSnapshot, world: Store, clock: Clock
    ) -> Booking:
        """Work not yet picked up, planned again under the person as a fork changed them: the takes that pin it as
        they stand now, and its picking up drawn again from when it was first booked, so a person whose answers did
        not change takes it up when they were going to. Someone the fork made silent holds it with no moment, unless a
        take pins it."""
        port = self.port(course.item.provider)
        waiting = next((w for w in port.items_for(person, world) if w.item == course.item), None)
        if waiting is not None and isinstance(port, CarriesWork):
            work = _Work(
                port=port, carrier=port, waiting=waiting, played=course.item.provider in self._scenario.played()
            )
            others = [c for r, c in self.courses(person.key, world) if r != ref]
            used = [t for c in others for t in (c.start_take, c.finish_take) if t is not None and t.nth is not None]
            start, finish = self._course_takes(person, work, held.pending.nth, world, used)
            after = finish.after if finish is not None else None
            course = course.model_copy(
                update={
                    "start_take": start,
                    "finish_take": finish,
                    "pinned_finish": course.since + after if after is not None else None,
                }
            )
            self._write_course(world, ref, course, Operation.UPDATE)
        if not _picks(person) and course.start_take is None and course.pinned_finish is None:
            changed = held.pending.model_copy(
                update={
                    "due_at": None,
                    "drawn": None,
                    "pinned": None,
                    "take": None,
                    "held": "they never act on it unprompted",
                }
            )
            self._write(world, held.ref, changed, Operation.UPDATE)
            return Booking(pending=held.ref, person=person.key, item=course.item, at=None, drawn=None)
        began = next(e.sim_time for e in world.events() if e.entity == held.ref)
        first = self._first_asked(person, world, clock.now())
        return self._step(person, course, held.pending.nth, first, held, world, clock, began=began)

    def _applies(self, person: Person, take: Take, work: _Work, world: Store) -> bool:
        """Whether a take pins a piece of work: one naming its provider or its place does; one for every item anywhere
        does only where it names what a ticket's state means, or a move the work offers now."""
        if take.provider is not None or take.nth is not None:
            return True
        if take.take.strip().casefold() in {s.value for s in TicketState}:
            return True
        return _matching(take.take, work.port.legal(work.waiting.item, Actor.PERSON, person, world)) is not None

    async def _effort(
        self, person: Person, carrier: CarriesWork, waiting: Waiting, world: Store, clock: Clock
    ) -> tuple[timedelta, EffortFrom] | None:
        """How long the work takes them from when they pick it up, in their available time: what its service says is
        left of it, else drawn within their pace, else as long as they judge it takes them (`_judged`); None for
        someone no model speaks for, who judges nothing."""
        estimate = carrier.estimate(waiting.item, world)
        if estimate is not None:
            return estimate, EffortFrom.ESTIMATE
        if person.pace is not None:
            return drawn_span(self._scenario, person, waiting.item, "pace", person.pace), EffortFrom.PACE
        behaviour = person.reply
        if self._words is None or not isinstance(behaviour, Answers | Scripted):
            return None
        return await self._judged(person, behaviour, self._words, waiting, world, clock), EffortFrom.PERSON

    async def _judged(
        self,
        person: Person,
        behaviour: Answers | Scripted,
        words: PeopleReplier,
        waiting: Waiting,
        world: Store,
        clock: Clock,
    ) -> timedelta:
        """How much of their working time a piece of work they take up takes them, as they judge it from what it asks
        and what they know, where its tracker holds no estimate and the scenario gives them no pace: one model call,
        kept and replayed by its context as each of theirs is."""
        history = world.events()
        system = estimate_prompt(person, behaviour, clock.now(), self._scenario.starts_at)
        context = (
            await words.context(person, behaviour, history[-1], history, world, clock, answers=False) if history else []
        )
        shown = f"The work you are taking up, as {waiting.item.provider} shows it:\n{waiting.shown}"
        messages = [ModelMessage(speaker=Speaker.ASKER, text=shown + "\n\n" + (context[0].text if context else ""))]
        judged = await words.ask_model(
            person, Wrote.ESTIMATE, waiting.item, system, messages, JudgedEffort, ESTIMATE_PROMPT_VERSION, world, clock
        )
        return timedelta(hours=judged.answer.hours)

    def _course_ref(self, person: Person, item: EntityRef, world: Store) -> EntityRef:
        return EntityRef(
            provider=OWN,
            kind=EntityKind.COURSE,
            external_id=f"{person.key}|{item.provider}|{item.kind.value}|{item.external_id}|{world.head() + 1:010d}",
        )

    def _live_course(self, person: str, item: EntityRef, world: Store) -> tuple[EntityRef, CourseSnapshot] | None:
        return next(((r, c) for r, c in self.courses(person, world) if c.item == item and c.status in _LIVE), None)

    def _write_course(self, world: Store, ref: EntityRef, course: CourseSnapshot, operation: Operation) -> None:
        world.apply(
            Change(
                entity=ref,
                operation=operation,
                actor=Actor.SCENARIO,
                body=course.model_dump_json(),
                parent=course.person,
                after=course,
            )
        )

    # -- acting ---------------------------------------------------------------------------------------------------

    def pending(self, pending: EntityRef, world: Store) -> PendingSnapshot:
        """The engine's record of one item pending on a person, as it stands."""
        return self._read(pending, world)

    def heard_of(self, pending: EntityRef, world: Store, clock: Clock) -> bool:
        """Whether the service tells the agent when this person's move lands (`ProvidesTransitions.heard_of`)."""
        held = self._read(pending, world)
        return self.port(held.item.provider).heard_of(held.item, self._people[held.person], world, clock)

    async def act(self, pending: EntityRef, world: Store, clock: Clock) -> Acted:
        """The person acts on the item now: the pinned offer, or the model's pick among the legal ones, applied
        through the provider. Nothing when it no longer waits on them, or nothing is offered (it is gone)."""
        snap = self._read(pending, world)
        if snap.status is not PendingStatus.PENDING:
            return Acted(transition=None)
        held = Held(ref=pending, pending=snap)
        person = self._people[snap.person]
        port = self.port(snap.item.provider)
        waiting = next((w for w in port.items_for(person, world) if w.item == snap.item), None)
        if waiting is None:
            self._close(world, held, PendingStatus.GONE)
            return Acted(transition=None)
        if snap.conversation and (snap.plan is not None or snap.answer is not None):
            return await self._answer(held, person, port, world, clock)
        course = self._live_course(person.key, snap.item, world)
        if course is not None and isinstance(port, CarriesWork):
            return await self._work_step(held, person, port, port, waiting, course[0], course[1], world, clock)
        take = person.takes[snap.pinned] if snap.pinned is not None and snap.pinned < len(person.takes) else None
        steers = port if isinstance(port, SteersPeople) else None
        failure: str | None = None
        for _ in range(2):
            offers = port.legal(snap.item, Actor.PERSON, person, world)
            if not offers:
                self._close(world, held, PendingStatus.GONE, failure="nothing could be done to it")
                return Acted(transition=None)
            try:
                drawn = steers.drawn(snap.item, offers, world) if steers is not None and take is None else None
                leaning = steers.leaning(snap.item, world) if steers is not None else None
                offer, content, model = await self._choose(person, waiting, offers, take, drawn, leaning, world, clock)
            except ModelFailed as e:
                failure = str(e)
                break
            except _Unpinnable as e:
                self._close(world, held, PendingStatus.GONE, failure=str(e))
                return Acted(transition=None)
            try:
                transition = await port.apply(snap.item, offer.name, Actor.PERSON, person, content, world, clock)
            except ValueError as e:
                failure = f"{snap.item.provider} refused {offer.name!r}: {e}"
                continue  # the item moved under them: asked again from what it offers now
            if snap.conversation:
                written = Provenance(model=model, prompt_version=TRANSITION_PROMPT_VERSION) if model else None
                world.remember(_said(person, snap.item, offer.name, content, take, written, world, clock))
            self._close(world, held, PendingStatus.ACTED, transition=transition.seq)
            return Acted(transition=transition)
        assert failure is not None
        self._write(world, pending, snap.model_copy(update={"failure": failure}), Operation.UPDATE)
        return Acted(transition=None, failure=failure)

    async def _work_step(
        self,
        held: Held,
        person: Person,
        port: ProvidesTransitions,
        carrier: CarriesWork,
        waiting: Waiting,
        ref: EntityRef,
        course: CourseSnapshot,
        world: Store,
        clock: Clock,
    ) -> Acted:
        """The person's move on the work they are on, as its course says. Picking it up moves it to a started state,
        unless it is in one already, or makes the move its start take names; it fixes how long the work takes them.
        Finishing it moves it to done, or makes the move its finish take names. A finish its workflow cannot reach from
        where it stands takes the next started state they have not moved it to (a review), and the rest is booked for
        when they come back to it; one it reaches only through a screen nobody is offered (a resolution) is walked
        there by what done means. Several moves to choose from go to the model, which writes what they carry; one that
        takes nothing is taken with no model. Nothing that moves it forward leaves it stuck: they move on, and come
        back once someone else moves it."""
        now = clock.now()
        snap = held.pending
        offers = port.legal(snap.item, Actor.PERSON, person, world)
        # cancelling is theirs to choose beside moving it on, never alone, and never where a take pins how it ends: a
        # model picks it from what they know. Someone who never chooses for themselves (silent), or with no model to
        # choose for them, moves on with the first move forward.
        chooses = _picks(person) and self._words is not None
        cancels = [o for o in offers if o.means is TicketState.CANCELLED and o.unprompted]
        dropping = cancels if chooses and course.finish_take is None else []
        if course.started_at is None:
            first = self._first_asked(person, world, now)
            try:
                judged = await self._effort(person, carrier, waiting, world, clock)
            except ModelFailed as e:
                return self._still_owed(held, world, str(e))
            effort: timedelta | None = None
            effort_from: EffortFrom | None = None
            finishes: datetime | None = None
            believed: datetime | None = None
            if course.pinned_finish is not None:
                finishes, effort_from = max(course.pinned_finish, now), EffortFrom.TAKE
                effort = available_between(now, finishes, person, self._scenario.starts_at, first_ask=first)
                # what they believe it takes them, so a later pinned finish is a slip they know of; someone no
                # model speaks for believes it as it lands
                believed = (
                    after_available(now, judged[0], person, self._scenario.starts_at, first_ask=first)
                    if judged is not None
                    else finishes
                )
            elif judged is not None:
                effort, effort_from = judged
                believed = after_available(now, effort, person, self._scenario.starts_at, first_ask=first)
                finishes = believed
            # else: nobody judges how long it takes someone no model speaks for, and no estimate, pace or take says:
            # they take it up as a take pins it, and never finish it unprompted
            if finishes is not None and finishes <= now:
                # its finish is due already as they come to it (a take pins it then, or nothing is left of it):
                # they finish it outright, one move, rather than starting and finishing it in the same moment
                course = course.model_copy(
                    update={
                        "status": CourseStatus.WORKING,
                        "started_at": now,
                        "effort": effort,
                        "effort_from": effort_from,
                        "finishes_at": finishes,
                        "expects_at": believed,
                    }
                )
                self._write_course(world, ref, course, Operation.UPDATE)
                return await self._finish(
                    held,
                    person,
                    port,
                    waiting,
                    ref,
                    course,
                    course.finish_take,
                    offers,
                    dropping,
                    chooses,
                    world,
                    clock,
                )
            start = course.start_take
            if start is not None:
                named = _matching(start.take, offers)
                if named is None and _meaning(snap.item, world) is TicketState.STARTED:
                    starting: list[Offer] = []  # under way already: taking it up moves nothing
                elif named is None and start.take.strip().casefold() not in _MEANINGS:
                    return self._dropped(held, ref, course, _not_offered(person, start, offers), world)
                else:
                    starting = [named] if named is not None else []
            else:
                forward = [o for o in offers if o.means is TicketState.STARTED and o.unprompted]
                if _meaning(snap.item, world) not in (None, TicketState.OPEN):
                    forward = []
                starting = ([*forward, *dropping] if chooses else forward[:1]) if forward else []
            moved_to = list(course.moved_to)
            transition: Transition | None = None
            offer: Offer | None = None
            if starting:
                try:
                    told = waiting.model_copy(update={"shown": f"{waiting.shown}\n\n{_TAKING_UP}"})
                    offer, content = await self._pick(person, told, starting, start, world, clock)
                    transition = await port.apply(snap.item, offer.name, Actor.PERSON, person, content, world, clock)
                except ModelFailed as e:
                    return self._still_owed(held, world, str(e))
                except _Unpinnable as e:
                    return self._dropped(held, ref, course, str(e), world)
                except ValueError as e:
                    return self._still_owed(held, world, f"{snap.item.provider} refused it: {e}")
                moved_to.append(offer.to_state)
            state = transition.to_state if transition is not None else course.state
            begun = course.model_copy(
                update={
                    "status": CourseStatus.FINISHED
                    if offer is not None and offer.means in FINISHED
                    else CourseStatus.WORKING,
                    "started_at": now,
                    "effort": effort,
                    "effort_from": effort_from,
                    "finishes_at": finishes,
                    "expects_at": believed,
                    "moved_to": moved_to,
                    "state": state,
                }
            )
            self._write_course(world, ref, begun, Operation.UPDATE)
            self._close(world, held, PendingStatus.ACTED, transition=transition.seq if transition is not None else None)
            return Acted(transition=transition)
        return await self._finish(
            held, person, port, waiting, ref, course, course.finish_take, offers, dropping, chooses, world, clock
        )

    async def _finish(
        self,
        held: Held,
        person: Person,
        port: ProvidesTransitions,
        waiting: Waiting,
        ref: EntityRef,
        course: CourseSnapshot,
        take: Take | None,
        offers: list[Offer],
        dropping: list[Offer],
        chooses: bool,
        world: Store,
        clock: Clock,
    ) -> Acted:
        """The person's move once the work is done: to done, or the move its finish take names; else the next started
        state its workflow takes first; else walked to done; with cancelling beside them for a model to choose from
        what they know. A take naming a move the work does not offer is dropped, saying so; one naming a move that
        leaves the work unfinished (a comment) is made, and the work then waits for someone else to move it before they
        come back to it, as any item a person moves waits on them again only once someone else moves it."""
        snap = held.pending
        done = [o for o in offers if o.means is TicketState.DONE and o.unprompted]
        forward = [
            o
            for o in offers
            if o.means is TicketState.STARTED
            and o.unprompted
            and o.to_state not in course.moved_to
            and o.to_state != waiting.state
        ]
        # a finish its workflow reaches only through a screen a person is not offered (a resolution) is walked there
        walked = [o for o in offers if o.means is TicketState.DONE and not o.unprompted][:1]
        ahead = done or forward or walked
        named: Offer | None = None
        if take is not None:
            named = _matching(take.take, offers)
            if named is None and take.take.strip().casefold() not in _MEANINGS:
                return self._dropped(held, ref, course, _not_offered(person, take, offers), world)
            choices = [named] if named is not None else ahead
        else:
            choices = ([*ahead, *dropping] if chooses else ahead[:1]) if ahead else []
        if not choices:
            stuck = course.model_copy(
                update={"status": CourseStatus.HELD if course.started_at else CourseStatus.QUEUED, "stuck": True}
            )
            self._write_course(world, ref, stuck, Operation.UPDATE)
            self._close(world, held, PendingStatus.GONE, failure=f"nothing takes it forward from {waiting.state}")
            return Acted(transition=None)
        try:
            told = waiting.model_copy(update={"shown": f"{waiting.shown}\n\n{_FINISHING}"})
            offer, content = await self._pick(person, told, choices, take, world, clock)
            transition = await port.apply(snap.item, offer.name, Actor.PERSON, person, content, world, clock)
        except ModelFailed as e:
            return self._still_owed(held, world, str(e))
        except _Unpinnable as e:
            return self._dropped(held, ref, course, str(e), world)
        except ValueError as e:
            return self._still_owed(held, world, f"{snap.item.provider} refused it: {e}")
        moved = {"moved_to": [*course.moved_to, offer.to_state], "state": transition.to_state}
        if offer.means in FINISHED:
            ended = course.model_copy(update={**moved, "status": CourseStatus.FINISHED})
        elif named is not None:
            # the move its take names, which leaves the work unfinished: theirs is made
            ended = course.model_copy(update={**moved, "status": CourseStatus.HELD, "stuck": True})
        else:
            ended = course.model_copy(update=moved)
        self._write_course(world, ref, ended, Operation.UPDATE)
        self._close(world, held, PendingStatus.ACTED, transition=transition.seq)
        return Acted(transition=transition)

    async def _pick(
        self, person: Person, waiting: Waiting, offers: list[Offer], take: Take | None, world: Store, clock: Clock
    ) -> tuple[Offer, str]:
        """One of `offers` and what it carries: the only one, taken with nothing written when it requires nothing and
        no take asks for words; else the take's, worded by a model from its facts where it gives some; else the model's
        pick, which writes what it carries."""
        if take is not None:
            chosen = offers[0]
            exact = _exact(take, chosen, person)
            if not take.facts:
                return chosen, json.dumps(exact)
            offer, content, _ = await self._written(person, waiting, offers, take, chosen, None, world, clock)
            return offer, json.dumps({**json.loads(content), **exact})
        if len(offers) == 1 and not any(f.required for f in offers[0].fields):
            return offers[0], json.dumps({})
        offer, content, _ = await self._written(person, waiting, offers, None, None, None, world, clock)
        return offer, content

    def _dropped(self, held: Held, ref: EntityRef, course: CourseSnapshot, failure: str, world: Store) -> Acted:
        """A move a take pins that the work does not offer: dropped, saying so, and the work left where it stands
        until someone else moves it (`stuck`), so it is not tried again and again."""
        stuck = course.model_copy(
            update={"status": CourseStatus.HELD if course.started_at else CourseStatus.QUEUED, "stuck": True}
        )
        self._write_course(world, ref, stuck, Operation.UPDATE)
        self._close(world, held, PendingStatus.GONE, failure=failure)
        return Acted(transition=None)

    def _still_owed(self, held: Held, world: Store, failure: str) -> Acted:
        """A move a model failed to write, or the service refused: still owed, its failure kept, tried again later."""
        self._write(world, held.ref, held.pending.model_copy(update={"failure": failure}), Operation.UPDATE)
        return Acted(transition=None, failure=failure)

    async def ready(self, pending: EntityRef, world: Store, clock: Clock) -> Ready:
        """Whether the person's move is ready to land now, asked before anyone is told of it: an answer in words is
        ready once its words are written (`_worded`), and they are written now if a model failed to before. One that,
        read now, needs no answer is passed; one a model fails to write again is still owed, its failure kept."""
        snap = self._read(pending, world)
        if snap.status is not PendingStatus.PENDING or snap.plan is None:
            return Ready.READY
        if snap.answer is not None and not self._learned(snap, clock):
            return Ready.READY
        asked = _latest(world.events(), snap.item)
        if asked is None:
            return Ready.READY  # gone: `act` finds nothing to answer
        worded = await self._worded(self._people[snap.person], snap, asked, world, clock)
        self._write(world, pending, worded, Operation.UPDATE)
        if worded.status is PendingStatus.PASSED:
            return Ready.PASSED
        return Ready.READY if worded.answer is not None else Ready.FAILED

    def _learned(self, pending: PendingSnapshot, clock: Clock) -> bool:
        """Whether what the person knows changed (`Person.fact_changes`) between their answer's words and now: the
        words are written again, from what they know as they send it."""
        if pending.worded_at is None:
            return False
        person = self._people[pending.person]
        changes = [self._scenario.starts_at + c.after for c in person.fact_changes]
        return any(pending.worded_at < at <= clock.now() for at in changes)

    async def _answer(self, held: Held, person: Person, port: ProvidesTransitions, world: Store, clock: Clock) -> Acted:
        """The person's answer to an ask they owe, its words written (`ready`), delivered by its provider and kept
        as said; nothing when it needs no answer. A model that fails leaves it owed."""
        readied = await self.ready(held.ref, world, clock)
        snap = self._read(held.ref, world)
        if readied is Ready.FAILED:
            return Acted(transition=None, failure=snap.failure)
        if readied is Ready.PASSED or snap.answer is None:
            return Acted(transition=None)
        reply = PersonReply.model_validate_json(snap.answer)
        offer, content = _offer_of(reply)
        try:
            transition = await port.apply(snap.item, offer, Actor.PERSON, person, content, world, clock)
        except Unheard as e:
            # Said and taken by the service: kept as said, though its push to the agent failed; the run then stops.
            world.remember(reply.model_copy(update={"at": clock.now()}))
            self._close(world, Held(ref=held.ref, pending=snap), PendingStatus.ACTED, transition=e.seq, failure=str(e))
            raise
        except ValueError as e:
            self._close(
                world,
                Held(ref=held.ref, pending=snap),
                PendingStatus.GONE,
                failure=f"{snap.item.provider} refused {offer!r}: {e}",
            )
            return Acted(transition=None)
        world.remember(reply.model_copy(update={"at": clock.now()}))
        self._close(world, Held(ref=held.ref, pending=snap), PendingStatus.ACTED, transition=transition.seq)
        return Acted(transition=transition)

    async def _worded(
        self, person: Person, pending: PendingSnapshot, asked: WorldEvent, world: Store, clock: Clock
    ) -> PendingSnapshot:
        """The answer's words, written now (`replier.write`) from everything the person can see by now, and kept with
        the record until it lands, so a fork replays them. What they answer is the newest message of the conversation
        they owe an answer in — the ask, or the latest follow-up that arrived while it was owed — so a follow-up that
        brought news is read as such, not answered as though it never came; the answer still lands on the ask
        (`in_reply_to`). An ask that, read so, needs no answer passes; a model that fails leaves it owed, its failure
        kept, and it is written again on the next look or at its moment."""
        assert pending.plan is not None
        history = world.events()
        item = pending.item
        newest = max(
            (
                e
                for e in history
                if e.entity.provider == item.provider
                and e.entity.kind is item.kind
                and e.entity.external_id in pending.follow_ups
                and isinstance(e.after, MessageSnapshot)
            ),
            key=lambda e: e.seq,
            default=asked,
        )
        try:
            reply = await self._replier.write(
                person,
                max(asked, newest, key=lambda e: e.seq),
                Plan.model_validate_json(pending.plan),
                history,
                world,
                clock,
            )
        except ModelFailed as e:
            return pending.model_copy(update={"failure": str(e)})
        if reply is None:
            return pending.model_copy(update={"status": PendingStatus.PASSED, "failure": None, "answer": None})
        on_ask = reply.model_copy(update={"in_reply_to": pending.item})
        return pending.model_copy(
            update={"answer": on_ask.model_dump_json(), "failure": None, "worded_at": clock.now()}
        )

    async def _choose(
        self,
        person: Person,
        waiting: Waiting,
        offers: list[Offer],
        take: Take | None,
        drawn: Offer | None,
        leaning: str | None,
        world: Store,
        clock: Clock,
    ) -> tuple[Offer, str, str | None]:
        """The offer the person takes and what it carries, as a JSON object: a pinned one, one the provider's own
        odds drew (its words a model's), or the model's pick; and the model that wrote any of it."""
        pinned: Offer | None = drawn
        if take is None:
            offers = [o for o in offers if o.unprompted]
            if not offers:
                raise _Unpinnable(f"nothing {person.key} would do unprompted is offered")
            return await self._written(person, waiting, offers, None, pinned, leaning, world, clock)
        pinned = _matching(take.take, offers)
        if pinned is None:
            raise _Unpinnable(
                f"{person.key} is pinned to take {take.take!r}, which is not offered: "
                + ", ".join(repr(o.name) for o in offers)
            )
        exact = _exact(take, pinned, person)
        if not take.facts:
            return pinned, json.dumps(exact), None
        offer, content, model = await self._written(person, waiting, offers, take, pinned, leaning, world, clock)
        written: dict[str, str] = json.loads(content)
        return offer, json.dumps({**written, **exact}), model

    async def _written(
        self,
        person: Person,
        waiting: Waiting,
        offers: list[Offer],
        take: Take | None,
        pinned: Offer | None,
        leaning: str | None,
        world: Store,
        clock: Clock,
    ) -> tuple[Offer, str, str]:
        if self._words is None:
            raise RunRefused(f"a model writes what {person.key} does, and no model is configured")
        behaviour = person.reply
        assert isinstance(behaviour, Answers | Scripted)
        history = world.events()
        system = transition_prompt(
            person,
            behaviour,
            take,
            pinned,
            leaning,
            clock.now(),
            self._scenario.starts_at,
            work_part(person, world, clock.now()),
        )
        shown = asked_to_move(waiting, offers)
        context = (
            await self._words.context(person, behaviour, history[-1], history, world, clock, answers=False)
            if history
            else []
        )
        messages = [ModelMessage(speaker=Speaker.ASKER, text=shown + "\n\n" + (context[0].text if context else ""))]
        said = WrittenTransition(take="")
        for attempt in range(2):
            written = await self._words.ask_model(
                person,
                Wrote.TRANSITION,
                waiting.item,
                system,
                messages,
                WrittenTransition,
                TRANSITION_PROMPT_VERSION,
                world,
                clock,
            )
            said = written.answer
            offer = pinned or _matching(said.take, offers, exact=True)
            given = {f.name: f.value for f in said.fields}
            why: str | None = None
            if offer is None:
                why = f"{said.take!r} is not one of the things you can do now."
            else:
                try:
                    offer.refuse_content(given, person.key)
                except ValueError as e:
                    why = f"{e}."
            if why is None and offer is not None:
                return offer, json.dumps({k: v for k, v in given.items() if v.strip()}), written.model
            if attempt == 0:
                messages = [
                    *messages,
                    ModelMessage(speaker=Speaker.MODEL, text=said.model_dump_json()),
                    ModelMessage(speaker=Speaker.ASKER, text=f"{why} Pick again from what is listed."),
                ]
        raise ModelFailed(f"the model had {person.key} take what is not offered, twice: {said.take!r}")

    # -- the engine's own record ----------------------------------------------------------------------------------

    def _read(self, pending: EntityRef, world: Store) -> PendingSnapshot:
        stored = world.get(pending)
        if stored is None:
            raise LookupError(f"the engine holds no pending item {pending.external_id}")
        return PendingSnapshot.model_validate_json(stored.body)

    def _close(
        self,
        world: Store,
        held: Held,
        status: PendingStatus,
        *,
        transition: int | None = None,
        failure: str | None = None,
    ) -> None:
        closed = held.pending.model_copy(update={"status": status, "transition": transition, "failure": failure})
        self._write(world, held.ref, closed, Operation.UPDATE)

    def _write(self, world: Store, ref: EntityRef, pending: PendingSnapshot, operation: Operation) -> None:
        world.apply(
            Change(
                entity=ref,
                operation=operation,
                actor=Actor.SCENARIO,
                body=pending.model_dump_json(),
                parent=pending.person,
                after=pending,
            )
        )


def _offer_of(reply: PersonReply) -> tuple[str, str]:
    """The move a written answer is, and what it carries: a decision and its inputs, a control used and what its
    form holds, or words written back."""
    if not reply.answers:
        return AUTOMATIC_REPLY, json.dumps({TEXT: reply.text})
    if reply.decides is not None:
        return reply.decides.decision, json.dumps(reply.decides.inputs)
    if reply.press is not None:
        carried = {TEXT: reply.text} if reply.text != reply.press.label else {}
        if reply.press.picks is not None:
            carried[PICKS] = reply.press.picks
        if reply.press.form:
            carried[FORM] = json.dumps([f.model_dump() for f in reply.press.form])
        return reply.press.action_id, json.dumps(carried)
    return REPLY, json.dumps({TEXT: reply.text})


def _said(
    person: Person,
    item: EntityRef,
    offer: str,
    content: str,
    take: Take | None,
    written_by: Provenance | None,
    world: Store,
    clock: Clock,
) -> PersonReply:
    """An answer the engine picked or a take pinned, as the run keeps it said: its words, the control it used and what
    its form held, the decision it made, and its take's facts (a rule reads a decision's answer from its inputs,
    `checks.facts`). Its words are the take's exact ones, a model's from the take's facts, or a model's pick."""
    given: dict[str, str] = json.loads(content)
    writing = Writing.CONVERSING if take is None else Writing.VERBATIM if not take.facts else Writing.SCRIPT
    facts = list(take.facts) if take is not None else []
    if offer == REPLY:
        return PersonReply(
            person=person.key,
            in_reply_to=item,
            text=given[TEXT],
            at=clock.now(),
            writing=writing,
            facts=facts,
            written_by=written_by,
        )
    if item.kind is EntityKind.INBOX_ITEM:
        return PersonReply(
            person=person.key,
            in_reply_to=item,
            text=decision_text(offer, given),
            at=clock.now(),
            decides=Decides(decision=offer, inputs=given),
            writing=writing,
            facts=facts,
            written_by=written_by,
        )
    shown = next((e.after for e in reversed(world.events()) if e.entity == item and e.after is not None), None)
    control = (
        next((a for a in shown.actions if a.action_id == offer), None) if isinstance(shown, MessageSnapshot) else None
    )
    form = _FORM_INPUTS.validate_json(given[FORM]) if FORM in given and given[FORM].strip() else []
    text = given[TEXT] if TEXT in given else "\n".join(f.value for f in form) or (control.label if control else offer)
    return PersonReply(
        person=person.key,
        in_reply_to=item,
        text=text,
        at=clock.now(),
        press=Press(
            action_id=offer,
            label=control.label if control is not None else offer,
            value=control.value if control is not None else None,
            picks=given[PICKS] if PICKS in given else None,
            form=form,
        ),
        writing=writing,
        facts=facts,
        written_by=written_by,
    )


_FORM_INPUTS: TypeAdapter[list[FormInput]] = TypeAdapter(list[FormInput])


def _latest(events: list[WorldEvent], item: EntityRef) -> WorldEvent | None:
    """The ask as it reads now: its last version."""
    return next((e for e in reversed(events) if e.entity == item and e.after is not None), None)


def _reworded(events: list[WorldEvent], item: EntityRef, since: int, now: WorldEvent) -> bool:
    """Whether the ask reads otherwise now than it did at `since`: its words, or what a reader can use on it."""
    before = next((e for e in reversed(events) if e.entity == item and e.seq <= since and e.after is not None), None)
    if before is None:
        return False
    was, is_ = before.after, now.after
    if isinstance(was, MessageSnapshot) and isinstance(is_, MessageSnapshot):
        return (was.text, was.actions) != (is_.text, is_.actions)
    return False


class _Unpinnable(Exception):
    """A pinned transition the item does not offer: the person cannot take what is not there."""


async def happen(
    scenario: Scenario, port: ProvidesTransitions, happening: TicketHappening, world: Store, clock: Clock
) -> Transition | None:
    """`People.happen` for one provider: a ticket happening landed through `port`'s `apply`. Nothing when the ticket
    is gone, or already where the move would take it."""
    seeded = scenario.happening_ticket(happening)
    if not isinstance(port, HoldsSeeded):
        raise RunRefused(f"a happening acts on a seeded {seeded.provider} ticket, and {seeded.provider} holds none")
    item = port.seeded(scenario, seeded, world)
    if item is None:
        return None
    person = next(p for p in scenario.people if p.key == happening.person)
    name, given = ticket_move(happening.action, {p.key: p.email for p in scenario.people if p.email})
    if isinstance(happening.action, Moves) and _matching(name, port.legal(item, Actor.PERSON, person, world)) is None:
        return None  # already where it would go
    return await move(port, item, name, Actor.PERSON, person, given, world, clock)


async def move(
    port: ProvidesTransitions,
    item: EntityRef,
    said: str,
    by: Actor,
    who: Person | None,
    given: Mapping[str, str],
    world: Store,
    clock: Clock,
) -> Transition:
    """One move made outright, by a person or the scenario: the offer `said` names (by its name, the state it
    reaches, its label, or what a ticket's state means), carrying `given`. Refused when nothing offered is it."""
    offer = _matching(said, port.legal(item, by, who, world))
    if offer is None:
        raise ValueError(f"{item.provider} {item.external_id} offers no {said!r} now")
    return await port.apply(item, offer.name, by, who, json.dumps(dict(given)), world, clock)


def pinned(person: Person, provider: ProviderKey, nth: int, nth_any: int | None, moved: int = 0) -> int | None:
    """Which of `person`'s takes pins the item that is their nth in `provider` and their `nth_any` of all, by its
    place in `Person.takes`: one naming the provider and the item wins, then one naming the provider for every item,
    then one naming the item among all their asks (`nth_any`, None for anything but an ask), then one for every item
    anywhere. Takes that name the same item are its moves in order: the one for the move after the `moved` they
    already made on it, the last holding from then on (an ask back, then an approval)."""
    for wanted in (
        (provider, nth),
        (provider, None),
        *([(None, nth_any)] if nth_any is not None else []),
        (None, None),
    ):
        found = [n for n, t in enumerate(person.takes) if (t.provider, t.nth) == wanted]
        if found:
            return found[min(moved, len(found) - 1)]
    return None


def takes_naming(person: Person, provider: ProviderKey, nth: int) -> list[Take]:
    """Every take of `person`'s naming their nth piece of work in `provider`, in their order, at the first of these
    that names any: the provider and its place, the provider, every item anywhere (as `pinned` orders them, asks
    aside)."""
    for wanted in ((provider, nth), (provider, None), (None, None)):
        found = [t for t in person.takes if (t.provider, t.nth) == wanted]
        if found:
            return found
    return []


def _place(person: Person, take: Take | None) -> int | None:
    """Where `take` stands in `Person.takes`; None for none, or one they no longer hold (a fork took it away)."""
    if take is None:
        return None
    return next((n for n, t in enumerate(person.takes) if t == take), None)


def _matching(said: str, offers: Sequence[Offer], *, exact: bool = False) -> Offer | None:
    """The offer `said` names, in any case: by its name, else (unless `exact`) by the state it reaches, the label the
    person sees on it, or what a ticket's state means."""
    wanted = said.strip().casefold()
    by_name = next((o for o in offers if o.name.casefold() == wanted), None)
    if by_name is not None or exact:
        return by_name
    for read in (
        lambda o: o.to_state,
        lambda o: o.label,
        lambda o: o.means.value if o.means is not None else None,
    ):
        found = next((o for o in offers if (read(o) or "").casefold() == wanted), None)
        if found is not None:
            return found
    return None


def _exact(take: Take, offer: Offer, person: Person) -> dict[str, str]:
    """What a take fixes of what its offer carries: its `fields`, the form it types, and its `verbatim` words in the
    offer's text (the first field it does not otherwise fix)."""
    exact = dict(take.fields)
    if take.form:
        exact[FORM] = json.dumps([f.model_dump() for f in take.form])
    if take.verbatim is not None:
        text = next((f for f in offer.fields if f.name not in exact and f.name not in (FORM, PICKS)), None)
        if text is None:
            raise _Unpinnable(f"{take.take!r} takes no words, and {person.key}'s take gives `verbatim` ones")
        exact[text.name] = take.verbatim
    return exact


def _places(held: list[Held], item: EntityRef, conversation: bool) -> tuple[int, int | None]:
    """Where `item` stands among the items held on a person, from 1, in the order each began to wait: among those in
    its provider, and, for an ask (`conversation`), among every ask of theirs in any provider (None for anything
    else: a ticket is counted only in its own provider). One not yet held comes after every one."""
    every = list(dict.fromkeys(h.pending.item for h in held if h.pending.take != AUTOMATIC_REPLY))
    here = [i for i in every if i.provider == item.provider]
    nth = here.index(item) + 1 if item in here else len(here) + 1
    if not conversation:
        return nth, None
    asks = list(
        dict.fromkeys(h.pending.item for h in held if h.pending.take != AUTOMATIC_REPLY and h.pending.conversation)
    )
    return nth, asks.index(item) + 1 if item in asks else len(asks) + 1


def _order(course: CourseSnapshot) -> tuple[bool, bool, date, bool, int, datetime, str]:
    """Which of a person's work comes first: work already under way when it became theirs, then the day it is due
    (none last), its priority (none last), when it became theirs, then its id, so the same world always orders it the
    same way."""
    return (
        not course.under_way,
        course.due is None,
        course.due if course.due is not None else date.max,
        course.rank is None,
        course.rank if course.rank is not None else 0,
        course.since,
        course.item.external_id,
    )


def _work_places(plans: list[tuple[EntityRef, CourseSnapshot]], waiting: list[EntityRef]) -> dict[EntityRef, int]:
    """Where each piece of work stands among the work handed to a person in its provider, from 1, in the order each
    became theirs (what a take's `nth` counts): work they already carried first, then what waits on them now."""
    places: dict[EntityRef, int] = {}
    counted: dict[ProviderKey, int] = {}
    for item in [*(c.item for _, c in plans), *waiting]:
        if item in places:
            continue
        counted[item.provider] = counted.get(item.provider, 0) + 1
        places[item] = counted[item.provider]
    return places


def _not_offered(person: Person, take: Take, offers: Sequence[Offer]) -> str:
    return f"{person.key} is pinned to take {take.take!r}, which is not offered: " + ", ".join(
        repr(o.name) for o in offers
    )


def _under_way_at_setup(item: EntityRef, world: Store) -> bool:
    """Whether a ticket stood started at setup (wake 0, as the scenario set it up) with the assignee it has now: work
    its person was already on as the run began. Work started or handed to them since is new to them."""
    seeded: TicketSnapshot | None = None
    latest: TicketSnapshot | None = None
    for event in world.events(kinds=[EntityKind.TICKET], providers=[item.provider]):
        if event.entity == item and isinstance(event.after, TicketSnapshot):
            latest = event.after
            if event.wake == 0:
                seeded = event.after
    return (
        seeded is not None
        and latest is not None
        and seeded.state is TicketState.STARTED
        and seeded.assignee_email == latest.assignee_email
    )


def _meaning(item: EntityRef, world: Store) -> TicketState | None:
    """What a ticket's state means now, from its latest version in the log; None for an item that is no ticket."""
    latest: TicketState | None = None
    for event in world.events(kinds=[EntityKind.TICKET], providers=[item.provider]):
        if event.entity == item and isinstance(event.after, TicketSnapshot):
            latest = event.after.state
    return latest


def last_turn(world: Store, item: EntityRef, person: str) -> int:
    """The seq of the last transition anyone but `person` made on `item`; 0 when none has."""
    latest = 0
    after: str | None = None
    while True:
        page = world.children(item.provider, EntityKind.TRANSITION, item_parent(item), after=after, limit=_SCAN)
        for stored in page:
            moved = Transition.model_validate_json(stored.body)
            if not (moved.by is Actor.PERSON and moved.who == person):
                latest = max(latest, stored.seq)
        if len(page) < _SCAN:
            return latest
        after = page[-1].entity.external_id


def transition_prompt(
    person: Person,
    behaviour: Answers | Scripted,
    take: Take | None,
    pinned: Offer | None,
    leaning: str | None,
    today: datetime,
    starts_at: datetime,
    work: str = "",
) -> str:
    facts, stale = person.knows_at(today, starts_at)
    believed = believed_part(behaviour, stale)
    decided = f"\nHow people tend to act here: {leaning}\n" if leaning else ""
    if pinned is not None:
        why = bulleted(take.facts) if take is not None and take.facts else "- what you know"
        decided += f'\nYou have decided: "{pinned.name}", and what you write with it says:\n{why}\nPick that.\n'
    return TRANSITION_PROMPT.format(
        who=who_is(person),
        known=bulleted(facts),
        believed=believed,
        work=work,
        decided=decided,
        or_believe=" or in what you believe" if believed else "",
        helpfulness=f"With what is asked: {HELPFULNESS[behaviour.helpfulness]}",
        voice=voice_rule(behaviour),
        today=today.strftime("%A %d %B %Y"),
    )


def estimate_prompt(person: Person, behaviour: Answers | Scripted, today: datetime, starts_at: datetime) -> str:
    facts, stale = person.knows_at(today, starts_at)
    return ESTIMATE_PROMPT.format(
        who=who_is(person),
        known=bulleted(facts),
        believed=believed_part(behaviour, stale),
        today=today.strftime("%A %d %B %Y"),
    )


def asked_to_move(waiting: Waiting, offers: Sequence[Offer]) -> str:
    """The item as the person is shown it, and each thing they can do with it, with what it takes."""
    lines = [
        f"What waits on you, as {waiting.item.provider} shows it:",
        waiting.shown,
        "",
        "What you can do with it now:",
    ]
    for offer in offers:
        said = f" ({offer.description})" if offer.description else ""
        lines.append(f'- "{offer.name}": it becomes {offer.to_state}{said}')
        for given in offer.fields:
            needed = "required" if given.required else "optional"
            lines.append(f'    field "{given.name}" ({needed}): {given.description}')
    return "\n".join(lines)
