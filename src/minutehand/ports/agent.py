"""How the orchestrator reaches the agent under test."""

from __future__ import annotations

from typing import Protocol

from minutehand.domain.agent import AgentReport, WakeRequest


class AgentDriver(Protocol):
    async def wake(self, request: WakeRequest) -> None:
        """Tell the agent the time and let it work."""
        ...

    async def report(self) -> AgentReport:
        """Whether it is still working, and when it next needs to wake if it can say."""
        ...
