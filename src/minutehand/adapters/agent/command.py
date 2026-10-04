"""`Command`: one process per wake, `WakeRequest` JSON on stdin, `AgentReport` JSON on stdout."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping

from pydantic import ValidationError

from minutehand.application.refusals import AgentFailed
from minutehand.domain.agent import AgentReport, WakeRequest


class CommandDriver:
    def __init__(self, argv: list[str], *, env: Mapping[str, str] | None = None, timeout: float = 600.0) -> None:
        if not argv:
            raise ValueError("a command needs at least the program to run")
        self._argv = argv
        self._env = {**os.environ, **(env or {})}
        self._timeout = timeout
        self._last: AgentReport | None = None

    async def wake(self, request: WakeRequest) -> None:
        self._last = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self._argv,
                env=self._env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise AgentFailed(f"{self._argv[0]} could not be started: {e}") from e
        try:
            out, err = await asyncio.wait_for(process.communicate(request.model_dump_json().encode()), self._timeout)
        except TimeoutError as e:
            process.kill()
            await process.wait()
            raise AgentFailed(f"{self._argv[0]} did not finish within {self._timeout}s") from e
        if process.returncode != 0:
            raise AgentFailed(
                f"{self._argv[0]} exited {process.returncode}: {err.decode(errors='replace').strip()[-2000:]}"
            )
        try:
            self._last = AgentReport.model_validate_json(out)
        except ValidationError as e:
            raise AgentFailed(f"{self._argv[0]} did not print an AgentReport: {e}") from e

    async def report(self) -> AgentReport:
        if self._last is None:
            raise AgentFailed(f"{self._argv[0]} was asked for a report before a wake finished")
        return self._last
