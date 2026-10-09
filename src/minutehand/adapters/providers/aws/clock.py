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
finds none would have to wait, and the run's clock does not move inside a call: moto's wait raises `WouldWait`
instead, saying how long the call waits and when a message of the queue next becomes visible, and the provider has
the proxy hold the call (`adapters.proxy.held`). Looked at again once the run's clock has reached the end of its
wait (`waiting(False)`), moto answers it as the queue stands, without waiting.

moto read outside any call of the provider's is Minutehand's bug, and raises.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

import moto.scheduler.models as scheduler_models
import moto.sqs.models as sqs_models
from moto.core import utils as moto_utils
from moto.sqs.models import Message, Queue, SQSBackend

from minutehand.ports.clock import Clock

_CLOCK: ContextVar[Clock | None] = ContextVar("minutehand_aws_clock", default=None)
_WAITS: ContextVar[bool] = ContextVar("minutehand_aws_waits", default=True)


@contextmanager
def on(clock: Clock, *, waits: bool = True) -> Iterator[None]:
    """Every reading of the time moto makes inside this block is `clock`'s. Without `waits`, a long poll that finds
    no message answers at once, its wait being over."""
    token, waiting = _CLOCK.set(clock), _WAITS.set(waits)
    try:
        yield
    finally:
        _WAITS.reset(waiting)
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
    """moto was about to wait for a message on a clock that cannot move inside a call: for `seconds` at most, and a
    message of the queue becomes visible by itself at `again` (a delay or a visibility timeout running out)."""

    def __init__(self, queue: str, seconds: int = 0, again: datetime | None = None) -> None:
        super().__init__(f"a long poll on {queue} found no message, and the run's clock does not move inside a call")
        self.queue = queue
        self.seconds = seconds
        self.again = again


def _waits_for_messages(queue: Queue, timeout: int) -> None:
    raise WouldWait(queue.name)


_RECEIVE = SQSBackend.receive_message


def _receive_message(
    backend: SQSBackend,
    queue_name: str,
    count: int,
    wait_seconds_timeout: int,
    visibility_timeout: int,
    message_attribute_names: list[str] | None = None,
) -> list[Message]:
    """moto's `receive_message`, which waits through `Queue.wait_for_messages`: a wait that is over is none, and one
    that would wait says how long, and when the queue next has a message without anyone sending one."""
    seconds = wait_seconds_timeout if _WAITS.get() else 0
    try:
        return _RECEIVE(backend, queue_name, count, seconds, visibility_timeout, message_attribute_names)
    except WouldWait:
        raise WouldWait(queue_name, seconds, _next_visible(backend.get_queue(queue_name))) from None


def _next_visible(queue: Queue) -> datetime | None:
    """The first moment after now a message of `queue` is visible and not delayed. moto reads a message as visible
    only once now is past its `visible_at`, by a millisecond's resolution, and as delayed until `delayed_until`."""
    now = _seconds() * 1000.0
    moments = [max(m.visible_at + 1.0, m.delayed_until) for m in queue._messages]
    later = [m for m in moments if m > now]
    return datetime.fromtimestamp(0, UTC) + timedelta(milliseconds=min(later)) if later else None


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
    SQSBackend.receive_message = _receive_message  # type: ignore[method-assign]
