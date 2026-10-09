"""Events the agent's own calls set off, sent to the agent as Slack sends them.

A person's act is pushed by `inbound`, which is handed the agent's inbound target and signing secret with each
call. An event the agent itself causes (it creates a channel, invites a member, removes a reaction) happens inside a
Web API call, which is handed neither: the provider is told them once a run or a world starts
(`SlackProvider.listen`), and `Pusher` sends what a call set off after the call has been answered, as Slack does:
the caller is never kept waiting for the agent to take its own event, and an agent whose handler calls back into
Slack cannot be deadlocked by one. A world whose agent declares no Slack target has subscribed to nothing, so
nothing is sent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from minutehand.adapters.providers.slack import inbound, wire
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.application.refusals import AgentFailed
from minutehand.domain.people import InboundTarget

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Listener:
    """Where the agent takes its Slack events, and the secret that signs them."""

    target: InboundTarget
    secret: str


class Pusher:
    def __init__(self, listening: Callable[[], Listener | None]) -> None:
        self._listening = listening
        self._sending: set[asyncio.Task[None]] = set()
        self.refused: list[str] = []
        """The events the agent's own calls set off that it did not take after Slack's retries, in the order sent."""

    def send(self, slack: SlackWorld, callbacks: list[wire.EventCallback]) -> None:
        """Send `callbacks` in order, in the background, to the agent if it listens."""
        listener = self._listening()
        if listener is None or not callbacks:
            return
        task = asyncio.create_task(self._send(slack, listener, callbacks))
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _send(self, slack: SlackWorld, listener: Listener, callbacks: list[wire.EventCallback]) -> None:
        for callback in callbacks:
            try:
                await inbound.push_event(slack, listener.target, callback, listener.secret)
            except AgentFailed as failed:
                logger.error("the agent did not take %s: %s", callback.event_id, failed)
                self.refused.append(f"{callback.event_id}: {failed}")

    def delivering(self) -> int:
        """How many sends have started and not finished."""
        return len(self._sending)

    async def settled(self) -> None:
        while self._sending:
            await asyncio.gather(*list(self._sending))
