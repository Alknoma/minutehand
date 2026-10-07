"""Every external emulator a run, or a standing server, has in use, each started once and stopped together.

`application.orchestrator.Environment`: the run asks after each wake whether one has failed under it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from minutehand.adapters.emulator.process import EmulatorRefused, Running
from minutehand.domain.emulator import EmulatorChange, ExternalEmulator


def log_of(directory: Path, name: str) -> Path:
    """Where what an emulator Minutehand starts prints is kept."""
    return directory / f"emulator-{name}.log"


class Emulators:
    def __init__(
        self,
        directory: Path,
        environment: Mapping[str, str],
        on_change: Callable[[EmulatorChange], None],
        *,
        running: dict[str, Running] | None = None,
    ) -> None:
        """`directory` holds each one's log; `environment` is added to each one's (the OTLP variables);
        `on_change` hears each health change as it happens. `running` is the mapping the proxy's capturing reads
        its routes from (`Capturing.emulators`), filled as each starts."""
        self._directory = directory
        self.environment = dict(environment)
        self.on_change = on_change
        self.running: dict[str, Running] = running if running is not None else {}

    async def start(self, declared: Sequence[ExternalEmulator]) -> None:
        """Each declared emulator not yet running is started (or attached to) and waited on until ready; one
        running already under the same name must be declared the same. Refused with the log's tail, and nothing
        this call started is left running, when one does not come up."""
        started: list[Running] = []
        try:
            for declaration in declared:
                if declaration.name in self.running:
                    if self.running[declaration.name].declaration != declaration:
                        raise EmulatorRefused(
                            f"emulator {declaration.name} is already running as another world declared it; one "
                            "name is one emulator, declared the same by every world that uses it"
                        )
                    continue
                running = Running(
                    declaration,
                    log=log_of(self._directory, declaration.name),
                    environment=self.environment,
                    on_change=lambda change: self.on_change(change),
                )
                await running.start()
                started.append(running)
                self.running[declaration.name] = running
        except EmulatorRefused:
            for running in started:
                del self.running[running.name]
                await running.stop(record=False)
            raise

    @property
    def routes(self) -> Mapping[str, Running]:
        return self.running

    def failure(self) -> str | None:
        """The first emulator in use that has failed, and why; None while all answer."""
        return next((f for r in self.running.values() if (f := r.unavailable()) is not None), None)

    async def stop(self) -> None:
        for running in list(self.running.values()):
            await running.stop()
        self.running.clear()
