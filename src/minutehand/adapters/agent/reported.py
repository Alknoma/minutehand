"""`Reported`: the agent takes a `WakeRequest` at `wake_url` and answers `AgentReport` at `report_url`.

A wake is settled when the report says anything but WORKING. The report is first asked for after
`Reported.report_first_after`, then after twice that, and so on up to `Reported.report_at_most_every`: a turn
that takes milliseconds is seen at once, and one that takes minutes is asked about a few times a minute rather
than a hundred times a second. A wake still WORKING after `Reported.working_limit` of real time fails the run,
and the failure says which limit it ran into.
"""

from __future__ import annotations

import asyncio

import httpx
from pydantic import ValidationError

from minutehand.application.refusals import AgentFailed
from minutehand.domain.agent import AgentReport, AgentStatus, Reported, WakeRequest


class ReportedDriver:
    def __init__(self, source: Reported) -> None:
        self._source = source

    async def wake(self, request: WakeRequest) -> None:
        url = self._source.wake_url
        async with self._client() as client:
            try:
                response = await client.post(
                    url, content=request.model_dump_json(), headers={"content-type": "application/json"}
                )
            except httpx.TimeoutException as e:
                raise AgentFailed(
                    f"POST {url} did not answer within {self._timeout()}, the agent file's wake_timeout"
                ) from e
            except httpx.HTTPError as e:
                raise AgentFailed(f"POST {url}: {e!r}") from e
        if response.is_error:
            raise AgentFailed(f"POST {url} answered {response.status_code}: {response.text[:500]}")

    async def settled(self) -> AgentReport:
        source = self._source
        loop = asyncio.get_running_loop()
        give_up = loop.time() + source.working_limit.total_seconds()
        wait = source.report_first_after.total_seconds()
        longest = source.report_at_most_every.total_seconds()
        while True:
            await asyncio.sleep(max(min(wait, give_up - loop.time()), 0.0))
            report = await self._report()
            if report.status is not AgentStatus.WORKING:
                return report
            if loop.time() >= give_up:
                raise AgentFailed(
                    f"{source.report_url} still answered WORKING {_span(source.working_limit.total_seconds())} "
                    "after the wake began, the agent file's working_limit for one wake"
                )
            wait = min(wait * 2, longest)

    async def _report(self) -> AgentReport:
        url = self._source.report_url
        async with self._client() as client:
            try:
                response = await client.get(url)
            except httpx.TimeoutException as e:
                raise AgentFailed(
                    f"GET {url} did not answer within {self._timeout()}, the agent file's wake_timeout"
                ) from e
            except httpx.HTTPError as e:
                raise AgentFailed(f"GET {url}: {e!r}") from e
        if response.is_error:
            raise AgentFailed(f"GET {url} answered {response.status_code}: {response.text[:500]}")
        try:
            return AgentReport.model_validate_json(response.text)
        except ValidationError as e:
            raise AgentFailed(f"GET {url} did not answer an AgentReport: {e}") from e

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._source.wake_timeout.total_seconds())

    def _timeout(self) -> str:
        return _span(self._source.wake_timeout.total_seconds())


def _span(seconds: float) -> str:
    return f"{seconds:g} s" if seconds < 120 else f"{seconds / 60:g} min"
