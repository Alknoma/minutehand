"""A provider whose app lets out each kind of exception, UNGUARDED, so the proxy's own guard is what answers.

`GET /fine` answers 200; `GET /missing` answers its own 404 without raising; `GET /refuse` raises its refusal;
`GET /fault` raises it as a fault armed on purpose; `GET /later` raises `NotImplementedError`; `GET /boom` raises
an error of Minutehand's own. Its error shape is `{"fault": {"code": ..., "message": ...}}`.
"""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from minutehand.domain.errors import Rendered, ServiceRefusal
from minutehand.domain.scenario import Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store

from .manifest import MANIFEST


def shaped(status: int, code: str, message: str) -> Rendered:
    return Rendered(
        status=status,
        content_type="application/json",
        body=json.dumps({"fault": {"code": code, "message": message}}).encode(),
    )


class Forbidden(ServiceRefusal):
    def __init__(self, *, deliberate: bool = False) -> None:
        super().__init__("forbidden_here", "you may not", status=403, deliberate=deliberate)

    def render(self) -> Rendered:
        return shaped(self.status, self.code, self.message)


class Faulty:
    manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        async def fine(_: Request) -> JSONResponse:
            return JSONResponse({"ok": True})

        async def missing(_: Request) -> JSONResponse:
            return JSONResponse({"fault": {"code": "not_found", "message": "no such thing"}}, status_code=404)

        async def refuse(_: Request) -> JSONResponse:
            raise Forbidden()

        async def fault(_: Request) -> JSONResponse:
            raise Forbidden(deliberate=True)

        async def later(_: Request) -> JSONResponse:
            raise NotImplementedError("later")

        async def boom(_: Request) -> JSONResponse:
            raise RuntimeError("kaboom")

        return Starlette(
            routes=[
                Route("/fine", fine),
                Route("/missing", missing),
                Route("/refuse", refuse),
                Route("/fault", fault),
                Route("/later", later),
                Route("/boom", boom),
            ]
        )

    def seed(self, scenario: Scenario, world: Store) -> None:
        """Holds nothing to seed."""

    def error(self, status: int, code: str, message: str) -> Rendered:
        return shaped(status, code, message)


def build() -> Faulty:
    return Faulty()
