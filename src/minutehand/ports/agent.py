"""How the orchestrator reaches the agent under test."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from minutehand.domain.agent import AgentReport, WakeRequest
from minutehand.domain.people import PersonReply
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store


class AgentDriver(Protocol):
    async def wake(self, request: WakeRequest) -> None:
        """Tell the agent the time and let it work."""
        ...

    async def settled(self) -> AgentReport:
        """Its report once it is no longer working: whether it is done, and when it next needs to wake if it can
        say. How often it is asked and how long it may keep working are the driver's to know, from the agent
        file; an agent still working past its limit raises `AgentFailed` saying so."""
        ...


@runtime_checkable
class Reports(Protocol):
    """A driver that can ask the agent for its report at any moment, without waking it (`Reported`). One that
    learns the report only as a wake ends (`Command`) or never (`Polled`) is not one: what such an agent's
    state holds after a restore cannot be asked, so it cannot be verified."""

    async def report(self) -> AgentReport:
        """The agent's report now. Raises `AgentFailed` when it does not answer one."""
        ...


class TakesReplies(Protocol):
    """A captured channel people answer through (`Acknowledge.replies`): a person's answer to one of the agent's
    sends is written into the world as their message and delivered to the agent's own inbound endpoint as the
    declaration says. Raises `AgentFailed` when the agent refuses it, as a refused pushed event does."""

    async def deliver(self, reply: PersonReply, world: Store, clock: Clock) -> None: ...
