"""One external emulator in use: started (or attached to), waited on until ready, watched while the run uses it, and
stopped with the run.

Readiness and health follow Testcontainers' wait strategies: an HTTP path answering a status, a port accepting a
connection, or a line in its log, each within a limit; a start that does not come up is refused with the tail of
its log. Health is polled for as long as it is in use, and the first failure is kept: the emulator is then
unavailable, and Minutehand answers every call to it at once rather than forwarding it. It is never restarted.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import socket
from collections.abc import Callable
from pathlib import Path

import httpx

from minutehand.adapters.emulator.relay import Relay, RelayFailure, Where
from minutehand.application.refusals import RunRefused
from minutehand.domain.emulator import (
    PORT,
    EmulatorChange,
    EmulatorHealth,
    ExternalEmulator,
    ReadyHttp,
    ReadyLog,
    ReadyTcp,
)

TAIL = 1500
"""Characters of an emulator's log kept in a refusal, a health change and a finding."""

STOP_TIMEOUT = 5.0
POLL = 0.05


class EmulatorRefused(RunRefused):
    """An external emulator did not come up: the run or world is refused before the agent starts."""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Running:
    """`adapters.proxy.capture.EmulatorRoute` for one emulator: where the proxy forwards its calls (the relay), and
    whether it is still available."""

    def __init__(
        self,
        declaration: ExternalEmulator,
        *,
        log: Path,
        environment: dict[str, str],
        on_change: Callable[[EmulatorChange], None],
    ) -> None:
        self.declaration = declaration
        self.log = log
        self._environment = environment
        self._on_change = on_change
        self._process: asyncio.subprocess.Process | None = None
        self._watching: asyncio.Task[None] | None = None
        self._failure: str | None = None
        port = str(free_port()) if declaration.command is not None else ""
        self._fill = {PORT: port}
        url = self._filled(declaration.upstream.url)
        self.where = Where.of(declaration.upstream, url)
        self.relay = Relay(declaration.name, self.where, answer_within=declaration.answer_within.total_seconds())

    @property
    def name(self) -> str:
        return self.declaration.name

    @property
    def relay_port(self) -> int:
        return self.relay.port

    @property
    def authority(self) -> str:
        """The upstream's own host and port, for `HostHeader.UPSTREAM`."""
        return self.where.authority

    @property
    def prefix(self) -> str:
        """The path of the upstream's URL, put before every forwarded path."""
        return self.where.prefix

    def _filled(self, text: str) -> str:
        for placeholder, value in self._fill.items():
            text = text.replace(placeholder, value)
        return text

    def unavailable(self) -> str | None:
        """Why the emulator cannot answer, once it has failed; None while it is in use."""
        return self._failure

    def failed(self, reason: str) -> None:
        """It failed: the first reason is kept, and a health change recorded, once."""
        if self._failure is not None:
            return
        self._failure = reason
        exited = self._process is not None and self._process.returncode is not None
        self._on_change(
            EmulatorChange(
                emulator=self.name,
                health=EmulatorHealth.DIED if exited else EmulatorHealth.UNHEALTHY,
                reason=reason,
                log_tail=self.tail(),
            )
        )

    def failure_for(self, peer_port: int) -> RelayFailure | None:
        return self.relay.failure_for(peer_port)

    def tail(self) -> str | None:
        """The end of its log, for one Minutehand started."""
        if self.declaration.command is None:
            return None
        text = self.log.read_text(encoding="utf-8", errors="replace") if self.log.is_file() else ""
        return text.strip()[-TAIL:] or "(it printed nothing)"

    # -- starting -------------------------------------------------------------------------------------------------

    async def start(self) -> None:
        await self.relay.start()
        command = self.declaration.command
        if command is not None:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            env = {
                **os.environ,
                "OTEL_SERVICE_NAME": self.name,
                **self._environment,
                **{k: self._filled(v) for k, v in self.declaration.env.items()},
            }
            with self.log.open("ab") as out:
                try:
                    self._process = await asyncio.create_subprocess_exec(
                        *[self._filled(word) for word in command],
                        env=env,
                        cwd=self.declaration.directory,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=out,
                        stderr=out,
                    )
                except OSError as e:
                    await self.relay.stop()
                    raise EmulatorRefused(
                        f"emulator {self.name}'s command {command[0]} could not be started: {e}"
                    ) from e
        try:
            await self._until_ready()
        except EmulatorRefused:
            await self.stop(record=False)
            raise
        self._on_change(EmulatorChange(emulator=self.name, health=EmulatorHealth.READY))
        self._watching = asyncio.ensure_future(self._watch())

    async def _until_ready(self) -> None:
        loop = asyncio.get_running_loop()
        limit = self.declaration.ready_within.total_seconds()
        give_up = loop.time() + limit
        ready = self.declaration.ready
        last = "it never answered"
        while True:
            process = self._process
            if process is not None and process.returncode is not None:
                raise EmulatorRefused(
                    f"emulator {self.name} exited {process.returncode} before it was ready:\n{self.tail()}"
                )
            if isinstance(ready, ReadyHttp):
                answered, last = await self._answers(ready.path, ready.status, timeout=2.0)
            elif isinstance(ready, ReadyTcp):
                answered, last = await self._accepts()
            else:
                answered, last = self._logged(ready)
            if answered:
                return
            if loop.time() > give_up:
                where = f"at {self.where.describe()}"
                told = f"; its log ends:\n{self.tail()}" if self.declaration.command is not None else ""
                raise EmulatorRefused(f"emulator {self.name} was not ready {where} within {limit:g} s ({last}){told}")
            await asyncio.sleep(POLL)

    async def _answers(self, path: str, status: int, *, timeout: float) -> tuple[bool, str]:
        url = f"http://127.0.0.1:{self.relay.port}{self.prefix}{path}"
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
                got = await client.get(url, headers={"host": self.authority})
        except httpx.HTTPError as e:
            return False, f"GET {path}: {type(e).__name__}"
        if got.status_code != status:
            return False, f"GET {path} answered {got.status_code}, not {status}"
        return True, ""

    async def _accepts(self) -> tuple[bool, str]:
        try:
            _, writer = await asyncio.wait_for(self.where.open(), 2.0)
        except (OSError, TimeoutError) as e:
            return False, f"no connection: {e}"
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True, ""

    def _logged(self, ready: ReadyLog) -> tuple[bool, str]:
        text = self.log.read_text(encoding="utf-8", errors="replace") if self.log.is_file() else ""
        found = len(re.findall(ready.line, text, flags=re.MULTILINE))
        return found >= ready.times, f"its log matched {ready.line!r} {found} of {ready.times} times"

    # -- watching -------------------------------------------------------------------------------------------------

    async def _watch(self) -> None:
        health = self.declaration.health
        failures = 0
        while self._failure is None:
            await asyncio.sleep(health.every.total_seconds())
            process = self._process
            if process is not None and process.returncode is not None:
                self.failed(f"emulator {self.name} exited {process.returncode}")
                return
            if health.path is not None:
                ok, why = await self._answers(health.path, health.status, timeout=health.answer_within.total_seconds())
            else:
                ok, why = await self._accepts()
            failures = 0 if ok else failures + 1
            if failures >= health.fails:
                self.failed(f"emulator {self.name} failed its health check {failures} times in a row: {why}")
                return

    # -- stopping -------------------------------------------------------------------------------------------------

    async def stop(self, *, record: bool = True) -> None:
        watching, self._watching = self._watching, None
        if watching is not None:
            watching.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watching
        await self.relay.stop()
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
            except TimeoutError:
                process.kill()
                await process.wait()
        if record:
            self._on_change(EmulatorChange(emulator=self.name, health=EmulatorHealth.STOPPED))
