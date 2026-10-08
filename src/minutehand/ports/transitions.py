"""The one port people act through (`docs/design-transitions.md`): what waits on a person in a provider, what anyone
may do to an item now, and the move taken through the provider's own code path.

The people engine (`application.people`) asks every provider the scenario names in `transitions_on` what waits on
each person, books a moment for each new item, and at that moment asks for the legal offers, picks one (a pin, or a
model's pick from what the person can see) and applies it. The provider does the rest exactly as its service would.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from minutehand.domain.common import Window
from minutehand.domain.scenario import Person
from minutehand.domain.transitions import Offer, Transition, Waiting
from minutehand.domain.world import Actor, EntityRef
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store


@runtime_checkable
class ProvidesTransitions(Protocol):
    def items_for(self, person: Person, world: Store) -> list[Waiting]:
        """What waits on this person in this provider now (assigned, invited, addressed, requested), each with its
        state and what the person sees of it. Nothing that does not wait on them: an item they have finished, or
        one taken from them."""
        ...

    def legal(self, item: EntityRef, by: Actor, who: Person | None, world: Store) -> list[Offer]:
        """The transitions this actor (`who`, when a person) may take on the item now, by the provider's own
        semantics (Jira's workflow, an attendee's response states), each saying what it takes. Empty when there is
        none. An item the world does not hold raises `LookupError`."""
        ...

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        """Take `offer` (by its name) on the item through the provider's own code path, with the same validation,
        history and side effects (pushes, notified subscriptions) as when the service does it, recording the
        transition once (`domain.transitions.transition_change`). `content` is a JSON object of the offer's
        fields. An offer no longer legal raises `ValueError`, as the service refuses it."""
        ...

    def heard_of(self, item: EntityRef, world: Store, clock: Clock) -> bool:
        """Whether the service tells the agent when a person moves this item, by a push the agent asked for (a live
        watch on the calendar it is on): that push is a wake, as a pushed reply's is. False: the agent finds the move
        on its next read."""
        ...


@runtime_checkable
class SteersPeople(Protocol):
    """A provider that says more of how its people act than the scenario's people do (a declared service's
    `within`, `bias` and `odds`)."""

    def within(self, item: EntityRef, world: Store) -> Window | None:
        """When a person acts on the item: this much of their available time after it began to wait on them; None:
        as their own answers are drawn."""
        ...

    def leaning(self, item: EntityRef, world: Store) -> str | None:
        """How people tend to act here, in plain words, shown to the model that picks for them; None: nothing."""
        ...

    def drawn(self, item: EntityRef, offers: Sequence[Offer], world: Store) -> Offer | None:
        """The offer drawn for the item from the provider's own odds, seeded: the model then writes only what it
        carries; None: the model picks."""
        ...
