"""The people engine (`docs/design-transitions.md`): one way every person acts on what waits on them, in a run and
in a standing world alike, through any provider that implements `ports.transitions.ProvidesTransitions`: every ask
a person answers in words (a message to them, an item in the agent's own product) on every provider that has them,
and every other item (a ticket, a declared service's request) on the providers the scenario plays
(`Scenario.played()`).

1. **Look.** After every wake (and every `advance` of a standing world) each provider is asked what waits on each
   person (`items_for`). An item not yet held is pending on that person: written to the log as a `PendingSnapshot`
   (actor SCENARIO), so a fork reads what was owed at its checkpoint. One held that no longer waits on them is gone.
   A person acts once per turn: after their own move an item waits on them again only once someone else moves it.
2. **Book.** Its moment is pinned by the scenario (`Person.takes` with `after`) or drawn as their answers are
   (`moments.draw_for`): from the run's seed, the person and the item, in their available time. A person who never
   acts (`Silent`, or a script that goes silent with nothing pinned) holds the item pending with no moment.
3. **Act.** At the moment the provider is asked for the legal offers. A pinned offer is taken (its words exactly as
   pinned, or a model's from its facts); otherwise a model picks one and writes what it carries, from what the person
   can see: the item as the service shows it, their facts at that moment, their conversations
   (`person-transition/1`). A pick that is not offered is asked again once; then the failure is recorded and the item
   stays owed, tried again later. The provider applies it through its own code path.
4. **Kept.** Every model call is kept and replayed by its context, as the replier's are (`PersonCall`).

**Conversations** (decision 2: one item per conversation) are planned and worded by the person's script and voice
(`application.replier`): a new message to them is an ask (`replier.plan`: whether and when they answer it, from
their script's steps, or conversing), unless it follows up an answer they already owe in the same conversation, which
then moves sooner when they are `reminded`; an ask edited before its answer is planned again from its new words; a
person away while a delegate covers sends their automatic reply at once (a record of its own, due then). The answer's
words are written as it is planned (`replier.write`, again on the next look or at its moment if a model fails) and
kept on the record (`PendingSnapshot.answer`), so a fork replays them; at its moment the provider delivers it, and it
is kept as said. An ask that, read so, needs no answer passes (`PendingStatus.PASSED`): nobody hears of anything. A
pinned take (`Person.takes`) wins over the script for the nth item it names, and on a provider the scenario names in
`transitions_on` the model picks among the message's offers (step 3).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import Field, TypeAdapter

from minutehand.application.moments import (
    automatic_reply,
    decision_text,
    draw_for,
    follow_up_of,
    messages_to,
    pinned_at,
    sooner,
)
from minutehand.application.refusals import RunRefused
from minutehand.application.replier import HELPFULNESS, PeopleReplier, believed_part, bulleted, voice_rule, who_is
from minutehand.domain.clock import Drawn, DrawnFrom
from minutehand.domain.conversation import ModelMessage, Provenance, Speaker, Wrote
from minutehand.domain.people import Decides, PersonReply, Plan, Press, Writing
from minutehand.domain.scenario import (
    AfterScript,
    Answers,
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
    Actor,
    Change,
    EntityKind,
    EntityRef,
    InboxItemSnapshot,
    MessageSnapshot,
    Operation,
    PendingSnapshot,
    PendingStatus,
    WorldEvent,
)
from minutehand.ports.clock import Clock
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.model import ModelFailed
from minutehand.ports.people import Replier
from minutehand.ports.store import Store
from minutehand.ports.transitions import HoldsSeeded, ProvidesTransitions, SteersPeople

TRANSITION_PROMPT_VERSION = "person-transition/1"
"""Changes whenever TRANSITION_PROMPT or what the person is shown changes a word."""

TRANSITION_PROMPT = """\
You are {who}. You are at work. Something in a tool your team uses waits on you. You are shown it as you see it \
there, what you can do with it now and what each takes, and what you can see of your conversations.

What you know:
{known}
{believed}{decided}
Rules:
- Pick exactly one of the things you can do, by its name exactly as listed, in "take".
- Fill "fields" with one entry per field you fill, each by its name exactly as listed: a required field always, an \
optional one only when you have something to say in it. Write each value as you would type it, from what you know.
- Stay consistent with what you said before.
- You decide only from what you know{or_believe}. {helpfulness}
- {voice}
- Today is {today}.
"""

OWN = "minutehand"
"""The provider the engine's own records are kept under, as the run loop's are."""

_SCAN = 500


class WrittenField(Model):
    name: str = Field(description="The field's name, exactly as listed")
    value: str = Field(description="What you write in it")


class WrittenTransition(Model):
    """What the model answers for one item pending on one person: the transition taken, and what it carries."""

    take: str = Field(description="The name of what you do, exactly as listed")
    fields: list[WrittenField] = Field(default=[], description="One per field you fill")


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
                    elif (
                        key in played
                        or self._pins(person, port, item.item, held, world, conversation=False) is not None
                    ):
                        made = self._pend(person, item, held, world, clock)
                        if made is not None:
                            booked.append(made)
            if asks:
                await self._converse(person, asks, events, world, clock, booked, moved)
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
            if automatic is not None:
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
            moved.append(await self._replanned(person, h, now, [e for e in events if e.seq <= now.seq], world, clock))

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
        """An ask of its own: what the person's script makes of it (`replier.plan`) and, when they answer, its words
        (`_worded`), held with its plan."""
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
        if plan is not None:
            pending = await self._worded(person, pending, asked, world, clock)
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

    async def _replanned(
        self, person: Person, held: Held, asked: WorldEvent, history: list[WorldEvent], world: Store, clock: Clock
    ) -> Booking:
        """An ask whose words changed before its answer, or whose person changed: planned and worded again from the
        message as it reads now, with nothing it was owed before counted."""
        owed = [(ref, at) for ref, at in self._owed(world) if ref != held.pending.item]
        plan = self._replier.plan(person, asked, history, owed)
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
        if plan is not None:
            changed = await self._worded(person, changed, asked, world, clock)
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
                    moved.append(self._repended(person, h, world, clock))
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
        found = pinned(person, item.provider, nth, nth_any)
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
        drawn = self._drawn(person, item.item, take, asked.sim_time if asked is not None else clock.now(), world)
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

    def _drawn(self, person: Person, item: EntityRef, take: Take | None, began: datetime, world: Store) -> Drawn | None:
        """When the person acts on `item`, which began to wait on them at `began`: exactly `after` it when a take
        says, else drawn as their answers are (within the take's window, or the provider's), or never for someone
        who neither is pinned nor picks for themselves."""
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
        drawn = self._drawn(person, p.item, take, began, world)
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
        take = person.takes[snap.pinned] if snap.pinned is not None else None
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

    async def ready(self, pending: EntityRef, world: Store, clock: Clock) -> Ready:
        """Whether the person's move is ready to land now, asked before anyone is told of it: an answer in words is
        ready once its words are written (`_worded`), and they are written now if a model failed to before. One that,
        read now, needs no answer is passed; one a model fails to write again is still owed, its failure kept."""
        snap = self._read(pending, world)
        if snap.status is not PendingStatus.PENDING or snap.plan is None or snap.answer is not None:
            return Ready.READY
        asked = _latest(world.events(), snap.item)
        if asked is None:
            return Ready.READY  # gone: `act` finds nothing to answer
        worded = await self._worded(self._people[snap.person], snap, asked, world, clock)
        self._write(world, pending, worded, Operation.UPDATE)
        if worded.status is PendingStatus.PASSED:
            return Ready.PASSED
        return Ready.READY if worded.answer is not None else Ready.FAILED

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
        """The answer's words, written now from the ask as it reads (`replier.write`) and kept with the record until
        it lands, so a fork replays them: an ask that needs no answer passes; a model that fails leaves it owed, its
        failure kept, and it is written again on the next look or at its moment."""
        assert pending.plan is not None
        history = [e for e in world.events() if e.seq <= asked.seq]
        try:
            reply = await self._replier.write(
                person, asked, Plan.model_validate_json(pending.plan), history, world, clock
            )
        except ModelFailed as e:
            return pending.model_copy(update={"failure": str(e)})
        if reply is None:
            return pending.model_copy(update={"status": PendingStatus.PASSED, "failure": None, "answer": None})
        return pending.model_copy(update={"answer": reply.model_dump_json(), "failure": None})

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
        system = transition_prompt(person, behaviour, take, pinned, leaning, clock.now(), self._scenario.starts_at)
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
    its form held, the decision it made. A decision's facts are what it carries, its inputs as given (what a rule's
    `{ask.facts}` reads); anything else carries its take's facts. Its words are the take's exact ones, a model's from
    the take's facts, or a model's pick."""
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
            facts=[v for v in given.values() if v.strip()],
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
    name, given = ticket_move(happening.action, {p.key: p.email for p in scenario.people})
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


def pinned(person: Person, provider: ProviderKey, nth: int, nth_any: int | None) -> int | None:
    """Which of `person`'s takes pins the item that is their nth in `provider` and their `nth_any` of all, by its
    place in `Person.takes`: one naming the provider and the item wins, then one naming the provider for every item,
    then one naming the item among all their asks (`nth_any`, None for anything but an ask), then one for every item
    anywhere."""
    for wanted in (
        (provider, nth),
        (provider, None),
        *([(None, nth_any)] if nth_any is not None else []),
        (None, None),
    ):
        found = next((n for n, t in enumerate(person.takes) if (t.provider, t.nth) == wanted), None)
        if found is not None:
            return found
    return None


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
        decided=decided,
        or_believe=" or in what you believe" if believed else "",
        helpfulness=f"With what is asked: {HELPFULNESS[behaviour.helpfulness]}",
        voice=voice_rule(behaviour),
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
