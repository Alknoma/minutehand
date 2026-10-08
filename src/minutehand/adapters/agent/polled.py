"""`Polled` and `Marked`: the agent takes a `WakeRequest` at `wake_url` and says nothing about itself.

Its wake is over when the call returns. It never reports DONE, because it has no channel to: the run ends at its
deadline or its wake limit. A `Polled` agent is woken on a fixed rhythm; a `Marked` one at the next wake it marked
with `minutehand.agent.wake`, which the run loop reads from the log.
"""

from __future__ import annotations

import httpx

from minutehand.application.refusals import AgentFailed
from minutehand.domain.agent import AgentReport, AgentStatus, WakeRequest


class PolledDriver:
    def __init__(self, wake_url: str, *, timeout: float = 300.0) -> None:
        self._wake_url = wake_url
        self._timeout = timeout
        self._answered = False

    async def wake(self, request: WakeRequest) -> None:
        self._answered = False
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(
                    self._wake_url, content=request.model_dump_json(), headers={"content-type": "application/json"}
                )
            except httpx.HTTPError as e:
                raise AgentFailed(f"POST {self._wake_url}: {e!r}") from e
        if response.is_error:
            raise AgentFailed(f"POST {self._wake_url} answered {response.status_code}: {response.text[:500]}")
        self._answered = True

    async def settled(self) -> AgentReport:
        if not self._answered:
            raise AgentFailed(f"{self._wake_url} was asked for a report before it answered a wake")
        return AgentReport(status=AgentStatus.IDLE)
