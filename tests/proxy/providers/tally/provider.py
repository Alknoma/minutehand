"""A provider with read-only routes. It writes nothing, so its calls produce no events.

GET /           the run's head, as JSON
GET /file       a file that is not text: the start of a .docx (a zip) and every byte value
GET /mislabel   JSON that says it is UTF-8 and holds bytes that are not
"""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minutehand.domain.errors import Rendered
from minutehand.domain.scenario import Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store

from .manifest import MANIFEST

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
FILE = b"PK\x03\x04\x14\x00\x06\x00" + bytes(range(256)) * 4
MISLABELLED = b'{"name": "Ren\xe9e", "bad": "\xff\xfe"}'


class Tally:
    manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        async def count(request: Request) -> JSONResponse:
            return JSONResponse({"events": world.head(), "at": clock.now().isoformat()})

        async def file(request: Request) -> Response:
            return Response(FILE, media_type=DOCX)

        async def mislabel(request: Request) -> Response:
            return Response(MISLABELLED, media_type="application/json; charset=utf-8")

        return Starlette(routes=[Route("/", count), Route("/file", file), Route("/mislabel", mislabel)])

    def error(self, status: int, code: str, message: str) -> Rendered:
        return Rendered(
            status=status,
            content_type="application/json",
            body=json.dumps({"error": code, "message": message}).encode(),
        )

    def seed(self, scenario: Scenario, world: Store) -> None:
        """Tally holds nothing a scenario can seed; every call reads the run's head."""


def build() -> Tally:
    return Tally()
