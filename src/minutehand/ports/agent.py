"""What the run loop sends the agent, through the world: nothing is ever asked of it."""

from __future__ import annotations

from typing import Protocol

from minutehand.domain.people import PersonReply
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store


class TakesReplies(Protocol):
    """A captured channel people answer through (`Acknowledge.replies`): a person's answer to one of the agent's
    sends is written into the world as their message and delivered to the agent's own inbound endpoint as the
    declaration says. Raises `AgentFailed` when the agent refuses it, as a refused pushed event does."""

    async def deliver(self, reply: PersonReply, world: Store, clock: Clock) -> None: ...
