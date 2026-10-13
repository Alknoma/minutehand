"""The agent's model calls answered from an earlier run's recording (`minutehand replay`, and a fork after its
checkpoint).

A replay plays a run again: the same world, the same people's words, and each of the agent's calls to its model
answered with what the model answered then, so the agent rebuilds whatever it holds in its own process (a
conversation, a plan) exactly as it was. A call is matched by its host, its path and its body as the recording
keeps it (credentials redacted, `model_calls.kept_body`); calls alike are answered in the order they were made.

The first call the recording has no answer for is where the runs part: the agent asked something it did not ask
before, so the recording's later answers describe another run. From that call on, and from `live_from` on when it is
given, every call goes to the model and is kept as a new recording would be. A `frozen` replay goes nowhere instead:
that call, and every one after it, is refused to the agent (`Parted`, a 422 naming the run and the call), nothing is
sent to any model, and the run loop stops the run `REPLAY_PARTED` with what was sent beside what the record held
next (`parted_at`), so a change to the agent's code is told by the first call it changed.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from minutehand.adapters.proxy.model_calls import kept_body
from minutehand.domain.run import PartedCalls, ReplayedCall
from minutehand.domain.telemetry import BytesValue, IntValue, SpanSource, StoredSpan, StringValue

JSON = "application/json"
EVENT_STREAM = "text/event-stream"
PARTED_STATUS = 422
"""What a frozen replay answers a call the record does not hold with: a status no model client retries."""


@dataclass(frozen=True)
class Recorded:
    """One answer the model gave, as the agent received it."""

    status: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class Parted:
    """A frozen replay's answer to a call the record does not hold: refused, never sent to the model."""

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
    frozen: bool = False
    """Refuse the first call the record does not hold, and every call after it, instead of sending it to the model."""
    _answers: dict[tuple[str, str, str], deque[Recorded]] = field(default_factory=dict)
    _pending: list[tuple[str, str, str]] = field(default_factory=list)
    """Every recorded call not yet answered, in the order it was made: what the record expects next."""
    parted: str | None = None
    """Why the runs parted: the first call with no recorded answer, or the moment live play began; None while
    every call has been answered from the recording."""
    parted_at: PartedCalls | None = None
    """What the agent sent at the call the runs parted on, beside what the record held next; only once parted."""
    answered: int = 0

    @classmethod
    def of(
        cls, source: str, spans: Sequence[StoredSpan], *, live_from: datetime | None = None, frozen: bool = False
    ) -> ModelReplay:
        """The model calls `source` recorded on the wire, oldest first."""
        replay = cls(source=source, live_from=live_from, frozen=frozen)
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
            replay._pending.append(key)
        return replay

    def answer(self, host: str, path: str, body: bytes, content_type: str, now: datetime) -> Recorded | Parted | None:
        """The recorded answer to this call; None, from now on, once the runs have parted and the call goes to the
        model; `Parted`, from now on, once a frozen replay has parted and nothing goes anywhere."""
        if self.parted is not None:
            return self._refusal() if self.frozen else None
        if self.live_from is not None and now >= self.live_from:
            self.parted = f"live from {self.live_from.isoformat()}"
            return None
        key = _key(host, path, kept_body(body, content_type))
        waiting = self._answers.get(key)
        if not waiting:
            self.parted = f"{host}{path.split('?', 1)[0]}: a call {self.source} never made, at {now.isoformat()}"
            self.parted_at = PartedCalls(sent=_call(key), expected=_call(self._pending[0]) if self._pending else None)
            return self._refusal() if self.frozen else None
        self.answered += 1
        self._pending.remove(key)
        return waiting.popleft()

    def _refusal(self) -> Parted:
        said = {
            "error": {
                "type": "minutehand_frozen_replay",
                "message": f"frozen replay of run {self.source}: {self.parted}. The record does not hold this call, "
                "and a frozen replay sends nothing to a model; the run stops here, REPLAY_PARTED",
            }
        }
        return Parted(PARTED_STATUS, JSON, json.dumps(said).encode())


def _call(key: tuple[str, str, str]) -> ReplayedCall:
    host, path, body = key
    return ReplayedCall(host=host, path=path, body=body)


def _guessed(body: bytes) -> str:
    """A recording made before content types were kept: a stream starts with an event's field."""
    return EVENT_STREAM if body.lstrip().startswith((b"data:", b"event:")) else JSON
