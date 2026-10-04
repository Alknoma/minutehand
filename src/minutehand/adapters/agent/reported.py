"""`Reported`: the agent takes a `WakeRequest` at `wake_url` and answers `AgentReport` at `report_url`."""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from minutehand.application.refusals import AgentFailed
from minutehand.domain.agent import AgentReport, WakeRequest


class ReportedDriver:
    def __init__(self, wake_url: str, report_url: str, *, timeout: float = 30.0) -> None:
        self._wake_url = wake_url
        self._report_url = report_url
        self._timeout = timeout

    async def wake(self, request: WakeRequest) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(
                    self._wake_url, content=request.model_dump_json(), headers={"content-type": "application/json"}
                )
            except httpx.HTTPError as e:
                raise AgentFailed(f"POST {self._wake_url}: {e!r}") from e
        if response.is_error:
            raise AgentFailed(f"POST {self._wake_url} answered {response.status_code}: {response.text[:500]}")

    async def report(self) -> AgentReport:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.get(self._report_url)
            except httpx.HTTPError as e:
                raise AgentFailed(f"GET {self._report_url}: {e!r}") from e
        if response.is_error:
            raise AgentFailed(f"GET {self._report_url} answered {response.status_code}: {response.text[:500]}")
        try:
            return AgentReport.model_validate_json(response.text)
        except ValidationError as e:
            raise AgentFailed(f"GET {self._report_url} did not answer an AgentReport: {e}") from e
