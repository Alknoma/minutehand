"""Who decides what a simulated person says, and when."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from minutehand.domain.people import PersonReply, Plan
from minutehand.domain.scenario import Person
from minutehand.domain.world import EntityRef, WorldEvent
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store


class Replier(Protocol):
    def plan(
        self,
        person: Person,
        asked: WorldEvent,
        history: Sequence[WorldEvent],
        owed: Sequence[tuple[EntityRef, datetime]],
    ) -> Plan | None:
        """Whether this person answers `asked` and when; None when they do nothing. Never a model call. `owed` is
        every answer they owe or gave, by the message it answers: a message following one up is not a new ask."""
        ...

    async def write(
        self,
        person: Person,
        asked: WorldEvent,
        plan: Plan,
        history: Sequence[WorldEvent],
        world: Store,
        clock: Clock,
    ) -> PersonReply | None:
        """The reply `plan` lands, its words written now; None when the person found the message needs no answer.
        Returning a reply is what opens an obligation: the agent is now owed an answer at `PersonReply.at`.
        Raises `ports.model.ModelFailed` when the words could not be written; the person still owes them."""
        ...
