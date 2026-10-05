"""A standing world: seeded once, answered for as long as it is open, with no run loop and no agent wakes.

`minutehand serve` holds many of these at once, one per test. Nothing in one happens by itself: its clock
stands still until whoever opened it moves it (`advance`), and only then do the things the world owes fall
due, in order: what people do by themselves (the scenario's happenings, of every family, whether or not
scripted people speak), a scripted person's reply to a message the agent sent them, a ticket's fate, the
owner's directions. With `scripted` off, nobody answers but the test, which speaks for people itself (`say`,
`reply`, `move_ticket`, `edit_ticket`).

A world is read the way a finished run is: its log, its calls, and the same checks over it as of now.
What a scheduler provider booked is recorded in the log and is never fired here: a booking becomes a wake
only in the run loop.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from minutehand.application.model_calls import per_wake
from minutehand.application.refusals import RunRefused, refuse_unheld
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.application.run_clock import RunClock
from minutehand.checks.runner import RunResult, evaluate, view_of
from minutehand.domain.people import InboundTarget, PersonMessage, PersonReply, Press
from minutehand.domain.provider import Manifest
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import (
    Answers,
    DocumentHappening,
    Happening,
    Person,
    ProviderKey,
    Scenario,
    TicketHappening,
    TicketState,
)
from minutehand.domain.world import (
    Actor,
    EntityRef,
    MessageSnapshot,
    Operation,
    TicketSnapshot,
    WorldEvent,
)
from minutehand.ports.provider import (
    ActsOnTickets,
    ASGIApp,
    ChangesDocuments,
    DeclaresFaults,
    DeletesTickets,
    EditsTickets,
    HoldsTickets,
    NotifiesChanges,
    Provider,
    PushesEvents,
    PushesInteractions,
)
from minutehand.ports.store import Store

STANDING_WAKE = 1
"""Everything after the seed happens in this one wake: the seed is wake 0, as in a run."""


class WorldRefused(RunRefused):
    """What was asked of a standing world cannot be done, and nothing was changed."""


@dataclass(frozen=True)
class Fired:
    """One thing the world owed that fell due while the clock moved."""

    at: datetime
    what: str
    events: list[int]


@dataclass
class _Owed:
    at: datetime
    what: str
    reply: PersonReply | None = None
    fate: tuple[EntityRef, TicketState | None] | None = None
    direction: str | None = None
    happening: Happening | None = None


class StandingWorld:
    def __init__(
        self,
        *,
        scenario: Scenario,
        store: Store,
        clock: RunClock,
        provider: Callable[[ProviderKey], Provider],
        inbound: Sequence[InboundTarget],
        signing: Mapping[ProviderKey, str],
        scripted: bool,
    ) -> None:
        """`provider` builds a provider by key; it is seeded into this world the first time it is had.
        `signing` is the secret each inbound target's events are signed with. `scripted` lets the scenario's
        scripted people answer, its tickets meet their fates and its directions be said, as the clock passes
        each; a person whose replies a model writes is refused then, since a standing world has no model."""
        if scripted:
            written = [p.key for p in scenario.people if isinstance(p.reply, Answers)]
            if written:
                raise WorldRefused(
                    f"scripted people answer by script, and the replies of {', '.join(written)} are written by a "
                    "model (reply kind 'answers', the default): give each `reply: {kind: scripted, ...}` or "
                    "`{kind: silent}`, or open the world with scripted people off"
                )
        unsigned = sorted({t.provider for t in inbound} - set(signing))
        if unsigned:
            raise WorldRefused(f"no signing secret for the inbound target on {', '.join(unsigned)}")
        self.scenario = scenario
        self.store = store
        self.clock = clock
        self.scripted = scripted
        self._provider = provider
        self._inbound = {t.provider: t for t in inbound}
        self._signing = dict(signing)
        self._built: dict[ProviderKey, Provider] = {}
        self._apps: dict[ProviderKey, ASGIApp] = {}
        self._people = {p.email: p for p in scenario.people}
        self._replier = ScriptedReplier(scenario) if scripted else None
        self._owed: list[_Owed] = []
        self._fated: set[EntityRef] = set()
        self._seen = 0

    # -- the world as the proxy answers it --------------------------------------------------------------------

    def open(self, named: Sequence[ProviderKey]) -> None:
        """Seed every provider in `named`, then begin the one wake everything after the seed happens in."""
        for key in named:
            self.provider(key)
        for n, happening in enumerate(self.scenario.happenings, start=1):
            self._lands(happening, n)
            at = self.scenario.starts_at + happening.after
            self._owed.append(
                _Owed(at=at, what=f"happening {n}: {happening.person} {_doing(happening)}", happening=happening)
            )
        if self.scripted:
            for direction in self.scenario.directions:
                at = self.scenario.starts_at + direction.after
                self._owed.append(_Owed(at=at, what="the owner's direction", direction=direction.text))
        self.clock.begin_wake()
        self.store.wake_began(STANDING_WAKE)
        self._seen = self.store.head()

    def close(self) -> None:
        self.store.wake_ended(STANDING_WAKE)

    def provider(self, key: ProviderKey) -> Provider:
        """The provider, built and seeded with the scenario the first time this world has it, unless the world
        already holds anything of it."""
        if key not in self._built:
            found = self._provider(key)
            if not any(e.entity.provider == key for e in self.store.events()):
                found.seed(self.scenario, self.store)
            self._built[key] = found
        return self._built[key]

    def app_for(self, manifest: Manifest) -> ASGIApp:
        if manifest.key not in self._apps:
            self._apps[manifest.key] = self.provider(manifest.key).app(self.store, self.clock)
        return self._apps[manifest.key]

    # -- time ---------------------------------------------------------------------------------------------------

    async def advance(self, to: datetime) -> list[Fired]:
        """Move the clock to `to`, firing in order everything the world owes up to it."""
        if to < self.clock.now():
            raise WorldRefused(
                f"the clock only moves forward: {to.isoformat()} is before {self.clock.now().isoformat()}"
            )
        await self.observe()
        fired: list[Fired] = []
        while True:
            due = sorted((o for o in self._owed if o.at <= to), key=lambda o: o.at)
            if not due:
                break
            owed = due[0]
            self._owed.remove(owed)
            self.clock.jump(max(owed.at, self.clock.now()))
            before = self.store.head()
            await self._fire(owed)
            fired.append(
                Fired(at=self.clock.now(), what=owed.what, events=list(range(before + 1, self.store.head() + 1)))
            )
            await self.observe()
        self.clock.jump(to)
        return fired

    def owed(self) -> list[tuple[datetime, str]]:
        """What will fall due as the clock moves, earliest first."""
        return [(o.at, o.what) for o in sorted(self._owed, key=lambda o: o.at)]

    async def observe(self) -> None:
        """Read what the agent did since the last look and schedule what the world owes back: a scripted reply
        to each message it sent a person, the fate of each ticket it handed to a person who has one. A message
        is answered as it read when it was first seen; an edit is not put to the person again."""
        new = self.store.events(since=self._seen)
        if new:
            self._seen = new[-1].seq
        if self._replier is None:
            return
        history: list[WorldEvent] | None = None
        for event in new:
            if event.actor is not Actor.AGENT or event.operation not in (Operation.CREATE, Operation.UPDATE):
                continue
            after = event.after
            if isinstance(after, TicketSnapshot) and after.assignee_email in self._people:
                self._fate(self._people[after.assignee_email], event)
            if event.operation is Operation.CREATE and isinstance(after, MessageSnapshot):
                history = history if history is not None else self.store.events()
                for email in after.recipient_emails:
                    if email in self._people:
                        await self._ask(self._people[email], event, [h for h in history if h.seq <= event.seq])

    async def _ask(self, person: Person, asked: WorldEvent, history: list[WorldEvent]) -> None:
        assert self._replier is not None
        reply = await self._replier.decide(person, asked, history, self.clock)
        if reply is None:
            return
        self.store.remember(reply)
        self._owed.append(_Owed(at=reply.at, what=f"{person.key}'s scripted reply", reply=reply))

    def _fate(self, person: Person, assigned: WorldEvent) -> None:
        fate = next((f for f in self.scenario.ticket_fates if f.assignee == person.key), None)
        if fate is None or assigned.entity in self._fated:
            return
        self._fated.add(assigned.entity)
        self._owed.append(
            _Owed(
                at=assigned.sim_time + fate.after,
                what=f"{person.key} "
                + (
                    f"moves {assigned.entity.external_id} to {fate.becomes.value}"
                    if fate.becomes is not None
                    else f"deletes {assigned.entity.external_id}"
                ),
                fate=(assigned.entity, fate.becomes),
            )
        )

    async def _fire(self, owed: _Owed) -> None:
        if owed.happening is not None:
            await self._happen(owed.happening)
        elif owed.reply is not None:
            provider = owed.reply.in_reply_to.provider
            await self._pushes(provider).deliver(
                owed.reply, self._target(provider), self.store, self.clock, secret=self._signing[provider]
            )
        elif owed.fate is not None:
            ticket, becomes = owed.fate
            if becomes is None:
                self._deletes(ticket.provider).delete_ticket(ticket, self.store, self.clock)
            else:
                self._holds(ticket.provider).transition(ticket, becomes, self.store, self.clock)
        elif owed.direction is not None:
            await self.say(self.scenario.owner, owed.direction, provider=self._only_inbound())

    def _lands(self, happening: Happening, n: int) -> None:
        """Refuse, before the world opens, a happening whose provider lacks the port its family lands through."""
        provider = self.scenario.happening_provider(happening)
        found = self.provider(provider)
        if isinstance(happening, TicketHappening):
            port, ok = "ActsOnTickets", isinstance(found, ActsOnTickets)
        elif isinstance(happening, DocumentHappening):
            port, ok = "ChangesDocuments", isinstance(found, ChangesDocuments)
        else:
            port, ok = "PushesEvents", isinstance(found, PushesEvents) and provider in self._inbound
        if not ok:
            raise WorldRefused(
                f"happening {n} ({happening.person} {_doing(happening)}) lands on {provider}, which has no "
                f"{port}" + ("" if port != "PushesEvents" else " or no inbound target in this world")
            )

    async def _happen(self, happening: Happening) -> None:
        provider = self.scenario.happening_provider(happening)
        found = self.provider(provider)
        if isinstance(happening, TicketHappening):
            assert isinstance(found, ActsOnTickets)
            found.act(happening, self.scenario, self.store, self.clock)
        elif isinstance(happening, DocumentHappening):
            assert isinstance(found, ChangesDocuments)
            found.change(happening, self.scenario, self.store, self.clock)
            if isinstance(found, NotifiesChanges) and found.watched(self.store, self.clock):
                await found.notify(self.store, self.clock)
        else:
            await self._pushes(provider).happen(
                happening, self._target(provider), self.store, self.clock, secret=self._signing[provider]
            )

    # -- a person acting ----------------------------------------------------------------------------------------

    async def say(self, person: str, text: str, *, provider: ProviderKey) -> WorldEvent:
        """`person` messages the agent directly (in Slack, a DM to its bot), pushed to its inbound target."""
        self._person(person)
        before = self.store.head()
        message = PersonMessage(person=person, text=text, at=self.clock.now())
        await self._pushes(provider).say(
            message, self._target(provider), self.store, self.clock, secret=self._signing[provider]
        )
        return self._written(before)

    async def reply(self, person: str, text: str, *, to: EntityRef) -> WorldEvent:
        """`person` answers a message: in its thread in a channel, as a new message in a DM."""
        self._person(person)
        before = self.store.head()
        answer = PersonReply(person=person, in_reply_to=to, text=text, at=self.clock.now())
        self.store.remember(answer)
        await self._pushes(to.provider).deliver(
            answer, self._target(to.provider), self.store, self.clock, secret=self._signing[to.provider]
        )
        return self._written(before)

    async def happen_now(self, happening: Happening) -> WorldEvent:
        """`happening`, of any family, done now rather than at its offset: checked as a scenario's would be (its
        person, its ticket, document, channel or post, the port it lands through), then landed the way `advance`
        lands one."""
        try:
            checked = type(self.scenario).model_validate(
                {**self.scenario.model_dump(), "happenings": [happening.model_dump()]}
            )
            key = checked.happening_provider(happening)
            refuse_unheld(checked, {key: self.provider(key).manifest})
        except (ValueError, RunRefused) as e:
            raise WorldRefused(f"this happening cannot land here: {e}") from e
        self._lands(happening, 1)
        before = self.store.head()
        await self._happen(happening)
        return self._written(before)

    async def press(self, person: str, on: EntityRef, press: Press) -> WorldEvent:
        """`person` uses a control on the message `on` now (a button, a pick, a form filled), pushed to the
        agent's interactivity target the way the provider's service pushes it."""
        self._person(person)
        found = self.provider(on.provider)
        if not isinstance(found, PushesInteractions):
            raise WorldRefused(f"{on.provider} carries no controls a person can use")
        before = self.store.head()
        answer = PersonReply(person=person, in_reply_to=on, text=press.label, at=self.clock.now(), press=press)
        self.store.remember(answer)
        await found.press(answer, self._target(on.provider), self.store, self.clock, secret=self._signing[on.provider])
        return self._written(before)

    def declare_faults(self, provider: ProviderKey, faults: str) -> None:
        """A fragment of `provider`'s own seed model declaring faults, validated and recorded by the provider."""
        found = self.provider(provider)
        if not isinstance(found, DeclaresFaults):
            raise WorldRefused(f"{provider} declares no faults of its own")
        try:
            found.declare(faults, self.store, self.clock)
        except ValueError as e:
            raise WorldRefused(f"{provider} cannot declare these faults: {e}") from e

    def move_ticket(self, ticket: EntityRef, to: TicketState) -> WorldEvent:
        """The ticket's assignee moves it, as actor PERSON."""
        before = self.store.head()
        self._holds(ticket.provider).transition(ticket, to, self.store, self.clock)
        return self._written(before)

    def edit_ticket(self, ticket: EntityRef, *, state: TicketState | None, assignee: str | None) -> WorldEvent:
        """The ticket is rewritten from outside the agent (reassigned, reopened), as actor SCENARIO."""
        email = self._person(assignee).email if assignee is not None else None
        provider = self.provider(ticket.provider)
        if not isinstance(provider, EditsTickets):
            raise WorldRefused(f"{ticket.provider} holds no tickets that can be rewritten")
        before = self.store.head()
        provider.edit(ticket, state=state, assignee_email=email, world=self.store, clock=self.clock)
        return self._written(before)

    # -- reading ------------------------------------------------------------------------------------------------

    async def checks(self, *, stop: StopReason | None) -> RunResult:
        """Every deterministic check and the scorecard over the world as it stands now."""
        await self.observe()
        view = view_of(
            self.scenario,
            self.store.events(),
            [],
            self.store.replies(),
            unmatched_calls=[c.exchange for c in self.store.calls() if c.refused],
            model_calls=per_wake(self.store.spans(), [STANDING_WAKE]),
        )
        return evaluate(view, stop=stop, ended=self.clock.now())

    # -- lookups that refuse loudly -----------------------------------------------------------------------------

    def _written(self, before: int) -> WorldEvent:
        written = [e for e in self.store.events(since=before) if e.operation is not Operation.READ]
        if not written:
            raise WorldRefused("the provider recorded nothing for this act")
        return written[0]

    def _person(self, key: str) -> Person:
        found = next((p for p in self.scenario.people if p.key == key), None)
        if found is None:
            raise WorldRefused(
                f"no person {key} in this world; it has {', '.join(p.key for p in self.scenario.people)}"
            )
        return found

    def _pushes(self, provider: ProviderKey) -> PushesEvents:
        found = self.provider(provider)
        if not isinstance(found, PushesEvents):
            raise WorldRefused(f"{provider} pushes no events to the agent")
        return found

    def _target(self, provider: ProviderKey) -> InboundTarget:
        if provider not in self._inbound:
            raise WorldRefused(f"this world declares no inbound target for {provider}: where would the event go?")
        return self._inbound[provider]

    def _only_inbound(self) -> ProviderKey:
        if len(self._inbound) != 1:
            raise WorldRefused(
                "a direction is said on the world's one inbound target, and it declares " + str(len(self._inbound))
            )
        return next(iter(self._inbound))

    def _deletes(self, provider: ProviderKey) -> DeletesTickets:
        found = self.provider(provider)
        if not isinstance(found, DeletesTickets):
            raise WorldRefused(f"{provider} holds no tickets a person can delete")
        return found

    def _holds(self, provider: ProviderKey) -> HoldsTickets:
        found = self.provider(provider)
        if not isinstance(found, HoldsTickets):
            raise WorldRefused(f"{provider} holds no tickets a person can move")
        return found


def _doing(happening: Happening) -> str:
    """What the happening has its person do, in a few words, for a refusal or the list of what is owed."""
    if isinstance(happening, TicketHappening):
        return f"{happening.action.kind} the seeded ticket {happening.ticket!r}"
    if isinstance(happening, DocumentHappening):
        return f"{happening.action.kind} the seeded document {happening.document!r}"
    return happening.kind
