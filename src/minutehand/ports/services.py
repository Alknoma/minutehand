"""Declared services (`docs/services.md`), as the proxy and the run loop reach them.

The proxy hands every call to a service's host to `AnswersServices`, which holds the service's state and renders
what the agent is answered; the service's pushes leave through `DeliversPushes`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from minutehand.domain.services import Service
from minutehand.domain.world import AnsweredBy
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store


@dataclass(frozen=True)
class ServiceAnswer:
    """What the agent's call to a service is answered with, who answered it, and what went other than asked."""

    status: int
    body: str
    answered_by: AnsweredBy
    note: str | None = None


@dataclass(frozen=True)
class Call:
    """The agent's call to a service, as the proxy read it: its path with its query, its body as text."""

    method: str
    path: str
    body: str | None


class AnswersServices(Protocol):
    def declared(self, host: str) -> Service | None:
        """The service the scenario declares at `host`, if any."""
        ...

    @property
    def answers_undeclared(self) -> bool:
        """Whether a host nobody declared is answered as a service with no description (`--capture-unknown model`)."""
        ...

    def undeclared(self, host: str) -> Service:
        """`host`, which nobody declared, as a service with no description, its machine proposed from its calls."""
        ...

    async def answer(self, service: Service, call: Call, world: Store, clock: Clock) -> ServiceAnswer:
        """Apply what the call asks of the service's items, if the machine allows it, and render the answer."""
        ...

    async def filed(self, service: Service, item: str, call: Call, route: str, world: Store, clock: Clock) -> None:
        """An item the agent filed through one of the service's `collections`: it enters the machine's first state."""
        ...


@dataclass(frozen=True)
class Delivered:
    status: int | None
    failure: str | None


class DeliversPushes(Protocol):
    async def push(self, url: str, body: str) -> Delivered:
        """POST `body` (JSON) to `url`; never raises for the address's failure, which it returns."""
        ...
