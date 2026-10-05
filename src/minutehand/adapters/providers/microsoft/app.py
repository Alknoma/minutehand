"""Every Microsoft host as one ASGI app over the run's store and clock, dispatched by the `Host` a call went to.

| Host | Surface |
|---|---|
| `login.microsoftonline.com` | sign-in (`signin.py`) |
| `login.botframework.com` | the Bot Framework's OpenID metadata and keys |
| `smba.trafficmanager.net` | the Bot Framework connector (`connector.py`) |
| `graph.microsoft.com` | Graph `v1.0`: users, Teams, chats (`graph_teams.py`), files (`graph_files.py`), subscriptions |
| `*.sharepoint.com` | pre-authenticated downloads, upload sessions and copy monitors handed out by Graph |

A path's repeated slashes are folded into one before routing: a bot that joins `serviceUrl` (which ends in `/`)
to `/v3/…` sends `//v3`, and the connector answers it. A fault the scenario declares is raised in front of the
surface it names, as that surface's own refusal made on purpose, and used up one call at a time. A refusal leaves
the app stamped with the moment and the call it answers (`MicrosoftRefusal.answered`), and the guard renders it; a
call no route of the fake takes is an operation it does not implement (`unrouted`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match, Route, Router
from starlette.types import Message, Receive, Scope, Send

from minutehand.adapters.answering import unrouted
from minutehand.adapters.providers.microsoft import tokens, wire
from minutehand.adapters.providers.microsoft.common import GraphRefusal, MicrosoftRefusal
from minutehand.adapters.providers.microsoft.connector import ConnectorRefusal, connector_router
from minutehand.adapters.providers.microsoft.graph_files import Caller, Files
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
    Subscriptions,
    conversation_watch,
    drive_watch,
)
from minutehand.adapters.providers.microsoft.wire import TokenUse
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
        self.files = Files(self._world, clock)
        self._teams = TeamsGraph(self._world, clock)
        self._subscriptions = Subscriptions(self._world, clock, self._watchable)

    def _watchable(self, resource: str, claims: wire.Claims | None) -> tuple[str, timedelta]:
        parts = [p for p in resource.strip("/").split("/") if p]
        if parts[-1:] == ["root"] and len(parts) >= 2:
            drive_parts = parts[:-1]
            if drive_parts[0] == "me" and claims is None:
                found = next((s for s in self._world.subscriptions() if s.subscription.resource == resource), None)
                if found is None:
                    raise GraphRefusal(404, "ResourceNotFound", "The subscription's resource is no longer known.")
                return found.watches, DRIVE_LIMIT
            if claims is not None:
                address = self.files.address(Caller(claims), drive_parts)
            else:
                drive = self._world.drive(drive_parts[1]) if drive_parts[0] == "drives" else None
                if drive is None:
                    raise GraphRefusal(404, "ResourceNotFound", "The subscription's resource is no longer known.")
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
            raise GraphRefusal(404, "ResourceNotFound", f"The resource '{resource}' could not be found.")
        raise GraphRefusal(400, "ExtensionError", f"Subscriptions to '{resource}' are not supported.")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = await self._route(Request(scope, receive))
        await response(scope, receive, send)

    async def _route(self, request: Request) -> Response:
        path = request.url.path
        parts = [p for p in path.removeprefix("/v1.0/").split("/") if p] if path.startswith("/v1.0/") else []
        if not parts:
            raise unrouted(request.method, path, GRAPH_ROUTES)
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
            return await self._teams.users(request, parts)
        if head == "teams":
            return await self._teams.teams(request, parts)
        if head == "chats":
            return await self._teams.chats(request, parts)
        if head == "communications":
            return await self._teams.communications(request, parts)
        raise unrouted(request.method, path, GRAPH_ROUTES)

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
        if len(parts) > 2:
            raise unrouted(method, request.url.path, GRAPH_ROUTES)
        raise GraphRefusal(405, "BadRequest", f"{method} is not allowed on subscriptions.")


class SharePointHost:
    """What Graph hands out on the site's own host: a download, an upload session, a copy's monitor. Each URL
    carries its own credential (`tempauth`), as SharePoint's do."""

    def __init__(self, graph: GraphApp) -> None:
        self._files = graph.files

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = await self._route(Request(scope, receive))
        await response(scope, receive, send)

    def _authorised(self, request: Request, audience: str) -> None:
        token = request.query_params["tempauth"] if "tempauth" in request.query_params else ""
        try:
            claims = tokens.decode(token, use=TokenUse.ACCESS)
        except tokens.TokenRefused as e:
            raise GraphRefusal(401, "unauthenticated", f"The pre-authenticated URL is not valid: {e.reason}.") from e
        if claims.aud != audience:
            raise GraphRefusal(401, "unauthenticated", "The pre-authenticated URL was issued for something else.")

    async def _route(self, request: Request) -> Response:
        path = request.url.path
        if path == "/_layouts/15/download.aspx":
            item = request.query_params["UniqueId"] if "UniqueId" in request.query_params else ""
            self._authorised(request, f"download {item}")
            return self._files.download(item)
        monitor = re.fullmatch(r"/_api/v2\.0/monitor/([^/]+)", path)
        if monitor is not None:
            return self._files.monitor(monitor.group(1))
        upload = re.fullmatch(r"/_api/v2\.0/drives/[^/]+/items/[^/]+/uploadSession", path)
        if upload is not None:
            guid = (request.query_params["guid"] if "guid" in request.query_params else "").strip("'")
            self._authorised(request, f"upload {guid}")
            return await self._files.upload_fragment(request, guid)
        raise unrouted(request.method, path, SHAREPOINT_ROUTES)


class MicrosoftApp:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._world = MicrosoftWorld(store)
        self._clock = clock
        routers = login_router(store, clock), bot_framework_router(), connector_router(store, clock)
        self._login, self._bot_login, self._connector = (answering(r) for r in routers)
        self._routes = [*(route for r in routers for route in served(r)), *GRAPH_ROUTES, *SHAREPOINT_ROUTES]
        self._graph = GraphApp(store, clock)
        self._sharepoint = SharePointHost(self._graph)

    def _surface(self, host: str) -> tuple[str, ASGI] | None:
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
        try:
            await self._answer(host, scope, receive, send)
        except MicrosoftRefusal as refusal:
            refusal.answered(self._clock, Request(scope))
            raise

    async def _answer(self, host: str, scope: Scope, receive: Receive, send: Send) -> None:
        found = self._surface(host)
        if found is None:
            raise unrouted(scope["method"], scope["path"], self._routes)
        surface, app = found
        if self._fault(surface, scope):
            await app(scope, receive, _without_id(send))
            return
        await app(scope, receive, send)

    def _fault(self, surface: str, scope: Scope) -> bool:
        """The first fault the scenario declares for this call that still has calls to fail, used up by one: raised
        as the surface's refusal, made on purpose; True when it lets the call through, to be answered without its
        id."""
        if surface == "login":
            return False
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
            if fault.without_id:
                return True
            refused = ConnectorRefusal if surface == "connector" else GraphRefusal
            raise refused(fault.status, fault.error, FAULTED, retry_after=fault.retry_after, deliberate=True)
        return False


FAULTED = "Failed on purpose by the scenario."

GRAPH_ROUTES = (
    "GET /v1.0/users",
    "GET /v1.0/users/{id}",
    "GET /v1.0/me",
    "GET /v1.0/users/{id}/presence",
    "GET /v1.0/users/{id}/mailboxSettings",
    "GET /v1.0/communications/presences/{id}",
    "POST /v1.0/communications/getPresencesByUserId",
    "GET /v1.0/teams/{team}",
    "GET /v1.0/teams/{team}/channels",
    "GET /v1.0/teams/{team}/channels/{channel}/messages",
    "GET /v1.0/teams/{team}/members",
    "GET /v1.0/chats/{chat}",
    "GET /v1.0/chats/{chat}/messages",
    "GET /v1.0/chats/{chat}/members",
    "GET /v1.0/sites/{site}",
    "GET /v1.0/sites/{site}/drive",
    "GET /v1.0/drives/{drive}",
    "GET /v1.0/drives/{drive}/items/{item}",
    "PATCH /v1.0/drives/{drive}/items/{item}",
    "DELETE /v1.0/drives/{drive}/items/{item}",
    "GET /v1.0/drives/{drive}/items/{item}/children",
    "POST /v1.0/drives/{drive}/items/{item}/children",
    "GET /v1.0/drives/{drive}/items/{item}/content",
    "PUT /v1.0/drives/{drive}/items/{item}/content",
    "GET /v1.0/drives/{drive}/root/delta",
    "POST /v1.0/drives/{drive}/items/{item}/createUploadSession",
    "POST /v1.0/drives/{drive}/items/{item}/copy",
    "POST /v1.0/drives/{drive}/items/{item}/invite",
    "POST /v1.0/drives/{drive}/items/{item}/createLink",
    "GET /v1.0/drives/{drive}/items/{item}/permissions",
    "POST /v1.0/subscriptions",
    "GET /v1.0/subscriptions",
    "GET /v1.0/subscriptions/{id}",
    "PATCH /v1.0/subscriptions/{id}",
    "DELETE /v1.0/subscriptions/{id}",
)
"""What the fake serves of Graph, as `METHOD /path`: `GraphApp` dispatches by hand, so these are written out, to name
the closest one to an operation it does not implement."""

SHAREPOINT_ROUTES = (
    "GET /_layouts/15/download.aspx",
    "GET /_api/v2.0/monitor/{id}",
    "PUT /_api/v2.0/drives/{drive}/items/{item}/uploadSession",
)


def served(router: Router) -> list[str]:
    """The routes of `router`, as `METHOD /path`."""
    return [
        f"{method} {route.path}"
        for route in router.routes
        if isinstance(route, Route)
        for method in sorted(route.methods or ())
        if method != "HEAD"
    ]


def answering(router: Router) -> ASGI:
    """`router`, answering a call none of its routes takes (a path it has not, or a method it has not on a path it
    has) as an operation the fake does not implement, never Starlette's bare 404 or 405."""
    routes = served(router)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        for route in router.routes:
            match, child = route.matches(scope)
            if match is Match.FULL:
                scope.update(child)
                await route.handle(scope, receive, send)
                return
        raise unrouted(scope["method"], scope["path"], routes)

    return app


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
