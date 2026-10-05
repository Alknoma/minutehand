"""How Minutehand reaches one inbox in the agent's own product as a person (`domain.inboxes`)."""

from __future__ import annotations

from typing import Protocol

from minutehand.domain.inboxes import DecideAnswer, HttpInbox, Listed
from minutehand.domain.people import Decides
from minutehand.domain.scenario import Person
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store


class ReachesInbox(Protocol):
    """One declared inbox. Every call it makes is recorded in `world` as Minutehand's own, as the person it acted as
    (`Exchange.inbox_call`), with no credential kept; none is ever the agent's."""

    declared: HttpInbox

    def can_act_as(self, person: Person) -> bool:
        """Whether Minutehand can sign in as this person: always, unless signing in needs a credential they lack."""
        ...

    async def pending(self, person: Person, world: Store, clock: Clock) -> Listed:
        """What waits on `person` now, every page of it. A product that does not answer a list says so in
        `Listed.read`, and never raises."""
        ...

    async def decide(self, person: Person, item: str, decides: Decides, world: Store, clock: Clock) -> DecideAnswer:
        """Make `person`'s decision on the item with the product's id `item`. A product that refuses it, or does not
        answer, says so in `DecideAnswer.accepted`, and never raises."""
        ...
