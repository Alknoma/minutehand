"""Every Microsoft host as one ASGI app over the run's store and clock, dispatched by the `Host` a call went to.

| Host | Surface |
|---|---|
| `login.microsoftonline.com` | sign-in (`signin.py`) |
| `login.botframework.com` | the Bot Framework's OpenID metadata and keys |
| `smba.trafficmanager.net` | the Bot Framework connector (`connector.py`) |
| `graph.microsoft.com` | Graph `v1.0`: users, Teams, chats (`graph_teams.py`), files (`graph_files.py`), subscriptions |
| `*.sharepoint.com` | pre-authenticated downloads, upload sessions and copy monitors handed out by Graph |

A path's repeated slashes are folded into one before routing: a bot that joins `serviceUrl` (which ends in `/`)
to `/v3/…` sends `//v3`, and the connector answers it. A fault the scenario declares is answered in front of the
surface it names, in that surface's own error shape, and used up one call at a time.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Router
from starlette.types import Receive, Scope, Send

from minutehand.adapters.providers.microsoft import tokens, wire
from minutehand.adapters.providers.microsoft.common import JSON, GraphRefusal, graph_error
from minutehand.adapters.providers.microsoft.connector import connector_router
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
        self._clock = clock
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
        request = Request(scope, receive)
        try:
            response = await self._route(request)
        except GraphRefusal as refusal:
            response = graph_error(refusal, self._clock, request)
        await response(scope, receive, send)

    async def _route(self, request: Request) -> Response:
        path = request.url.path
        if not path.startswith("/v1.0/"):
            raise GraphRefusal(400, "BadRequest", "Invalid version: only v1.0 is served.")
        parts = [p for p in path.removeprefix("/v1.0/").split("/") if p]
        if not parts:
            raise GraphRefusal(400, "BadRequest", "Invalid request")
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
        raise GraphRefusal(400, "BadRequest", f"Resource not found for the segment '{head}'.")

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
        raise GraphRefusal(405, "BadRequest", f"{method} is not allowed on subscriptions.")


class SharePointHost:
    """What Graph hands out on the site's own host: a download, an upload session, a copy's monitor. Each URL
    carries its own credential (`tempauth`), as SharePoint's do."""

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
        raise GraphRefusal(404, "itemNotFound", "Nothing is served at this address.")


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
        if faulted is not None:
            await faulted(scope, receive, send)
            return
        await app(scope, receive, send)

    def _fault(self, surface: str, scope: Scope) -> Response | None:
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
            if fault.only_rich and (surface != "connector" or method != "POST"):
                continue
            left = None if fault.remaining is None else fault.remaining - 1
            self._world.write(
                fault_ref(fault.position),
                fault.model_copy(update={"remaining": left}),
                operation=Operation.UPDATE,
                actor=Actor.SCENARIO,
                parent=FAULTS,
                after=RecordSnapshot(resource="faults", text=f"{method} {path} failed on purpose: {fault.error}"),
            )
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
