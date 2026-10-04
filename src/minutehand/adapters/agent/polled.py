"""`Polled`: the agent takes a `WakeRequest` at `wake_url` on a fixed rhythm and says nothing about itself.

Its tick is over when the call returns. It never names a next wake and never reports DONE, because it has
no channel to: the run ends at its deadline or its wake limit.
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

    async def report(self) -> AgentReport:
        if not self._answered:
            raise AgentFailed(f"{self._wake_url} was asked for a report before it answered a wake")
        return AgentReport(status=AgentStatus.IDLE)
