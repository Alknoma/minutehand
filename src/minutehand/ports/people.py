"""Who decides what a simulated person says."""

from __future__ import annotations

from typing import Protocol

from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Person
from minutehand.domain.world import WorldEvent
from minutehand.ports.clock import Clock


class Replier(Protocol):
    async def decide(
        self, person: Person, asked: WorldEvent, history: list[WorldEvent], clock: Clock
    ) -> PersonReply | None:
        """The reply this person gives to one message, or None when it needs no answer or they stay silent.

        Returning a reply is what opens an obligation: the agent is now owed an answer at `PersonReply.at`.
        """
        ...
