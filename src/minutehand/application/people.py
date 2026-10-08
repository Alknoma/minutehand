"""The people engine (`docs/design-transitions.md`): one way every person acts on what waits on them, in a run and
in a standing world alike, through any provider that implements `ports.transitions.ProvidesTransitions` and that the
scenario names in `transitions_on`.

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
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import Field

from minutehand.application.moments import draw_for, messages_to, pinned_at
from minutehand.application.refusals import RunRefused
from minutehand.application.replier import HELPFULNESS, PeopleReplier, believed_part, bulleted, voice_rule, who_is
from minutehand.domain.clock import Drawn
from minutehand.domain.conversation import ModelMessage, Speaker, Wrote
from minutehand.domain.scenario import (
    AfterScript,
    Answers,
    Model,
    Person,
    ProviderKey,
    Scenario,
    Scripted,
    Silent,
    Take,
)
from minutehand.domain.transitions import Offer, Transition, Waiting, item_parent
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    Operation,
    PendingSnapshot,
    PendingStatus,
)
from minutehand.ports.clock import Clock
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.model import ModelFailed
from minutehand.ports.store import Store
from minutehand.ports.transitions import ProvidesTransitions, SteersPeople

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


@dataclass(frozen=True)
class Looked:
    booked: list[Booking]
    gone: list[EntityRef]


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
    if not scenario.played():
        return []
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
    """The engine over the providers a scenario names in `transitions_on`, each found by `provider` and held to
    `ProvidesTransitions`; refused before anything runs when a model writes what someone does and none is configured,
    or when a provider it plays cannot be."""

    def __init__(
        self, scenario: Scenario, provider: Callable[[ProviderKey], object], model: LanguageModel | None
    ) -> None:
        needing = needs_model(scenario)
        if needing and model is None:
            raise RunRefused(f"a model writes what {'; '.join(needing)} do, and no model is configured")
        self._scenario = scenario
        self._provider = provider
        self._model = model
        self._replier = PeopleReplier(scenario, model) if model is not None else None
        self._people = {p.key: p for p in scenario.people}
        for key in scenario.played():
            self.port(key)

    @property
    def scenario(self) -> Scenario:
        return self._scenario

    def port(self, key: ProviderKey) -> ProvidesTransitions:
        found = self._provider(key)
        if not isinstance(found, ProvidesTransitions):
            raise RunRefused(f"the scenario has people act through transitions on {key}, which offers none")
        return found

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

    def look(self, world: Store, clock: Clock) -> Looked:
        """What waits on each person now, against what the engine holds: each new item pending on them, written and
        booked; each held one that no longer waits on them, gone."""
        booked: list[Booking] = []
        gone: list[EntityRef] = []
        for key in self._scenario.played():
            port = self.port(key)
            for person in self._scenario.people:
                waiting = port.items_for(person, world)
                held = [h for h in self.held(person.key, world) if h.pending.item.provider == key]
                here = [w.item for w in waiting]
                for h in held:
                    if h.pending.status is PendingStatus.PENDING and h.pending.item not in here:
                        self._close(world, h, PendingStatus.GONE)
                        gone.append(h.ref)
                for item in waiting:
                    made = self._pend(person, item, held, world, clock)
                    if made is not None:
                        booked.append(made)
        return Looked(booked=booked, gone=gone)

    def _pend(self, person: Person, item: Waiting, held: list[Held], world: Store, clock: Clock) -> Booking | None:
        mine = [h for h in held if h.pending.item == item.item]
        if any(h.pending.status is PendingStatus.PENDING for h in mine):
            return None
        turn = last_turn(world, item.item, person.key)
        if any(h.pending.status is PendingStatus.ACTED and h.pending.turn == turn for h in mine):
            return None
        items = list(dict.fromkeys(h.pending.item for h in held))
        nth = items.index(item.item) + 1 if item.item in items else len(items) + 1
        take = pinned_take(person, item.item.provider, nth)
        port = self.port(item.item.provider)
        within = port.within(item.item, world) if isinstance(port, SteersPeople) else None
        now = clock.now()
        drawn: Drawn | None = None
        if take is not None and take.after is not None:
            drawn = pinned_at(self._scenario, now, take.after)
        elif take is not None or _picks(person):
            behaviour = person.reply
            assert isinstance(behaviour, Answers | Scripted)
            sent = messages_to(person, world.events())
            drawn = draw_for(
                self._scenario,
                person,
                item.item,
                now,
                first_asked=sent[0].sim_time if sent else now,
                within=within,
                delay=behaviour.delay,
            )
        pending = PendingSnapshot(
            person=person.key,
            item=item.item,
            nth=nth,
            state=item.state,
            turn=turn,
            status=PendingStatus.PENDING,
            due_at=drawn.lands_at if drawn is not None else None,
            take=take.take if take is not None else None,
            drawn=drawn,
        )
        ref = EntityRef(
            provider=OWN,
            kind=EntityKind.PENDING,
            external_id=f"{person.key}|{item.item.provider}|{item.item.kind.value}|{item.item.external_id}|"
            f"{world.head() + 1:010d}",
        )
        self._write(world, ref, pending, Operation.CREATE)
        return Booking(pending=ref, person=person.key, item=item.item, at=pending.due_at, drawn=drawn)

    # -- acting ---------------------------------------------------------------------------------------------------

    def pending(self, pending: EntityRef, world: Store) -> PendingSnapshot:
        """The engine's record of one item pending on a person, as it stands."""
        return self._read(pending, world)

    def heard_of(self, pending: EntityRef, world: Store, clock: Clock) -> bool:
        """Whether the service tells the agent when this person's move lands (`ProvidesTransitions.heard_of`)."""
        held = self._read(pending, world)
        return self.port(held.item.provider).heard_of(held.item, world, clock)

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
        take = pinned_take(person, snap.item.provider, snap.nth)
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
                offer, content = await self._choose(person, waiting, offers, take, drawn, leaning, world, clock)
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
            self._close(world, held, PendingStatus.ACTED, transition=transition.seq)
            return Acted(transition=transition)
        assert failure is not None
        self._write(world, pending, snap.model_copy(update={"failure": failure}), Operation.UPDATE)
        return Acted(transition=None, failure=failure)

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
    ) -> tuple[Offer, str]:
        """The offer the person takes and what it carries, as a JSON object: a pinned one, one the provider's own
        odds drew (its words a model's), or the model's pick."""
        pinned: Offer | None = drawn
        if take is not None:
            pinned = _matching(take.take, offers)
            if pinned is None:
                raise _Unpinnable(
                    f"{person.key} is pinned to take {take.take!r}, which is not offered: "
                    + ", ".join(repr(o.name) for o in offers)
                )
            if take.verbatim is not None:
                text = next((f for f in pinned.fields), None)
                if text is None:
                    raise _Unpinnable(f"{take.take!r} takes no words, and {person.key}'s take gives `verbatim` ones")
                return pinned, json.dumps({text.name: take.verbatim})
            if not take.facts:
                return pinned, "{}"
        return await self._written(person, waiting, offers, take, pinned, leaning, world, clock)

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
    ) -> tuple[Offer, str]:
        if self._replier is None:
            raise RunRefused(f"a model writes what {person.key} does, and no model is configured")
        behaviour = person.reply
        assert isinstance(behaviour, Answers | Scripted)
        history = world.events()
        system = transition_prompt(person, behaviour, take, pinned, leaning, clock.now(), self._scenario.starts_at)
        shown = asked_to_move(waiting, offers)
        context = (
            await self._replier.context(person, behaviour, history[-1], history, world, clock, answers=False)
            if history
            else []
        )
        messages = [ModelMessage(speaker=Speaker.ASKER, text=shown + "\n\n" + (context[0].text if context else ""))]
        said = WrittenTransition(take="")
        for attempt in range(2):
            written = await self._replier.ask_model(
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
                return offer, json.dumps({k: v for k, v in given.items() if v.strip()})
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


class _Unpinnable(Exception):
    """A pinned transition the item does not offer: the person cannot take what is not there."""


def pinned_take(person: Person, provider: ProviderKey, nth: int) -> Take | None:
    """The transition the scenario pins for the nth item pending on `person` in `provider`: one for that item wins
    over one for every item."""
    mine = [t for t in person.takes if t.provider == provider]
    return next((t for t in mine if t.nth == nth), None) or next((t for t in mine if t.nth is None), None)


def _matching(said: str, offers: Sequence[Offer], *, exact: bool = False) -> Offer | None:
    """The offer `said` names: by its name, else (unless `exact`) by the state it reaches, in any case."""
    wanted = said.strip().casefold()
    by_name = next((o for o in offers if o.name.casefold() == wanted), None)
    if by_name is not None or exact:
        return by_name
    return next((o for o in offers if o.to_state.casefold() == wanted), None)


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
