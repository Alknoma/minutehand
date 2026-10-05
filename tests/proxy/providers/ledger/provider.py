"""A provider that keeps entries in the store, stamped by the run's clock.

`POST /entries` creates one entry (one event); `GET /whoami` reports what the app
saw of the request and writes nothing (no events).
"""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, RecordSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store

from .manifest import MANIFEST


def _entry(world: Store, actor: Actor, text: str) -> int:
    external_id = f"e{world.head() + 1}"
    event = world.apply(
        Change(
            entity=EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=external_id),
            operation=Operation.CREATE,
            actor=actor,
            body=json.dumps({"id": external_id, "text": text}),
            after=RecordSnapshot(resource="entries", text=text),
        )
    )
    return event.seq


class Ledger:
    manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        async def whoami(request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "path": request.url.path,
                    "host": request.headers["host"],
                    "authorized": "authorization" in request.headers,
                    "access_token": "issued-by-ledger",
                }
            )

        async def create(request: Request) -> JSONResponse:
            payload = await request.json()
            seq = _entry(world, Actor.AGENT, str(payload["text"]))
            return JSONResponse({"seq": seq, "created": clock.now().isoformat()}, status_code=201)

        async def entry_action(request: Request) -> JSONResponse:
            """`/entries/{id}:{action}`, Google's custom-method shape: routed only when the path arrives decoded."""
            return JSONResponse({"entry": request.path_params["entry"], "action": request.path_params["action"]})

        return Starlette(
            routes=[
                Route("/whoami", whoami),
                Route("/entries", create, methods=["POST"]),
                Route("/entries/{entry}:{action}", entry_action),
            ]
        )

    def seed(self, scenario: Scenario, world: Store) -> None:
        for ticket in scenario.tickets:
            if ticket.provider == MANIFEST.key:
                _entry(world, Actor.SCENARIO, ticket.title)


def build() -> Ledger:
    return Ledger()
