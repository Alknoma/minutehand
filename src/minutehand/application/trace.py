"""The agent's calls answered from a run's record, to play its sandbox back to a checkpoint (`application.playback`).
The proxy asks here for each call the agent makes while a playback is on (`adapters.proxy.addon`).

The record holds, for every call the agent made, what it was answered: the status, the body and the headers
(`Exchange`). Playing a fork's agent back means answering each call it makes again with exactly that, in the order
the record holds calls alike, and writing nothing: the fakes are not asked, since the world the record was made
against is the one the fork shares, and what they answered then is what they would answer now.

A call the record holds no answer for next is where the agent did not do what it did before (`parted`): it sent
something the record does not hold, so either its program changed or it draws on something outside the record (a
random number, a thread's timing). The playback stops there and the fork is refused, naming the call.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from minutehand.domain.world import Exchange, Header, RecordedCall

JSON = "application/json"
PARTED_STATUS = 422
"""What a parted playback answers the call with: a status no client retries, so the sandbox falls idle."""


@dataclass(frozen=True)
class Answered:
    """One answer as the agent received it."""

    status: int
    headers: list[Header]
    body: bytes


@dataclass(frozen=True)
class Parted:
    """The answer to a call the record does not hold: refused, and the playback is over."""

    status: int
    body: bytes


Key = tuple[str, str, str, bytes]
"""Method, host, path with its query, the body: each as the record keeps it (credentials redacted), which the caller
redacts a live call to the same way, so a call compares equal to its own record."""


def _recorded_key(exchange: Exchange) -> Key:
    body = exchange.request_body.encode("utf-8") if exchange.request_body is not None else exchange.request_bytes
    return exchange.method, exchange.host, exchange.path, body or b""


def _recorded_body(exchange: Exchange) -> bytes:
    if exchange.response_body is not None:
        return exchange.response_body.encode("utf-8")
    return exchange.response_bytes or b""


class Unplayable(ValueError):
    """The record holds a call that cannot be played back: no headers were kept for it, or it was relayed unopened."""


@dataclass
class TraceReplay:
    """A run's recorded calls up to a checkpoint, answered in turn until the agent parts from them."""

    source: str
    """The run whose record answers."""
    _answers: dict[Key, deque[RecordedCall]] = field(default_factory=dict)
    _pending: list[RecordedCall] = field(default_factory=list)
    answered: int = 0
    reached_seq: int = 0
    """The head of the record as far as the agent has been answered: what its memory reads are answered as of."""
    parted: str | None = None
    parted_held: str | None = None

    @classmethod
    def of(cls, source: str, calls: Sequence[RecordedCall]) -> TraceReplay:
        """`calls` are every call of the record up to the checkpoint, in the order recorded. One relayed unopened
        on a tunnel, carried on a WebSocket or gRPC, or recorded without its answer's headers cannot be played back:
        `Unplayable`, naming it."""
        made = cls(source=source)
        for call in calls:
            x = call.exchange
            if x.tunnelled is not None:
                raise Unplayable(
                    f"{x.method} {x.host}{x.path} at wake {call.wake} was relayed unopened on a tunnel, so the record "
                    "does not hold what it was answered: a run played back through a fork records its model calls "
                    "(the default), not `--tunnel-model-calls`"
                )
            if x.grpc is not None or x.frame is not None:
                how = "a gRPC call" if x.grpc is not None else "a WebSocket message"
                raise Unplayable(
                    f"{x.method} {x.host}{x.path} at wake {call.wake} is {how}, which is not played back yet"
                )
            if x.inbox_call is not None:
                continue  # Minutehand's own call to the agent's product: sent again as a delivery, not answered
            if x.response_headers is None:
                raise Unplayable(
                    f"{x.method} {x.host}{x.path} at wake {call.wake} was recorded without its answer's headers (a "
                    "run recorded before they were kept), so what the agent received cannot be played back"
                )
            made._answers.setdefault(_recorded_key(x), deque()).append(call)
            made._pending.append(call)
        return made

    @property
    def remaining(self) -> list[RecordedCall]:
        """The recorded calls the agent has not made again, in the order the record holds them."""
        return list(self._pending)

    def answer(self, key: Key) -> Answered | Parted:
        """The answer the record holds for the next call alike, or the refusal that parts the playback."""
        if self.parted is not None:
            return Parted(status=PARTED_STATUS, body=self.parted.encode())
        queue = self._answers.get(key)
        if not queue:
            sent = f"{key[0]} {key[1]}{key[2].split('?', 1)[0]}"
            held = self._pending[0] if self._pending else None
            self.parted = (
                f"the playback of run {self.source} parted: the agent sent {sent}, which the record does not hold there"
            )
            self.parted_held = (
                f"{held.exchange.method} {held.exchange.host}{held.exchange.path.split('?', 1)[0]} (wake {held.wake})"
                if held is not None
                else None
            )
            return Parted(status=PARTED_STATUS, body=self.parted.encode())
        call = queue.popleft()
        self._pending.remove(call)
        self.answered += 1
        self.reached_seq = max(self.reached_seq, call.last_seq)
        x = call.exchange
        assert x.response_headers is not None
        return Answered(status=x.status, headers=list(x.response_headers), body=_recorded_body(x))
