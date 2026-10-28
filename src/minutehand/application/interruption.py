"""A run stopped from outside: a signal to `minutehand run`, `fork` or `replay` (SIGTERM from `docker stop`, SIGINT
from a terminal) ends the run as a run, kept and loadable, rather than killing the process under it.

The signal is taken where the run loop is (`Orchestrator.run`, `resume`): the wake in progress is abandoned at the
await it was in, and the run ends `StopReason.INTERRUPTED`, its checks run over what happened and its record and
result kept like any other run's. Abandoning keeps the record consistent: the last checkpoint was written whole before
the wake began, and the abandoned wake is a later one, so whatever it half did lies after that checkpoint's seq,
where a fork from it does not reach, and no write of the agent's in the checkpoint's own wake follows it
(`rewind.not_restorable`). Taking a checkpoint is never abandoned: a signal that arrives while one is written, and its
agent state saved (`application.agent_state`), is held until both are done, so the newest checkpoint is whole.

Before the run loop stands (the proxy starting, a fork's agent being brought back) a signal is held until it does,
and the loop then stops at once; a fork whose agent is still being brought back is refused, leaving no run behind. A
second signal is not held anywhere: it stops whatever is running, and the run may then be left without a record,
for `minutehand close` to keep.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager


class Interruption:
    """Whether a signal asked the run to stop, and the task to stop when one does."""

    def __init__(self, main: asyncio.Task[object] | None = None) -> None:
        self.received: str | None = None
        """The signal that asked the run to stop, by name (`SIGTERM`); None while none has."""
        self._main = main
        self._task: asyncio.Task[object] | None = None
        self._held = 0

    def receive(self, signal_name: str) -> None:
        """A signal arrived: the task in a `stopping` region is cancelled now, or once it leaves a `held` one. A
        second signal cancels at once, held or not, and with no run loop standing cancels the process's main task."""
        again = self.received is not None
        self.received = signal_name
        target = self._task if self._task is not None else self._main if again else None
        if target is not None and (again or not self._held):
            target.cancel(f"stopped by {signal_name}")

    def taken(self) -> bool:
        """Whether a cancellation the run loop meets is this interruption's (a signal arrived), not someone else's."""
        return self.received is not None

    @contextmanager
    def stopping(self) -> Iterator[None]:
        """The region a signal stops: the run loop, or a fork's agent being brought back. A signal that arrived
        before it began cancels it at its first await."""
        task = asyncio.current_task()
        self._task = task
        try:
            if self.received is not None and task is not None:
                task.cancel(f"stopped by {self.received}")
            yield
        finally:
            self._task = None

    @contextmanager
    def held(self) -> Iterator[None]:
        """A region a first signal waits for: a checkpoint being taken, its agent state saved with it. One that
        arrived meanwhile cancels the run loop once the region is left whole; a region left by an exception is
        already unwinding, and cancelling then would cancel what handles it."""
        self._held += 1
        try:
            yield
        finally:
            self._held -= 1
        if not self._held and self.received is not None and self._task is not None:
            self._task.cancel(f"stopped by {self.received}")

    def settled(self) -> None:
        """The run loop has taken the interruption: the cancellation it met is spent, so what it does to end the run
        (its checks, its record) is not cancelled by it again."""
        task = asyncio.current_task()
        while task is not None and task.uncancel() > 0:
            continue
