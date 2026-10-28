"""The one clock in a run."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Simulated time. Every timestamp a provider writes comes from here."""
        ...

    def wake(self) -> int:
        """The agent wake in progress; 0 while the scenario is being set up."""
        ...

    async def agent_now(self) -> datetime:
        """The agent's own clock at this instant: what a request the agent checks against its clock is stamped with
        as it is sent (Slack's `X-Slack-Request-Timestamp`, a Bot Framework token's `nbf`). For an agent in a
        sandbox it is the sandbox's clock, read now, which runs on in real time while the agent starts and works and
        so may stand ahead of `now()`; with no sandbox it is `now()`. Never world time: what the world writes is
        stamped by `now()`."""
        ...
