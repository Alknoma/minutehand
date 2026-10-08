"""Every Microsoft host as one ASGI app over the run's store and clock, dispatched by the `Host` a call went to.

| Host | Surface |
|---|---|
| `login.microsoftonline.com` | sign-in (`signin.py`) |
| `login.botframework.com` | the Bot Framework's OpenID metadata and keys |
| `smba.trafficmanager.net` | the Bot Framework connector (`connector.py`) |
| `graph.microsoft.com` | Graph `v1.0`: users, Teams, chats (`graph_teams.py`), files (`graph_files.py`), mail (`graph_mail.py`), calendars (`graph_calendar.py`), subscriptions |
| `*.sharepoint.com` | pre-authenticated downloads, upload sessions and copy monitors handed out by Graph |

A path's repeated slashes are folded into one before routing: a bot that joins `serviceUrl` (which ends in `/`)
to `/v3/…` sends `//v3`, and the connector answers it. A fault the scenario declares is answered in front of the
surface it names, in that surface's own error shape, and used up one call at a time.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Router
from starlette.types import Message, Receive, Scope, Send

from minutehand.adapters import answering
from minutehand.adapters.providers.microsoft import surface, wire
from minutehand.adapters.providers.microsoft.common import JSON, GraphRefusal, graph_error
from minutehand.adapters.providers.microsoft.connector import connector_router
from minutehand.adapters.providers.microsoft.graph_calendar import CALENDAR_SEGMENTS, Calendar
from minutehand.adapters.providers.microsoft.graph_files import Caller, Files
from minutehand.adapters.providers.microsoft.graph_mail import MAIL_SEGMENTS, Mail, split_segments
from minutehand.adapters.providers.microsoft.graph_teams import TeamsGraph
from minutehand.adapters.providers.microsoft.signin import bot_framework_router, login_router
from minutehand.adapters.providers.microsoft.state import (
    CONNECTOR_HOST,
    FAULTS,
    MicrosoftWorld,
    fault_ref,
)
from minutehand.adapters.providers.microsoft.subscriptions import (
    DRIVE_LIMIT,
    MESSAGES_LIMIT,
    OUTLOOK_LIMIT,
    Subscriptions,
    calendar_watch,
    conversation_watch,
    drive_watch,
    mail_watch,
)
from minutehand.domain.world import Actor, Operation, RecordSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

LOGIN_HOST = "login.microsoftonline.com"
BOT_LOGIN_HOST = "login.botframework.com"
GRAPH_HOST = "graph.microsoft.com"
SHAREPOINT_SUFFIX = ".sharepoint.com"

ASGI = Callable[[Scope, Receive, Send], Awaitable[None]]


class GraphApp:
    """Graph `v1.0`: one entry that reads the first segment and hands the call to the surface that owns it."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._world = MicrosoftWorld(store)
        self._clock = clock
        self.files = Files(self._world, clock)
        self._teams = TeamsGraph(self._world, clock)
        self.mail = Mail(self._world, clock)
        self.calendar = Calendar(self._world, clock, self.mail)
        self._subscriptions = Subscriptions(self._world, clock, self._watchable)

    def _outlook(self, resource: str, claims: wire.Claims | None) -> str | None:
        """What a subscription to a mailbox's messages (all, or one folder's) or its events watches; None when the
        resource is neither."""
        parts = split_segments([p for p in resource.strip("/").split("/") if p])
        lowered = [p.lower() for p in parts]
        if lowered[0] == "me":
            key, rest = (claims.oid if claims is not None else None), lowered[1:]
        elif lowered[0] == "users" and len(parts) >= 2:
            key, rest = parts[1], lowered[2:]
        else:
            return None
        if rest not in (["messages"], ["events"], ["calendar", "events"]) and not (
            len(rest) == 3 and rest[0] == "mailfolders" and rest[2] == "messages"
        ):
            return None
        if key is None:
            found = next((s for s in self._world.subscriptions() if s.subscription.resource == resource), None)
            if found is None:
                raise NotImplementedError(
                    "a subscription whose resource is no longer known: Graph's answer is not recorded"
                )
            return found.watches
        user = self._world.user_by(key)
        if user is None:
            raise NotImplementedError(f"a subscription to {resource!r}, which the world does not hold: not recorded")
        if claims is not None and claims.oid is not None and claims.oid != user.user.id:
            raise NotImplementedError("a user's subscription to another user's mailbox: Graph's answer is not recorded")
        if rest[-1] == "events":
            return calendar_watch(user.user.id)
        if rest == ["messages"]:  # enum-lint: exempt Graph's path segment
            return mail_watch(user.user.id, None)
        return mail_watch(user.user.id, self.mail.folder(user, parts[-2]).value)

    def _watchable(self, resource: str, claims: wire.Claims | None) -> tuple[str, timedelta]:
        parts = [p for p in resource.strip("/").split("/") if p]
        outlook = self._outlook(resource, claims)
        if outlook is not None:
            return outlook, OUTLOOK_LIMIT
        if parts[-1:] == ["root"] and len(parts) >= 2:
            drive_parts = parts[:-1]
            if drive_parts[0] == "me" and claims is None:
                found = next((s for s in self._world.subscriptions() if s.subscription.resource == resource), None)
                if found is None:
                    raise NotImplementedError(
                        "a subscription whose resource is no longer known: Graph's answer is not recorded"
                    )
                return found.watches, DRIVE_LIMIT
            if claims is not None:
                address = self.files.address(Caller(claims), drive_parts)
            else:
                drive = self._world.drive(drive_parts[1]) if drive_parts[0] == "drives" else None
                if drive is None:
                    raise NotImplementedError(
                        "a subscription whose resource is no longer known: Graph's answer is not recorded"
                    )
                return drive_watch(drive.drive.id), DRIVE_LIMIT
            return drive_watch(address.drive.drive.id), DRIVE_LIMIT
        if parts[-1:] == ["messages"]:
            if parts[0] == "chats" and len(parts) == 3:
                chat = self._world.conversation_by_graph_id(parts[1])
                if chat is not None:
                    return conversation_watch(chat.id), MESSAGES_LIMIT
            if parts[0] == "teams" and len(parts) == 5 and parts[2] == "channels":
                channel = self._world.conversation(parts[3])
                if channel is not None and channel.team_id == parts[1]:
                    return conversation_watch(channel.id), MESSAGES_LIMIT
            raise NotImplementedError(f"a subscription to {resource!r}, which the world does not hold: not recorded")
        raise NotImplementedError(f"a subscription to {resource!r}: not a resource this provider watches")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        try:
            response = await self._route(request)
        except GraphRefusal as refusal:
            response = graph_error(refusal, self._clock, request)
        await response(scope, receive, send)

    async def _route(self, request: Request) -> Response:
        path = request.url.path
        if not path.startswith("/v1.0/"):
            raise NotImplementedError("Graph is served at v1.0 only")
        if not surface.served(request.method, path.removeprefix("/v1.0")):
            raise NotImplementedError("not on the Graph v1.0 surface this provider serves (surface.SERVED)")
        parts = [p for p in path.removeprefix("/v1.0/").split("/") if p]
        head = parts[0]
        if head == "subscriptions":
            return await self._subscription(request, parts)
        if (
            head in ("drives", "sites")
            or (len(parts) >= 2 and parts[1] == "drive")
            or (len(parts) >= 3 and parts[2] == "drive")
        ):
            return await self.files.answer(request, parts)
        if head in ("users", "me"):
            after = parts[1:2] if head == "me" else parts[2:3]
            segment = after[0].split("(")[0] if after else ""
            if segment in MAIL_SEGMENTS:
                return await self.mail.answer(request, parts)
            if segment in CALENDAR_SEGMENTS:
                return await self.calendar.answer(request, parts)
            return await self._teams.users(request, parts)
        if head == "teams":
            return await self._teams.teams(request, parts)
        if head == "chats":
            return await self._teams.chats(request, parts)
        if head == "communications":
            return await self._teams.communications(request, parts)
        raise NotImplementedError(f"the segment {head!r}")

    async def _subscription(self, request: Request, parts: list[str]) -> Response:
        method = request.method
        if len(parts) == 1 and method == "POST":
            return await self._subscriptions.create(request)
        if len(parts) == 1 and method == "GET":
            return await self._subscriptions.list(request)
        if len(parts) == 2:
            request.scope["path_params"] = {"sub": parts[1]}
            if method == "GET":
                return await self._subscriptions.get(request)
            if method == "PATCH":
                return await self._subscriptions.renew(request)
            if method == "DELETE":  # enum-lint: exempt HTTP's method name
                return await self._subscriptions.delete(request)
        raise NotImplementedError(f"{method} on subscriptions")


class SharePointHost:
    """What Graph hands out on the site's own host: a download, an upload session, a copy's monitor. Each URL
    carries its own credential (`tempauth`), as SharePoint's do; Minutehand does not enforce it."""

    def __init__(self, graph: GraphApp, clock: Clock) -> None:
        self._files = graph.files
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        try:
            response = await self._route(request)
        except GraphRefusal as refusal:
            response = graph_error(refusal, self._clock, request)
        await response(scope, receive, send)

    async def _route(self, request: Request) -> Response:
        path = request.url.path
        if path == "/_layouts/15/download.aspx":
            item = request.query_params["UniqueId"] if "UniqueId" in request.query_params else ""
            return self._files.download(item)
        monitor = re.fullmatch(r"/_api/v2\.0/monitor/([^/]+)", path)
        if monitor is not None:
            return self._files.monitor(monitor.group(1))
        upload = re.fullmatch(r"/_api/v2\.0/drives/[^/]+/items/[^/]+/uploadSession", path)
        if upload is not None:
            guid = (request.query_params["guid"] if "guid" in request.query_params else "").strip("'")
            return await self._files.upload_fragment(request, guid)
        raise NotImplementedError("nothing Graph hands out is at this address")


class MicrosoftApp:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._world = MicrosoftWorld(store)
        self._clock = clock
        self._login = login_router(store, clock)
        self._bot_login = bot_framework_router()
        self._connector = connector_router(store, clock)
        self._graph = GraphApp(store, clock)
        self._sharepoint = SharePointHost(self._graph, clock)

    def _surface(self, host: str) -> tuple[str, ASGI | Router] | None:
        if host == LOGIN_HOST:
            return "login", self._login
        if host == BOT_LOGIN_HOST:
            return "login", self._bot_login
        if host == CONNECTOR_HOST:
            return "connector", self._connector
        if host == GRAPH_HOST:
            return "graph", self._graph
        if host.endswith(SHAREPOINT_SUFFIX):
            return "graph", self._sharepoint
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        headers = dict(scope["headers"])
        host = headers[b"host"].decode().split(":")[0].lower() if b"host" in headers else ""
        scope["path"] = re.sub(r"/{2,}", "/", _decoded_path(scope))
        found = self._surface(host)
        if found is None:
            await Response("No Microsoft service answers at this host.", status_code=404)(scope, receive, send)
            return
        surface, app = found
        faulted = self._fault(surface, scope)
        if faulted is WITHOUT_ID:
            await app(scope, receive, _without_id(send))
            return
        if isinstance(faulted, Response):
            await faulted(scope, receive, send)
            return
        await app(scope, receive, send)

    def _fault(self, surface: str, scope: Scope) -> Response | object | None:
        """The first fault the scenario declares for this call that still has calls to fail, used up by one."""
        if surface == "login":
            return None
        method, path = scope["method"], scope["path"]
        now = int(self._clock.now().timestamp())
        for fault in self._world.faults():
            if fault.call is not None:
                wanted_method, _, wanted_path = fault.call.rpartition(" ")
                if (wanted_method and wanted_method.upper() != method) or not path.startswith(wanted_path):
                    continue
            if (fault.remaining is not None and fault.remaining < 1) or now < fault.from_time:
                continue
            if (fault.only_rich or fault.without_id) and (surface != "connector" or method != "POST"):
                continue
            left = None if fault.remaining is None else fault.remaining - 1
            self._world.write(
                fault_ref(fault.position),
                fault.model_copy(update={"remaining": left}),
                operation=Operation.UPDATE,
                actor=Actor.SCENARIO,
                parent=FAULTS,
                after=RecordSnapshot(
                    resource="faults",
                    text=f"{method} {path} answered without its id on purpose"
                    if fault.without_id
                    else f"{method} {path} failed on purpose: {fault.error}",
                ),
            )
            answering.injected()
            if fault.without_id:
                return WITHOUT_ID
            headers = {"Retry-After": str(fault.retry_after)} if fault.retry_after is not None else None
            if surface == "connector":
                body = wire.ConnectorError(
                    error=wire.ConnectorErrorBody(code=fault.error, message="Failed on purpose by the scenario.")
                )
                return Response(wire.dump(body), status_code=fault.status, media_type=JSON, headers=headers)
            refusal = GraphRefusal(
                fault.status, fault.error, "Failed on purpose by the scenario.", retry_after=fault.retry_after
            )
            return graph_error(refusal, self._clock, Request(scope))
        return None


WITHOUT_ID = object()
"""What `_fault` answers for a send to be carried out and answered without its id."""

_IDS = ("id", "activityId")


def _without_id(send: Send) -> Send:
    """`send`, with the id of what was sent taken out of a successful JSON answer: the connector's `id`, and a new
    conversation's `activityId`."""
    started: list[Message] = []

    async def sending(message: Message) -> None:
        if message["type"] == "http.response.start":
            started.append(message)
            return
        if message["type"] != "http.response.body" or not started:
            await send(message)
            return
        start = started.pop()
        body: bytes = message["body"] if "body" in message else b""
        if 200 <= start["status"] < 300 and body:
            answered = json.loads(body)
            if isinstance(answered, dict):
                body = json.dumps({k: v for k, v in answered.items() if k not in _IDS}).encode()
        headers = [(k, v) for k, v in start["headers"] if k != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode()))
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return sending


def _decoded_path(scope: Scope) -> str:
    """The path as sent, percent-decoded once. Read from `raw_path` because the proxy hands an app its path still
    quoted (`a%3A…` for a connector conversation `a:…`), where another server hands it decoded."""
    raw = scope["raw_path"] if scope.get("raw_path") else None
    if raw is None:
        return scope["path"]
    text = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
    return unquote(text.split("?", 1)[0])


def build_app(store: Store, clock: Clock) -> MicrosoftApp:
    return MicrosoftApp(store, clock)
