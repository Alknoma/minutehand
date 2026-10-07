"""What the proxy saw of the agent's outbound calls, for whoever must know the agent has gone quiet: a sandbox whose
clock Minutehand owns is released only once nothing the agent sent is still awaiting its answer
(`application.sandbox`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SeenCall:
    """The latest outbound call of the agent's that something saw: when, as `time.monotonic()` in this process,
    and what, for a person (`POST slack.com/api/chat.postMessage`)."""

    at: float
    what: str


class Traffic(Protocol):
    """Whoever sees the agent's outbound calls (the proxy): asked how long the agent has been quiet, and what it
    sent that has not been answered yet."""

    def last_call(self) -> SeenCall | None:
        """The latest call seen in this process, or None before the first: a request, an answer, or bytes moving
        on a tunnel the proxy does not open."""
        ...

    def waiting(self) -> list[str]:
        """What the agent sent and is still awaiting an answer to, for a person: a call sent on to a real host, or a
        tunnel whose last bytes went from the agent."""
        ...
