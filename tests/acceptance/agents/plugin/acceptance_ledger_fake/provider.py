"""A fake with a bug and a gap, each deliberate.

GET    /entries        answered: an empty list
POST   /entries        Minutehand's own bug: raises KeyError, as a fake with a missing key would
DELETE /entries/{id}   not implemented by the fake
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store

from .manifest import MANIFEST


async def listed(request: Request) -> JSONResponse:
    del request
    return JSONResponse({"entries": []})


async def added(request: Request) -> JSONResponse:
    held: dict[str, str] = {}
    return JSONResponse({"entry": held[(await request.body()).decode()]})


async def removed(request: Request) -> JSONResponse:
    raise NotImplementedError(f"deleting entry {request.path_params['entry']}")


class LedgerProvider:
    manifest: Manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        del world, clock
        return Starlette(
            routes=[
                Route("/entries", listed, methods=["GET"]),
                Route("/entries", added, methods=["POST"]),
                Route("/entries/{entry}", removed, methods=["DELETE"]),
            ]
        )

    def seed(self, scenario: Scenario, world: Store) -> None:
        """The ledger starts empty whatever the scenario says: nothing in a scenario is the ledger's."""
        del scenario, world


def build() -> LedgerProvider:
    return LedgerProvider()
