"""Real, minimal providers for the run-loop tests, served over real HTTP on an ephemeral port.

`Chat` is a messaging service with tickets: the agent posts messages and files tickets, people's replies are
recorded as PERSON messages under the agent's inbox and pushed to the agent's inbound URL. `Scheduler` books
wakes. Both read and write only through the store and stamp only from the clock.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import uvicorn
from pydantic import AwareDatetime
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minutehand.application.run_clock import RunClock
from minutehand.domain.clock import Due, DueKind
from minutehand.domain.people import InboundTarget, PersonMessage, PersonReply
from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.scenario import Model, ProviderKey, Scenario, TicketState
from minutehand.domain.world import (
    Actor, Change, EntityKind, EntityRef, MessageSnapshot, Operation, RecordSnapshot, TicketSnapshot,
)
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp, Wakes
from minutehand.ports.store import Store

CHAT = "testchat"
SCHED = "testsched"
INBOX = "agent"


class MessageIn(Model):
    to: str
    text: str


class TicketIn(Model):
    title: str
    assignee: str


class TicketBody(Model):
    title: str
    assignee: str | None
    state: TicketState


class BookingIn(Model):
    ref: str
    at: AwareDatetime


class Chat:
    manifest = Manifest(
        key=CHAT, tier=Tier.FINISHED, hosts=["chat.test"], kinds=[EntityKind.MESSAGE, EntityKind.TICKET],
        pushes_events=True,
    )

    def __init__(self) -> None:
        self.pushed: list[str] = []

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        async def post_message(request: Request) -> Response:
            sent = MessageIn.model_validate_json(await request.body())
            ref = EntityRef(provider=CHAT, kind=EntityKind.MESSAGE, external_id=f"m{world.head() + 1}")
            world.apply(Change(
                entity=ref, operation=Operation.CREATE, actor=Actor.AGENT, parent=sent.to,
                body=json.dumps({"to": sent.to, "text": sent.text}),
                after=MessageSnapshot(text=sent.text, channel=f"dm:{sent.to}", recipient_emails=[sent.to]),
            ))
            return JSONResponse({"id": ref.external_id})

        async def inbox(request: Request) -> Response:
            return Response("[" + ",".join(s.body for s in world.children(CHAT, EntityKind.MESSAGE, INBOX)) + "]",
                            media_type="application/json")

        async def post_ticket(request: Request) -> Response:
            filed = TicketIn.model_validate_json(await request.body())
            ref = EntityRef(provider=CHAT, kind=EntityKind.TICKET, external_id=f"t{world.head() + 1}")
            body = TicketBody(title=filed.title, assignee=filed.assignee, state=TicketState.OPEN)
            world.apply(Change(
                entity=ref, operation=Operation.CREATE, actor=Actor.AGENT, parent="P", body=body.model_dump_json(),
                after=TicketSnapshot(title=filed.title, project="P", assignee_email=filed.assignee),
            ))
            return JSONResponse({"id": ref.external_id})

        async def get_ticket(request: Request) -> Response:
            ref = EntityRef(provider=CHAT, kind=EntityKind.TICKET, external_id=request.path_params["id"])
            stored = world.get(ref)
            world.apply(Change(entity=ref, operation=Operation.READ, actor=Actor.AGENT))
            if stored is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            return Response(stored.body, media_type="application/json")

        async def pushed(request: Request) -> Response:
            self.pushed.append((await request.body()).decode())
            return JSONResponse({"ok": True})

        return Starlette(routes=[
            Route("/messages", post_message, methods=["POST"]),
            Route("/inbox", inbox, methods=["GET"]),
            Route("/tickets", post_ticket, methods=["POST"]),
            Route("/tickets/{id}", get_ticket, methods=["GET"]),
            Route("/pushed", pushed, methods=["POST"]),
        ])

    def seed(self, scenario: Scenario, world: Store) -> None:
        for n, seeded in enumerate(t for t in scenario.tickets if t.provider == CHAT):
            people = {p.key: p.email for p in scenario.people}
            email = people[seeded.assignee] if seeded.assignee else None
            world.apply(Change(
                entity=EntityRef(provider=CHAT, kind=EntityKind.TICKET, external_id=f"seed{n}"),
                operation=Operation.CREATE, actor=Actor.SCENARIO, parent=seeded.project,
                body=TicketBody(title=seeded.title, assignee=email, state=seeded.state).model_dump_json(),
                after=TicketSnapshot(title=seeded.title, project=seeded.project, assignee_email=email,
                                     state=seeded.state),
            ))

    async def deliver(self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock) -> None:
        ref = EntityRef(provider=CHAT, kind=EntityKind.MESSAGE, external_id=f"m{world.head() + 1}")
        body = json.dumps({"from": reply.person, "text": reply.text, "in_reply_to": reply.in_reply_to.external_id})
        world.apply(Change(
            entity=ref, operation=Operation.CREATE, actor=Actor.PERSON, parent=INBOX, body=body,
            after=MessageSnapshot(text=reply.text, channel=f"dm:{reply.person}",
                                  thread_of=reply.in_reply_to.external_id),
        ))
        async with httpx.AsyncClient() as client:
            (await client.post(target.url, content=body)).raise_for_status()

    async def say(self, message: PersonMessage, target: InboundTarget, world: Store, clock: Clock) -> None:
        ref = EntityRef(provider=CHAT, kind=EntityKind.MESSAGE, external_id=f"m{world.head() + 1}")
        body = json.dumps({"from": message.person, "text": message.text})
        world.apply(Change(
            entity=ref, operation=Operation.CREATE, actor=Actor.PERSON, parent=INBOX, body=body,
            after=MessageSnapshot(text=message.text, channel=f"dm:{message.person}"),
        ))
        async with httpx.AsyncClient() as client:
            (await client.post(target.url, content=body)).raise_for_status()

    def transition(self, ticket: EntityRef, to: TicketState, world: Store, clock: Clock) -> None:
        self._rewrite(ticket, Actor.PERSON, state=to, assignee_email=None, world=world)

    def edit(
        self, ticket: EntityRef, *, state: TicketState | None, assignee_email: str | None, world: Store, clock: Clock
    ) -> None:
        self._rewrite(ticket, Actor.SCENARIO, state=state, assignee_email=assignee_email, world=world)

    def _rewrite(
        self, ticket: EntityRef, actor: Actor, *, state: TicketState | None, assignee_email: str | None, world: Store
    ) -> None:
        stored = world.get(ticket)
        if stored is None:
            raise LookupError(f"no ticket {ticket.external_id}")
        was = TicketBody.model_validate_json(stored.body)
        now = TicketBody(title=was.title, assignee=assignee_email or was.assignee, state=state or was.state)
        world.apply(Change(
            entity=ticket, operation=Operation.UPDATE, actor=actor, parent=stored.parent, body=now.model_dump_json(),
            after=TicketSnapshot(title=now.title, project=stored.parent, assignee_email=now.assignee, state=now.state),
        ))


class Scheduler:
    manifest = Manifest(key=SCHED, tier=Tier.FINISHED, hosts=["sched.test"], kinds=[EntityKind.RECORD],
                        books_wakes=True)

    def __init__(self) -> None:
        self.fired: list[tuple[str, datetime]] = []
        self._wakes: Wakes | None = None

    def bind(self, wakes: Wakes) -> None:
        self._wakes = wakes

    def _bound(self) -> Wakes:
        if self._wakes is None:
            raise RuntimeError("the scheduler was never bound to the run's wakes")
        return self._wakes

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        async def book(request: Request) -> Response:
            booking = BookingIn.model_validate_json(await request.body())
            world.apply(Change(
                entity=EntityRef(provider=SCHED, kind=EntityKind.RECORD, external_id=booking.ref),
                operation=Operation.CREATE, actor=Actor.AGENT, body=booking.model_dump_json(),
                after=RecordSnapshot(resource="schedules", text=booking.ref),
            ))
            self._bound().book(Due(at=booking.at, kind=DueKind.AGENT_WAKE, ref=booking.ref))
            return JSONResponse({"ok": True})

        async def cancel(request: Request) -> Response:
            ref = request.path_params["ref"]
            world.apply(Change(entity=EntityRef(provider=SCHED, kind=EntityKind.RECORD, external_id=ref),
                               operation=Operation.DELETE, actor=Actor.AGENT))
            self._bound().cancel(ref)
            return JSONResponse({"ok": True})

        return Starlette(routes=[
            Route("/schedules", book, methods=["POST"]),
            Route("/schedules/{ref}", cancel, methods=["DELETE"]),
        ])

    def seed(self, scenario: Scenario, world: Store) -> None:
        """A scheduler starts with no bookings: a scenario has nothing to seed here."""

    async def fire(self, ref: str, world: Store, clock: Clock) -> None:
        self.fired.append((ref, clock.now()))
        world.apply(Change(
            entity=EntityRef(provider=SCHED, kind=EntityKind.RECORD, external_id=ref),
            operation=Operation.UPDATE, actor=Actor.SCENARIO, body=json.dumps({"fired": ref}),
        ))


class Switchboard:
    """`Mounts`, served: `/<provider>/<path>` reaches that provider's app for the current run."""

    def __init__(self) -> None:
        self.apps: dict[ProviderKey, ASGIApp] = {}

    def mount(self, world: Store, clock: Clock, apps: Mapping[ProviderKey, ASGIApp]) -> None:
        self.apps = dict(apps)

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        path = scope["path"]
        assert isinstance(path, str)
        key, _, rest = path.lstrip("/").partition("/")
        app = self.apps[key]
        await app({**scope, "path": "/" + rest, "raw_path": ("/" + rest).encode()}, receive, send)  # type: ignore[arg-type]


@asynccontextmanager
async def serving(app: object) -> AsyncIterator[str]:
    """Serve an ASGI app on 127.0.0.1 at an ephemeral port inside the test's own event loop; yields its base URL."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning"))  # type: ignore[arg-type]
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.005)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


class RecordingClock(RunClock):
    """The real clock, keeping every jump it was asked to make."""

    def __init__(self, start: datetime) -> None:
        super().__init__(start)
        self.jumps: list[datetime] = []

    def jump(self, to: datetime) -> None:
        self.jumps.append(to)
        super().jump(to)
