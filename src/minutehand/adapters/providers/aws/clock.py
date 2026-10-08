"""moto's SQS and EventBridge Scheduler, told the time by the run's clock.

moto reads the machine's clock through three functions its models import by name (`unix_time`,
`unix_time_millis`, `utcnow`). Each is replaced, in `moto.sqs.models` and `moto.scheduler.models` only, by one
that reads the clock of the run whose call moto is answering: a `ContextVar` the provider sets around every call it
makes into moto (`on`), which asgiref carries into the thread moto's WSGI app runs in. So a message's
`SentTimestamp`, its visibility timeout and `DelaySeconds`, a queue's `CreatedTimestamp`, and a schedule's
`CreationDate` and `LastModificationDate` are all the run's time, and a message received at a wake becomes visible
again once the run's clock passes its visibility timeout, whatever the machine's clock says.

A long poll (`WaitTimeSeconds`, or the queue's `ReceiveMessageWaitTimeSeconds`) that finds a visible message
answers at once, as SQS's does ("If a message is available, the call returns sooner than WaitTimeSeconds"). One that
finds none would have to wait, and the run's clock does not move inside a call: moto's wait raises instead, and the
provider refuses that poll by name (`provider.LONG_POLL`). Serving it needs the orchestrator to move the run's clock while a
call is held (see `CLAIMS.md`).

moto read outside any call of the provider's is Minutehand's bug, and raises.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

import moto.scheduler.models as scheduler_models
import moto.sqs.models as sqs_models
from moto.core import utils as moto_utils
from moto.sqs.models import Queue

from minutehand.ports.clock import Clock

_CLOCK: ContextVar[Clock | None] = ContextVar("minutehand_aws_clock", default=None)


@contextmanager
def on(clock: Clock) -> Iterator[None]:
    """Every reading of the time moto makes inside this block is `clock`'s."""
    token = _CLOCK.set(clock)
    try:
        yield
    finally:
        _CLOCK.reset(token)


def _now() -> datetime:
    clock = _CLOCK.get()
    if clock is None:
        raise RuntimeError("moto read the time outside a call of the aws provider's, where no run's clock is set")
    return clock.now()


def _seconds() -> float:
    return _now().timestamp()


def _unix_time(dt: datetime | None = None) -> float:
    """moto's `unix_time`: seconds since the epoch, of `dt` or of the run's now."""
    return moto_utils.unix_time(dt) if dt is not None else _seconds()


def _unix_time_millis(dt: datetime | None = None) -> float:
    return moto_utils.unix_time_millis(dt) if dt is not None else _seconds() * 1000.0


def _utcnow() -> datetime:
    """moto's `utcnow`: naive UTC."""
    return _now().astimezone(UTC).replace(tzinfo=None)


class WouldWait(RuntimeError):
    """moto was about to wait for a message on a clock that cannot move inside a call."""


def _waits_for_messages(queue: Queue, timeout: int) -> None:
    raise WouldWait(f"a long poll on {queue.name} found no message, and the run's clock does not move inside a call")


def install() -> None:
    """Point moto's SQS and Scheduler models at the run's clock. Idempotent: the same functions every time."""
    replaced: list[tuple[object, str, object]] = [
        (sqs_models, "unix_time", _unix_time),
        (sqs_models, "unix_time_millis", _unix_time_millis),
        (scheduler_models, "unix_time", _unix_time),
        (scheduler_models, "utcnow", _utcnow),
    ]
    for module, name, reading in replaced:
        setattr(module, name, reading)
    Queue.wait_for_messages = _waits_for_messages  # type: ignore[method-assign]
