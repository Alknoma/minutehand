"""Seeing what waits on people in the agent's own product, and having them decide it (`domain.inboxes`).

`look` reads every declared inbox as every person Minutehand can act as, and compares what it finds with what the
world already holds: an item seen for the first time is written as the agent asking that person
(`InboxItemSnapshot`, `PENDING`, actor AGENT), exactly where a message to them would be; an item the world holds
as pending that is no longer listed, and that the person did not decide, is written as withdrawn by the agent.
What is pending lives in the world's log, never in this object, so a fork or a reopened world reads it back.

`decide` makes a decision a person's replier decided (`PersonReply.decides`) as that person, when it falls due,
and writes it as their change: `DECIDED` when the product took it, still `PENDING` with the product's answer when
it refused. A refusal never stops the run; the wait stays open.

There is no default decision. `refuse_undecided` refuses, before anything runs, a person Minutehand can act as in
an inbox whose script says nothing of deciding (`Scripted.decisions` None), naming them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from minutehand.application.refusals import RunRefused
from minutehand.domain.inboxes import HttpInbox, ListedItem
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Account, AfterScript, Answers, Person, Scenario, Scripted, Silent
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    InboxItemSnapshot,
    ItemStatus,
    Operation,
    WorldEvent,
)
from minutehand.ports.clock import Clock
from minutehand.ports.inboxes import ReachesInbox
from minutehand.ports.store import Store

READABLE = frozenset({Account.MEMBER, Account.GUEST})
"""Accounts that can sign in to the agent's product: a bot or a deactivated account never decides anything."""


def item_ref(inbox: str, item_id: str) -> EntityRef:
    return EntityRef(provider=inbox, kind=EntityKind.INBOX_ITEM, external_id=item_id)


def items_in(events: Sequence[WorldEvent]) -> dict[EntityRef, InboxItemSnapshot]:
    """Every item the events hold, as it last read."""
    found: dict[EntityRef, InboxItemSnapshot] = {}
    for event in events:
        if isinstance(event.after, InboxItemSnapshot):
            found[event.entity] = event.after
    return found


def readers(scenario: Scenario, reach: ReachesInbox) -> list[Person]:
    """The people Minutehand can act as in one inbox, in scenario order."""
    return [p for p in scenario.people if p.account in READABLE and reach.can_act_as(p)]


def refuse_undecided(scenario: Scenario, reaches: Sequence[ReachesInbox]) -> None:
    """Every person who can receive items says what they do with them: a script of decisions, silence, or a model
    (`Answers`, or a script that goes on conversing once it is used, `then: answers`). A script that says nothing of
    items and nothing more once it is used is refused: there is no default decision. Each decision a script names
    exists in an inbox it can apply to, and gives no input the decision does not take; what it leaves out, a model
    writes."""
    for reach in reaches:
        declared = reach.declared
        for person in readers(scenario, reach):
            behaviour = person.reply
            if isinstance(behaviour, Silent | Answers):
                continue
            assert isinstance(behaviour, Scripted)
            if behaviour.decisions is None and behaviour.then is AfterScript.SILENT:
                raise RunRefused(
                    f"{person.key} can receive items in inbox {declared.name} and their script says nothing of "
                    "deciding them and `then: silent`; there is no default decision: give them `decisions` (`[]` "
                    "leaves every item pending), `reply: {kind: silent}`, or `then: answers` for a model to decide"
                )
    names = {r.declared.name: r.declared for r in reaches}
    for person in scenario.people:
        behaviour = person.reply
        if not isinstance(behaviour, Scripted):
            continue
        for scripted in behaviour.decisions or []:
            if scripted.inbox is not None and scripted.inbox not in names:
                raise RunRefused(f"{person.key} decides in inbox {scripted.inbox}, which the agent does not declare")
            where = [names[scripted.inbox]] if scripted.inbox is not None else list(names.values())
            found = [d for d in (i.decision(scripted.decision) for i in where) if d is not None]
            if not found:
                raise RunRefused(
                    f"{person.key} decides {scripted.decision!r}, which no inbox "
                    + (f"named {scripted.inbox} " if scripted.inbox else "the agent declares ")
                    + "offers"
                )
            try:
                for decision in found:
                    decision.refuse_unknown_inputs(scripted.inputs, person.key)
            except ValueError as e:
                raise RunRefused(str(e)) from e


def refuse_clashing(reaches: Sequence[ReachesInbox], taken: Sequence[str]) -> None:
    """An inbox's asks are recorded under its name: it may not be a provider's key or a captured host's name."""
    clash = sorted({r.declared.name for r in reaches} & set(taken))
    if clash:
        raise RunRefused(f"inbox {', '.join(clash)} is recorded under a name a provider or captured host already has")


@dataclass
class Looked:
    """What one look at every inbox wrote: the asks seen first now, and the items gone undecided."""

    asked: list[WorldEvent] = field(default_factory=list)
    withdrawn: list[EntityRef] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)


class Inboxes:
    """Every inbox the agent declares, as the people of one scenario."""

    def __init__(self, scenario: Scenario, reaches: Sequence[ReachesInbox]) -> None:
        self.scenario = scenario
        self.reaches = {r.declared.name: r for r in reaches}

    @property
    def names(self) -> list[str]:
        return list(self.reaches)

    def declared(self, inbox: str) -> HttpInbox:
        return self.reaches[inbox].declared

    def holds(self, entity: EntityRef) -> bool:
        return entity.kind is EntityKind.INBOX_ITEM and entity.provider in self.reaches

    async def look(self, world: Store, clock: Clock) -> Looked:
        looked = Looked()
        held = items_in(world.events())
        by_email = {p.email.casefold(): p for p in self.scenario.people}
        by_key = {p.key: p for p in self.scenario.people}
        for name, reach in self.reaches.items():
            per_person = reach.declared.pending.waits_on is None
            seen: set[str] = set()
            complete = True
            for person in readers(self.scenario, reach):
                listed = await reach.pending(person, world, clock)
                if not listed.read:
                    complete = False
                    looked.unread.append(f"{name} as {person.key}: {listed.problem}")
                    continue
                for item in listed.items:
                    seen.add(item.item_id)
                    if not per_person:
                        named = by_email.get((item.waits_on or "").casefold()) or by_key.get(item.waits_on or "")
                        if named is None or named.key != person.key:
                            continue  # someone else's, asked when their list is read; or nobody's here
                    ref = item_ref(name, item.item_id)
                    if ref in held:
                        continue
                    event = world.apply(self._asked(ref, item, person, reach))
                    held[ref] = _snapshot(event)
                    looked.asked.append(event)
            if not complete:
                continue  # an inbox that could not be read whole says nothing about what left it
            for ref, item in list(held.items()):
                if ref.provider != name or item.status is not ItemStatus.PENDING or ref.external_id in seen:
                    continue
                if item.person is None or item.person not in by_key:
                    continue
                if not reach.can_act_as(by_key[item.person]):
                    continue
                event = world.apply(
                    Change(
                        entity=ref,
                        operation=Operation.UPDATE,
                        actor=Actor.AGENT,
                        body=item.model_copy(update={"status": ItemStatus.WITHDRAWN}).model_dump_json(),
                        parent=item.person,
                        after=item.model_copy(update={"status": ItemStatus.WITHDRAWN}),
                    )
                )
                held[ref] = _snapshot(event)
                looked.withdrawn.append(ref)
        return looked

    def _asked(self, ref: EntityRef, item: ListedItem, person: Person, reach: ReachesInbox) -> Change:
        declared = reach.declared
        offered = [d.name for d in declared.decisions]
        allowed = offered if item.decisions is None else [n for n in offered if n in item.decisions]
        snapshot = InboxItemSnapshot(
            inbox=declared.name,
            item_id=item.item_id,
            person=person.key,
            waits_on=item.waits_on or person.email,
            summary=item.summary,
            category=item.category,
            decisions=allowed,
            gates=item.gates,
            status=ItemStatus.PENDING,
        )
        return Change(
            entity=ref,
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body=snapshot.model_dump_json(),
            parent=person.key,
            after=snapshot,
        )

    async def decide(self, reply: PersonReply, world: Store, clock: Clock) -> WorldEvent | None:
        """Make `reply`'s decision as its person, now. None when the item is no longer pending (withdrawn, or decided
        already): there is nothing left to decide, and nothing is called."""
        assert reply.decides is not None
        ref = reply.in_reply_to
        held = items_in(world.events())
        item = held[ref] if ref in held else None
        if item is None or item.status is not ItemStatus.PENDING:
            return None
        person = next(p for p in self.scenario.people if p.key == reply.person)
        reach = self.reaches[ref.provider]
        decision = reach.declared.decision(reply.decides.decision)
        if decision is None:
            raise RunRefused(
                f"{person.key} decides {reply.decides.decision!r}, which inbox {ref.provider} does not offer"
            )
        answer = await reach.decide(person, ref.external_id, reply.decides, world, clock)
        after = item.model_copy(
            update={
                "status": ItemStatus.DECIDED if answer.accepted else ItemStatus.PENDING,
                "decision": decision.name,
                "said": decision.said,
                "permits": decision.permits,
                "inputs": dict(reply.decides.inputs),
                "refused": None
                if answer.accepted
                else f"{answer.status if answer.status is not None else 'no answer'}: {answer.answer}",
            }
        )
        return world.apply(
            Change(
                entity=ref,
                operation=Operation.UPDATE,
                actor=Actor.PERSON,
                body=after.model_dump_json(),
                parent=item.person,
                after=after,
            )
        )


def _snapshot(event: WorldEvent) -> InboxItemSnapshot:
    assert isinstance(event.after, InboxItemSnapshot)
    return event.after
