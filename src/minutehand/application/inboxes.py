"""Seeing what waits on people in the agent's own product, and having them decide it (`domain.inboxes`).

`look` reads every declared inbox as every person Minutehand can act as, and compares what it finds with what the
world already holds: an item seen for the first time is written as the agent asking that person
(`InboxItemSnapshot`, `PENDING`, actor AGENT), exactly where a message to them would be; an item the world holds
as pending that is no longer listed, and that the person did not decide, is written as withdrawn by the agent.
What is pending lives in the world's log, never in this object, so a fork or a reopened world reads it back.

Each inbox is a provider of the transitions port (`ports.transitions`): what is pending on a person waits on them,
its decisions are the offers, and the people engine decides it as it decides anything (a take pins the decision; a
model picks one otherwise). `decide` makes the decision as that person, as the product's own page sends it, and
writes it as their change: `DECIDED` when the product took it, still `PENDING` with the product's answer when it
refused. A refusal never stops the run; the wait stays open. `refuse_untakeable` refuses, before anything runs, a
take naming a decision its inbox does not offer.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from minutehand.application.moments import decision_text
from minutehand.application.refusals import RunRefused
from minutehand.domain.inboxes import HttpInbox, ListedItem
from minutehand.domain.people import Decides, PersonReply
from minutehand.domain.scenario import Account, Person, Scenario
from minutehand.domain.transitions import AUTOMATIC_REPLY, TEXT, Offer, OfferField, Transition, Waiting, content_of
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
from minutehand.ports.transitions import record

PENDING = "pending"
"""An item's state while it waits on its person: the product's own word, `ItemStatus.PENDING`."""
DECIDED = "decided"
"""An item's state once its person decided it."""
WITHDRAWN = "withdrawn"
"""An item's state once the agent took it back undecided."""
WITHDRAW = "withdraw"
"""The agent's move that takes an item back: it left the person's list undecided."""
REFUSE = "refuse"
"""The product's move that turns a person's decision down: the item is pending again."""

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


def refuse_untakeable(scenario: Scenario, reaches: Sequence[ReachesInbox]) -> None:
    """Each take pinned on an inbox (`Take.provider` its `name`) names one of its decisions and gives only inputs that
    decision takes; what it leaves out, a model writes."""
    names = {r.declared.name: r.declared for r in reaches}
    for person in scenario.people:
        for take in person.takes:
            if take.provider is None or take.provider not in names:
                continue
            decision = names[take.provider].decision(take.take)
            if decision is None:
                raise RunRefused(
                    f"{person.key} takes {take.take!r} in inbox {take.provider}, which offers no such decision"
                )
            try:
                decision.refuse_unknown_inputs(take.fields, person.key)
            except ValueError as e:
                raise RunRefused(str(e)) from e


def refuse_clashing(reaches: Sequence[ReachesInbox], taken: Sequence[str]) -> None:
    """An inbox's asks are recorded under its name: it may not be a provider's key or a captured host's name."""
    clash = sorted({r.declared.name for r in reaches} & set(taken))
    if clash:
        raise RunRefused(f"inbox {', '.join(clash)} is recorded under a name a provider or captured host already has")


@dataclass
class Looked:
    """What one look at every inbox wrote: the asks seen first now, and each inbox it could not read whole."""

    asked: list[WorldEvent] = field(default_factory=list)
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
                record(
                    world,
                    Transition(
                        provider=name,
                        item=ref,
                        name=WITHDRAW,
                        from_state=PENDING,
                        to_state=WITHDRAWN,
                        by=Actor.AGENT,
                        who=None,
                        content="{}",
                        at=clock.now(),
                    ),
                )
                held[ref] = _snapshot(event)
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

    # -- `ProvidesTransitions`: each item pending on a person is an ask they answer by deciding it --------------

    def items_for(self, person: Person, world: Store) -> list[Waiting]:
        """Every item pending on the person in any inbox, as the product words it."""
        return [
            Waiting(item=ref, state=PENDING, shown=item.summary, conversation=True)
            for ref, item in items_in(world.events()).items()
            if ref.provider in self.reaches and item.person == person.key and item.status is ItemStatus.PENDING
        ]

    def legal(self, item: EntityRef, by: Actor, who: Person | None, world: Store) -> list[Offer]:
        """The decisions the product offers on the item, each with the inputs it takes; and each note the inbox
        declares (`Decision.settles` False), which leaves the item waiting, for a take or an automatic reply."""
        del by, who
        held = items_in(world.events())
        snapshot = held[item] if item in held else None
        if snapshot is None or snapshot.status is not ItemStatus.PENDING:
            return []
        declared = self.declared(item.provider)
        offered = [d for d in (declared.decision(n) for n in snapshot.decisions) if d is not None and d.settles]
        notes = [d for d in declared.decisions if not d.settles]
        return [
            Offer(
                name=d.name,
                to_state=DECIDED if d.settles else PENDING,
                description=d.description,
                fields=[OfferField(name=i.name, required=i.required, description=i.description) for i in d.inputs],
                note=not d.settles,
                unprompted=d.settles,
            )
            for d in [*offered, *notes]
        ]

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        """The person makes the decision as the product's own page would send it (`decide`): their move is recorded
        before the call goes out, so whatever the product does in handling it comes after the decision; when the
        product does not take it, the product's refusal is a move of its own (`refuse`, by the system, back to
        pending, its answer as content)."""
        offers = self.legal(item, by, who, world)
        if offer == AUTOMATIC_REPLY:
            note = next((o for o in offers if o.note and o.fields), None)
            if note is None or who is None:
                raise ValueError(f"inbox {item.provider} takes no note an automatic reply could be")
            words: dict[str, str] = json.loads(content)
            offer, content = note.name, json.dumps({note.fields[0].name: words[TEXT] if TEXT in words else ""})
        found = next((o for o in offers if o.name == offer), None)
        if found is None or who is None:
            raise ValueError(f"item {item.external_id} of inbox {item.provider} offers no decision {offer!r}")
        inputs = content_of(content, found, who.key)
        reply = PersonReply(
            person=who.key,
            in_reply_to=item,
            text=decision_text(offer, inputs),
            at=clock.now(),
            decides=Decides(decision=offer, inputs=inputs),
        )
        moved = record(
            world,
            Transition(
                provider=item.provider,
                item=item,
                name=offer,
                from_state=PENDING,
                to_state=found.to_state,
                by=by,
                who=who.key,
                content=content,
                at=clock.now(),
            ),
        )
        made = await self.decide(reply, world, clock)
        if made is not None and isinstance(made.after, InboxItemSnapshot) and made.after.refused is not None:
            record(
                world,
                Transition(
                    provider=item.provider,
                    item=item,
                    name=REFUSE,
                    from_state=DECIDED,
                    to_state=PENDING,
                    by=Actor.SYSTEM,
                    who=item.provider,
                    content=json.dumps({"answer": made.after.refused}),
                    at=clock.now(),
                ),
            )
        return moved

    def heard_of(self, item: EntityRef, who: Person | None, world: Store, clock: Clock) -> bool:
        """Always: the decision is a call to the agent's own product."""
        del item, who, world, clock
        return True

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
        refused = (
            None
            if answer.accepted
            else f"{answer.status if answer.status is not None else 'no answer'}: {answer.answer}"
        )
        settled = {"status": ItemStatus.DECIDED if answer.accepted else ItemStatus.PENDING}
        if decision.settles:
            settled |= {"decision": decision.name, "said": decision.said, "inputs": dict(reply.decides.inputs)}
        after = item.model_copy(update={**settled, "refused": refused} if decision.settles else {"refused": refused})
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
