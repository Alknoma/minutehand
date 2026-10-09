"""Real, minimal providers for the run-loop tests, served over real HTTP on an ephemeral port.

`Chat` is a messaging service with tickets: the agent posts messages and files tickets, people's replies are
recorded as PERSON messages under the agent's inbox and pushed to the agent's inbound URL. `Scheduler` books
wakes. Both read and write only through the store and stamp only from the clock.
"""

from __future__ import annotations

import asyncio
import json
import time
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

from minutehand.application.conversations import PushedConversations
from minutehand.application.run_clock import RunClock
from minutehand.application.traffic import SeenCall
from minutehand.domain.clock import Due, DueKind
from minutehand.domain.people import InboundTarget, PersonMessage, PersonReply
from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.scenario import (
    MessagingHappening,
    Model,
    Person,
    PersonPosts,
    ProviderKey,
    Scenario,
    SeededTicket,
    TicketState,
)
from minutehand.domain.transitions import DELETE, DELETED, Offer, Transition, Waiting, ticket_acts
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
    Stored,
    TicketSnapshot,
)
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp, HeldCalls, Wakes
from minutehand.ports.store import Store
from minutehand.ports.transitions import record

CHAT = "testchat"
SCHED = "testsched"
INBOX = "agent"
SECRET = "the-runs-signing-secret"
SIGNATURE = "x-chat-signature"
"""The header the chat fake signs each push with: the secret itself, which is enough to tell which one it was."""


class MessageIn(Model):
    to: str
    text: str


class MessageEdit(Model):
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
        key=CHAT,
        tier=Tier.FINISHED,
        hosts=["chat.test"],
        kinds=[EntityKind.MESSAGE, EntityKind.TICKET],
        pushes_events=True,
    )

    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.signatures: list[str] = []

    def talking(self, target: InboundTarget | None, secret: str | None) -> ChatPeople:
        """People's answers to the agent's messages, pushed to its inbound URL as `deliver` pushes them, and their
        moves on its tickets."""
        return ChatPeople(self, target, secret)

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        async def post_message(request: Request) -> Response:
            sent = MessageIn.model_validate_json(await request.body())
            ref = EntityRef(provider=CHAT, kind=EntityKind.MESSAGE, external_id=f"m{world.head() + 1}")
            world.apply(
                Change(
                    entity=ref,
                    operation=Operation.CREATE,
                    actor=Actor.AGENT,
                    parent=sent.to,
                    body=json.dumps({"to": sent.to, "text": sent.text}),
                    after=MessageSnapshot(text=sent.text, channel=f"dm:{sent.to}", recipient_emails=[sent.to]),
                )
            )
            return JSONResponse({"id": ref.external_id})

        async def edit_message(request: Request) -> Response:
            ref = EntityRef(provider=CHAT, kind=EntityKind.MESSAGE, external_id=request.path_params["id"])
            stored = world.get(ref)
            if stored is None:
                return JSONResponse({"error": "not_found"}, status_code=404)
            to = MessageIn.model_validate_json(stored.body).to
            text = MessageEdit.model_validate_json(await request.body()).text
            world.apply(
                Change(
                    entity=ref,
                    operation=Operation.UPDATE,
                    actor=Actor.AGENT,
                    parent=stored.parent,
                    body=json.dumps({"to": to, "text": text}),
                    after=MessageSnapshot(text=text, channel=f"dm:{to}", recipient_emails=[to]),
                )
            )
            return JSONResponse({"id": ref.external_id})

        async def inbox(request: Request) -> Response:
            return Response(
                "[" + ",".join(s.body for s in world.children(CHAT, EntityKind.MESSAGE, INBOX)) + "]",
                media_type="application/json",
            )

        async def post_ticket(request: Request) -> Response:
            filed = TicketIn.model_validate_json(await request.body())
            ref = EntityRef(provider=CHAT, kind=EntityKind.TICKET, external_id=f"t{world.head() + 1}")
            body = TicketBody(title=filed.title, assignee=filed.assignee, state=TicketState.OPEN)
            world.apply(
                Change(
                    entity=ref,
                    operation=Operation.CREATE,
                    actor=Actor.AGENT,
                    parent="P",
                    body=body.model_dump_json(),
                    after=TicketSnapshot(title=filed.title, project="P", assignee_email=filed.assignee),
                )
            )
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
            self.signatures.append(request.headers[SIGNATURE])
            return JSONResponse({"ok": True})

        return Starlette(
            routes=[
                Route("/messages", post_message, methods=["POST"]),
                Route("/messages/{id}", edit_message, methods=["PUT"]),
                Route("/inbox", inbox, methods=["GET"]),
                Route("/tickets", post_ticket, methods=["POST"]),
                Route("/tickets/{id}", get_ticket, methods=["GET"]),
                Route("/pushed", pushed, methods=["POST"]),
            ]
        )

    def seed(self, scenario: Scenario, world: Store) -> None:
        for n, seeded in enumerate(t for t in scenario.tickets if t.provider == CHAT):
            people = {p.key: p.email for p in scenario.people}
            email = people[seeded.assignee] if seeded.assignee else None
            world.apply(
                Change(
                    entity=EntityRef(provider=CHAT, kind=EntityKind.TICKET, external_id=f"seed{n}"),
                    operation=Operation.CREATE,
                    actor=Actor.SCENARIO,
                    parent=seeded.project,
                    body=TicketBody(title=seeded.title, assignee=email, state=seeded.state).model_dump_json(),
                    after=TicketSnapshot(
                        title=seeded.title, project=seeded.project, assignee_email=email, state=seeded.state
                    ),
                )
            )

    async def deliver(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        ref = EntityRef(provider=CHAT, kind=EntityKind.MESSAGE, external_id=f"m{world.head() + 1}")
        body = json.dumps({"from": reply.person, "text": reply.text, "in_reply_to": reply.in_reply_to.external_id})
        world.apply(
            Change(
                entity=ref,
                operation=Operation.CREATE,
                actor=Actor.PERSON,
                parent=INBOX,
                body=body,
                after=MessageSnapshot(
                    text=reply.text, channel=f"dm:{reply.person}", thread_of=reply.in_reply_to.external_id
                ),
            )
        )
        async with httpx.AsyncClient() as client:
            (await client.post(target.request_url(), content=body, headers={SIGNATURE: secret})).raise_for_status()

    async def say(
        self, message: PersonMessage, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        ref = EntityRef(provider=CHAT, kind=EntityKind.MESSAGE, external_id=f"m{world.head() + 1}")
        body = json.dumps({"from": message.person, "text": message.text})
        world.apply(
            Change(
                entity=ref,
                operation=Operation.CREATE,
                actor=Actor.PERSON,
                parent=INBOX,
                body=body,
                after=MessageSnapshot(text=message.text, channel=f"dm:{message.person}"),
            )
        )
        async with httpx.AsyncClient() as client:
            (await client.post(target.request_url(), content=body, headers={SIGNATURE: secret})).raise_for_status()

    async def happen(
        self, happening: MessagingHappening, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        """This chat has only messages: a post is said like any message; anything else is refused."""
        if not isinstance(happening, PersonPosts):
            raise ValueError(f"the test chat has nothing a person {happening.kind}")
        message = PersonMessage(person=happening.person, text=happening.text, at=clock.now())
        await self.say(message, target, world, clock, secret=secret)

    def _rewrite(
        self, ticket: EntityRef, actor: Actor, *, state: TicketState | None, assignee_email: str | None, world: Store
    ) -> None:
        stored = world.get(ticket)
        if stored is None:
            raise LookupError(f"no ticket {ticket.external_id}")
        was = TicketBody.model_validate_json(stored.body)
        now = TicketBody(title=was.title, assignee=assignee_email or was.assignee, state=state or was.state)
        world.apply(
            Change(
                entity=ticket,
                operation=Operation.UPDATE,
                actor=actor,
                parent=stored.parent,
                body=now.model_dump_json(),
                after=TicketSnapshot(
                    title=now.title, project=stored.parent, assignee_email=now.assignee, state=now.state
                ),
            )
        )


class ChatPeople:
    """`ProvidesTransitions` for the test chat, bound to one agent: its messages are asks (`PushedConversations`),
    and a ticket assigned to a person and not done waits on them, who moves it to another state, or deletes it."""

    def __init__(self, chat: Chat, target: InboundTarget | None, secret: str | None) -> None:
        self._chat = chat
        self._talk = PushedConversations(CHAT, chat, target, secret)

    def items_for(self, person: Person, world: Store) -> list[Waiting]:
        tickets = [
            Waiting(item=s.entity, state=body.state.value, shown=body.title)
            for s in world.children(CHAT, EntityKind.TICKET, "P", limit=1000) + _seeded(world)
            if (body := TicketBody.model_validate_json(s.body)).assignee == person.email
            and body.state is not TicketState.DONE
        ]
        return [*self._talk.items_for(person, world), *tickets]

    def legal(self, item: EntityRef, by: Actor, who: Person | None, world: Store) -> list[Offer]:
        if item.kind is not EntityKind.TICKET:
            return self._talk.legal(item, by, who, world)
        stored = world.get(item)
        if stored is None:
            return []
        now = TicketBody.model_validate_json(stored.body).state
        moves = [Offer(name=s.value, to_state=s.value, means=s) for s in TicketState if s is not now]
        return [*moves, *(o for o in ticket_acts(now.value) if o.name == DELETE)]

    async def apply(
        self, item: EntityRef, offer: str, by: Actor, who: Person | None, content: str, world: Store, clock: Clock
    ) -> Transition:
        if item.kind is not EntityKind.TICKET:
            return await self._talk.apply(item, offer, by, who, content, world, clock)
        stored = world.get(item)
        if stored is None or offer not in [o.name for o in self.legal(item, by, who, world)]:
            raise ValueError(f"ticket {item.external_id} offers no {offer!r}")
        was = TicketBody.model_validate_json(stored.body).state
        if offer == DELETE:
            world.apply(Change(entity=item, operation=Operation.DELETE, actor=by, parent=stored.parent))
        else:
            self._chat._rewrite(item, by, state=TicketState(offer), assignee_email=None, world=world)
        moved = Transition(
            provider=CHAT,
            item=item,
            name=offer,
            from_state=was.value,
            to_state=DELETED if offer == DELETE else offer,
            by=by,
            who=who.key if who is not None else None,
            content=content,
            at=clock.now(),
        )
        return record(world, moved)

    def heard_of(self, item: EntityRef, who: Person | None, world: Store, clock: Clock) -> bool:
        if item.kind is EntityKind.TICKET:
            return False
        return self._talk.heard_of(item, who, world, clock)

    def seeded(self, scenario: Scenario, ticket: SeededTicket, world: Store) -> EntityRef | None:
        n = [t for t in scenario.tickets if t.provider == CHAT].index(ticket)
        ref = EntityRef(provider=CHAT, kind=EntityKind.TICKET, external_id=f"seed{n}")
        return ref if world.get(ref) is not None else None


def _seeded(world: Store) -> list[Stored]:
    """The chat's seeded tickets, which live under their seeded project."""
    found: list[Stored] = []
    for e in world.events():
        if e.entity.provider == CHAT and e.entity.kind is EntityKind.TICKET and e.entity.external_id.startswith("seed"):
            stored = world.get(e.entity)
            if stored is not None and stored not in found:
                found.append(stored)
    return found


class Scheduler:
    manifest = Manifest(
        key=SCHED, tier=Tier.FINISHED, hosts=["sched.test"], kinds=[EntityKind.RECORD], books_wakes=True
    )

    def __init__(self) -> None:
        self.fired: list[tuple[str, datetime]] = []
        self.advanced: list[tuple[str, datetime]] = []
        self.delivered: list[str] = []
        self.taken_refs: list[str] = []
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
            world.apply(
                Change(
                    entity=EntityRef(provider=SCHED, kind=EntityKind.RECORD, external_id=booking.ref),
                    operation=Operation.CREATE,
                    actor=Actor.AGENT,
                    body=booking.model_dump_json(),
                    after=RecordSnapshot(resource="schedules", text=booking.ref),
                )
            )
            self._bound().book(Due(at=booking.at, kind=DueKind.AGENT_WAKE, ref=booking.ref))
            return JSONResponse({"ok": True})

        async def cancel(request: Request) -> Response:
            ref = request.path_params["ref"]
            world.apply(
                Change(
                    entity=EntityRef(provider=SCHED, kind=EntityKind.RECORD, external_id=ref),
                    operation=Operation.DELETE,
                    actor=Actor.AGENT,
                )
            )
            self._bound().cancel(ref)
            return JSONResponse({"ok": True})

        async def deliveries(request: Request) -> Response:
            """The agent's poll of its queue: every delivery it has not yet acknowledged. Receiving is not taking:
            as with SQS, a delivery is taken when the agent deletes it, after acting on it."""
            return JSONResponse({"deliveries": [ref for ref in self.delivered if ref not in self.taken_refs]})

        async def acknowledge(request: Request) -> Response:
            self.taken_refs.append(request.path_params["ref"])
            return JSONResponse({"ok": True})

        return Starlette(
            routes=[
                Route("/schedules", book, methods=["POST"]),
                Route("/schedules/{ref}", cancel, methods=["DELETE"]),
                Route("/deliveries", deliveries, methods=["GET"]),
                Route("/deliveries/{ref}", acknowledge, methods=["DELETE"]),
            ]
        )

    def taken(self, ref: str, world: Store) -> bool:
        """`ConfirmsDelivery`: the agent's poll has picked this booking's delivery up."""
        return ref in self.taken_refs

    def seed(self, scenario: Scenario, world: Store) -> None:
        """A scheduler starts with no bookings: a scenario has nothing to seed here."""

    async def advance_booking(self, ref: str, world: Store, clock: Clock) -> None:
        """Every booking here is one-off: the occurrence is over, and nothing more is booked."""
        self.advanced.append((ref, clock.now()))

    async def deliver_booking(self, ref: str, world: Store, clock: Clock) -> None:
        self.fired.append((ref, clock.now()))
        self.delivered.append(ref)
        world.apply(
            Change(
                entity=EntityRef(provider=SCHED, kind=EntityKind.RECORD, external_id=ref),
                operation=Operation.UPDATE,
                actor=Actor.SCENARIO,
                body=json.dumps({"fired": ref}),
            )
        )


class Switchboard:
    """`Mounts`, served: `/<provider>/<path>` reaches that provider's app for the current run. Also `Traffic`: it
    sees every call the test agents make, as the proxy does."""

    def __init__(self) -> None:
        self.apps: dict[ProviderKey, ASGIApp] = {}
        self.seen: SeenCall | None = None
        self.answering: list[str] = []

    def last_call(self) -> SeenCall | None:
        return self.seen

    def waiting(self) -> list[str]:
        return list(self.answering)

    def mount(
        self,
        world: Store,
        clock: Clock,
        apps: Mapping[ProviderKey, ASGIApp],
        *,
        scenario: Scenario,
        holds: HeldCalls | None = None,
    ) -> None:
        """It answers every call at once: no provider of its says a call waits."""
        self.apps = dict(apps)

    def flush(self) -> None:
        """It relays no tunnel, so nothing it sees is ever recorded later than as it is answered."""

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        path = scope["path"]
        assert isinstance(path, str)
        key, _, rest = path.lstrip("/").partition("/")
        what = f"{scope['method']} {path}"
        self.seen = SeenCall(at=time.monotonic(), what=what)
        app = self.apps[key]
        self.answering.append(what)
        try:
            await app({**scope, "path": "/" + rest, "raw_path": ("/" + rest).encode()}, receive, send)  # type: ignore[arg-type]
        finally:
            self.answering.remove(what)
        self.seen = SeenCall(at=time.monotonic(), what=what)


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
