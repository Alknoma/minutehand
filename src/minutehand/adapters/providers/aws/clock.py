"""moto's SQS and EventBridge Scheduler, told the time by the run's clock.

moto reads the machine's clock through three functions its models import by name (`unix_time`,
`unix_time_millis`, `utcnow`). Each is replaced, in `moto.sqs.models` and `moto.scheduler.models` only, by one
that reads the clock of the run whose call moto is answering: a `ContextVar` the provider sets around every call it
makes into moto (`on`), which asgiref carries into the thread moto's WSGI app runs in. So a message's
`SentTimestamp`, its visibility timeout and `DelaySeconds`, a queue's `CreatedTimestamp`, and a schedule's
`CreationDate` and `LastModificationDate` are all the run's time, and a message received at a wake becomes visible
again once the run's clock passes its visibility timeout, whatever the machine's clock says.

A long poll (`WaitTimeSeconds`) is the one thing in moto that waits for its own clock to move: it polls until
`unix_time()` passes the end of the wait, waiting on the queue for up to a second each time. The run's clock does
not move inside a call, so each of those waits counts its timeout towards that call's own reading of `unix_time`
(and nothing else): the poll still waits on the queue in real time, as long as `WaitTimeSeconds` says, and returns
the moment a message arrives, as SQS's does, and the run's clock is untouched.

moto read outside any call of the provider's is Minutehand's bug, and raises.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

import moto.scheduler.models as scheduler_models
import moto.sqs.models as sqs_models
from moto.core import utils as moto_utils
from moto.sqs.models import Queue

from minutehand.ports.clock import Clock

_CLOCK: ContextVar[Clock | None] = ContextVar("minutehand_aws_clock", default=None)
_WAITED: ContextVar[list[float] | None] = ContextVar("minutehand_aws_waited", default=None)


@contextmanager
def on(clock: Clock) -> Iterator[None]:
    """Every reading of the time moto makes inside this block is `clock`'s."""
    clock_token = _CLOCK.set(clock)
    waited_token = _WAITED.set([0.0])
    try:
        yield
    finally:
        _WAITED.reset(waited_token)
        _CLOCK.reset(clock_token)


def _now() -> datetime:
    clock = _CLOCK.get()
    if clock is None:
        raise RuntimeError("moto read the time outside a call of the aws provider's, where no run's clock is set")
    return clock.now()


def _seconds() -> float:
    return _now().timestamp()


def _unix_time(dt: datetime | None = None) -> float:
    """moto's `unix_time`: seconds since the epoch, of `dt` or of the run's now (plus what a long poll waited)."""
    if dt is not None:
        return moto_utils.unix_time(dt)
    waited = _WAITED.get()
    return _seconds() + (waited[0] if waited is not None else 0.0)


def _unix_time_millis(dt: datetime | None = None) -> float:
    return moto_utils.unix_time_millis(dt) if dt is not None else _seconds() * 1000.0


def _utcnow() -> datetime:
    """moto's `utcnow`: naive UTC."""
    return _now().astimezone(UTC).replace(tzinfo=None)


_wait_for_messages: Callable[[Queue, int], None] = Queue.wait_for_messages


def _waited_for_messages(queue: Queue, timeout: int) -> None:
    _wait_for_messages(queue, timeout)
    waited = _WAITED.get()
    if waited is not None:
        waited[0] += timeout


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
    Queue.wait_for_messages = _waited_for_messages  # type: ignore[method-assign]
