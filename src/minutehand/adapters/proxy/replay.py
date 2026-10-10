"""The agent's model calls answered from an earlier run's recording (`minutehand replay`).

A replay plays a run again from its start: the same world, the same people's words, and each of the agent's calls
to its model answered with what the model answered then, so the agent rebuilds whatever it holds in its own process
(a conversation, a plan) exactly as it was. A call is matched by its host, its path and its body as the recording
keeps it (credentials redacted, `model_calls.kept_body`); calls alike are answered in the order they were made.

The first call the recording has no answer for is where the runs part: the agent asked something it did not ask
before, so the recording's later answers describe another run. From that call on, and from `live_from` on when it is
given, every call goes to the model and is kept as a new recording would be.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from minutehand.adapters.proxy.model_calls import kept_body
from minutehand.domain.telemetry import BytesValue, IntValue, SpanSource, StoredSpan, StringValue

JSON = "application/json"
EVENT_STREAM = "text/event-stream"


@dataclass(frozen=True)
class Recorded:
    """One answer the model gave, as the agent received it."""

    status: int
    content_type: str
    body: bytes


def _bytes(value: object) -> bytes | None:
    if isinstance(value, StringValue):
        return value.value.encode("utf-8")
    if isinstance(value, BytesValue):
        return bytes.fromhex(value.value)
    return None


def _key(host: str, path: str, body: object) -> tuple[str, str, str]:
    kept = body.value if isinstance(body, StringValue | BytesValue) else ""
    return host, path.split("?", 1)[0], kept


@dataclass
class ModelReplay:
    """A run's recorded model calls, answered in turn until the run being played parts from it."""

    source: str
    live_from: datetime | None = None
    _answers: dict[tuple[str, str, str], deque[Recorded]] = field(default_factory=dict)
    parted: str | None = None
    """Why the runs parted: the first call with no recorded answer, or the moment live play began; None while
    every call has been answered from the recording."""
    answered: int = 0

    @classmethod
    def of(cls, source: str, spans: Sequence[StoredSpan], *, live_from: datetime | None = None) -> ModelReplay:
        """The model calls `source` recorded on the wire, oldest first."""
        replay = cls(source=source, live_from=live_from)
        for stored in sorted(spans, key=lambda s: (s.span.start, s.after_seq)):
            span = stored.span
            if stored.source is not SpanSource.WIRE:
                continue
            host, path = span.attribute("server.address"), span.attribute("url.path")
            status, said = (
                span.attribute("http.response.status_code"),
                _bytes(span.attribute("minutehand.response.body")),
            )
            if not isinstance(host, StringValue) or not isinstance(path, StringValue) or said is None:
                continue
            kind = span.attribute("minutehand.response.content_type")
            content_type = kind.value if isinstance(kind, StringValue) else _guessed(said)
            key = _key(host.value, path.value, span.attribute("minutehand.request.body"))
            code = status.value if isinstance(status, IntValue) else 200
            replay._answers.setdefault(key, deque()).append(Recorded(code, content_type, said))
        return replay

    def answer(self, host: str, path: str, body: bytes, content_type: str, now: datetime) -> Recorded | None:
        """The recorded answer to this call; None, from now on, once the runs have parted."""
        if self.parted is not None:
            return None
        if self.live_from is not None and now >= self.live_from:
            self.parted = f"live from {self.live_from.isoformat()}"
            return None
        waiting = self._answers.get(_key(host, path, kept_body(body, content_type)))
        if not waiting:
            self.parted = f"{host}{path.split('?', 1)[0]}: a call {self.source} never made, at {now.isoformat()}"
            return None
        self.answered += 1
        return waiting.popleft()


def _guessed(body: bytes) -> str:
    """A recording made before content types were kept: a stream starts with an event's field."""
    return EVENT_STREAM if body.lstrip().startswith((b"data:", b"event:")) else JSON
