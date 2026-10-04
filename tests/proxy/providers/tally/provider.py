"""A provider with one read-only route. It writes nothing, so its calls produce no events."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from minutehand.domain.scenario import Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store

from .manifest import MANIFEST


class Tally:
    manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        async def count(request: Request) -> JSONResponse:
            return JSONResponse({"events": world.head(), "at": clock.now().isoformat()})

        return Starlette(routes=[Route("/", count)])

    def seed(self, scenario: Scenario, world: Store) -> None:
        """Tally holds nothing a scenario can seed; every call reads the run's head."""


def build() -> Tally:
    return Tally()
