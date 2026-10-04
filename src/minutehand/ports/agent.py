"""How the orchestrator reaches the agent under test."""

from __future__ import annotations

from typing import Protocol

from minutehand.domain.agent import AgentReport, WakeRequest


class AgentDriver(Protocol):
    async def wake(self, request: WakeRequest) -> None:
        """Tell the agent the time and let it work."""
        ...

    async def settled(self) -> AgentReport:
        """Its report once it is no longer working: whether it is done, and when it next needs to wake if it can
        say. How often it is asked and how long it may keep working are the driver's to know, from the agent
        file; an agent still working past its limit raises `AgentFailed` saying so."""
        ...
