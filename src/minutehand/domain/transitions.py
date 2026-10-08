"""Transitions: everything a person, the agent, a system or time does to the world, as one move of one item's state
(`docs/design-transitions.md`).

A provider that implements `ports.transitions.ProvidesTransitions` says what waits on a person (`Waiting`), what
anyone may do to an item now (`Offer`), and takes a move through its own code path, recording it once in the run's
log as an event of `EntityKind.TRANSITION` beside its own entity versions (`transition_change`). Minutehand states
it; the team's rules judge it (`count: {transitions: ...}`, `each: transition`).
"""

from __future__ import annotations

from pydantic import AwareDatetime, Field

from minutehand.domain.scenario import Model, ProviderKey
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, TransitionSnapshot, WorldEvent


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
