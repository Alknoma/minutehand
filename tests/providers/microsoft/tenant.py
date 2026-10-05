"""A seeded Microsoft tenant over a real `SqliteStore` and `RunClock`, reached the way the services reach it: plain
`httpx` (what they use for sign-in, the connector and Graph) through the real proxy, over TLS signed by its CA.

`Bot` is a bot's messaging endpoint that authenticates every pushed activity with the validation a production bot
runs, re-created from its source: the Bot Framework's OpenID metadata fetched with `httpx`, its key set with PyJWT's
`PyJWKClient`, both through the proxy; `jwt.decode` with RS256, the bot's app id as audience, the Bot Framework's
issuers, `exp`/`iss`/`aud` required, 300 s of leeway; and the `serviceurl` claim equal to the activity's
`serviceUrl`. An activity that fails is answered 401, as that bot answers it.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import uvicorn
from jwt import PyJWKClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from minutehand.adapters.providers.microsoft.provider import MicrosoftProvider, build
from minutehand.adapters.providers.microsoft.state import Directory, MicrosoftWorld, directory_of
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.people import InboundTarget
from minutehand.domain.scenario import Scenario

START = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)

SCENARIO = Scenario.model_validate(
    {
        "name": "vendor_review",
        "goal": "Every vendor contract is reviewed.",
        "owner": "owen",
        "starts_at": START,
        "people": [
            {"key": "owen", "name": "Owen Okafor", "email": "owen@example.com", "title": "Head of Procurement"},
            {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com"},
            {"key": "dania", "name": "Dania Kovac", "email": "dania@example.com"},
        ],
        "documents": [
            {
                "provider": "microsoft",
                "title": "Vendor Plan",
                "text": "Three vendors remain.\nPrices due Friday.",
                "folder": "Plans",
            },
            {"provider": "microsoft", "title": "notes.txt", "text": "Kickoff on Monday."},
            {"provider": "slack", "title": "Not ours", "text": "belongs to another provider"},
        ],
    }
)

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
CONNECTOR = "https://smba.trafficmanager.net/teams/"
BOT_METADATA = "https://login.botframework.com/v1/.well-known/openidconfiguration"
VALID_ISSUERS = [
    "https://api.botframework.com",
    "https://sts.windows.net/d6d49420-f39b-4df7-a1dc-d59a935871db/",
    "https://login.microsoftonline.com/d6d49420-f39b-4df7-a1dc-d59a935871db/v2.0",
]


@dataclass
class Tenant:
    provider: MicrosoftProvider
    store: SqliteStore
    clock: RunClock
    directory: Directory

    @property
    def world(self) -> MicrosoftWorld:
        return MicrosoftWorld(self.store)


def seeded(path: Path, scenario: Scenario = SCENARIO) -> Tenant:
    clock = RunClock(scenario.starts_at)
    store = SqliteStore(path, "root", clock)
    provider = build()
    provider.seed(scenario, store)
    return Tenant(provider=provider, store=store, clock=clock, directory=directory_of(scenario))


@pytest.fixture
def tenant(tmp_path: Path) -> Tenant:
    return seeded(tmp_path / "world.db")


@dataclass
class Intercepted:
    proxy: Proxy
    trust: ssl.SSLContext

    def http(self, **kwargs: Any) -> httpx.AsyncClient:
        """`httpx.AsyncClient` as the services build it, reaching the internet through the proxy."""
        return httpx.AsyncClient(proxy=self.proxy.url, verify=self.trust, trust_env=False, **kwargs)


@pytest.fixture
async def microsoft(tenant: Tenant, tmp_path: Path) -> AsyncIterator[Intercepted]:
    proxy = Proxy(Routing(Registry.installed()), tenant.store, tenant.clock, confdir=tmp_path / "ca")
    async with proxy:
        proxy.mount(tenant.store, tenant.clock, {"microsoft": tenant.provider.app(tenant.store, tenant.clock)})
        yield Intercepted(proxy=proxy, trust=ssl.create_default_context(cafile=str(proxy.ca_bundle)))


async def token(http: httpx.AsyncClient, tenant: Tenant, scope: str, *, secret: str | None = None) -> str:
    """A client-credentials token, requested exactly as the services request it."""
    answered = await http.post(
        f"{LOGIN}/{tenant.directory.tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": tenant.directory.bot_app_id,
            "client_secret": secret or tenant.directory.bot_app_secret,
            "scope": scope,
        },
    )
    assert answered.status_code == 200, answered.text
    found = answered.json()["access_token"]
    assert isinstance(found, str)
    return found


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


@dataclass
class Received:
    activity: dict[str, Any]
    accepted: bool
    reason: str


@dataclass
class Bot:
    """The bot's messaging endpoint. `answer` decides its answer to an accepted `invoke`."""

    url: str
    received: list[Received] = field(default_factory=list)
    answer: Callable[[dict[str, Any]], dict[str, Any]] = lambda activity: {
        "statusCode": 200,
        "type": "application/vnd.microsoft.activity.message",
        "value": "Done.",
    }

    def accepted(self) -> list[dict[str, Any]]:
        return [r.activity for r in self.received if r.accepted]

    def target(self) -> InboundTarget:
        return InboundTarget(provider="microsoft", url=self.url)


async def validate(authorization: str, app_id: str, service_url: str, intercepted: Intercepted) -> str | None:
    """The bot's own check, re-created from its source: None when it accepts, else why it refuses."""
    if not authorization.startswith("Bearer "):
        return "no bearer token"
    presented = authorization[len("Bearer ") :].strip()
    async with intercepted.http() as http:
        metadata = (await http.get(BOT_METADATA)).json()
    jwks_uri = metadata["jwks_uri"]
    client = PyJWKClient(jwks_uri, cache_keys=True, ssl_context=intercepted.trust)
    try:
        key = await asyncio.to_thread(_signing_key, client, presented)
        claims = jwt.decode(
            presented,
            key,
            algorithms=["RS256"],
            audience=app_id,
            issuer=VALID_ISSUERS,
            options={"require": ["exp", "iss", "aud"]},
            leeway=300,
        )
    except jwt.PyJWTError as e:
        return f"{type(e).__name__}: {e}"
    claimed = claims["serviceurl"] if "serviceurl" in claims else ""
    if service_url and claimed.rstrip("/") != service_url.rstrip("/"):
        return "serviceurl does not match the activity"
    return None


def _signing_key(client: PyJWKClient, presented: str) -> Any:
    return client.get_signing_key_from_jwt(presented).key


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def bot(tenant: Tenant, microsoft: Intercepted, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Bot]:
    # PyJWKClient fetches with urllib, which takes its proxy from the environment, as in the bot's process.
    monkeypatch.setenv("https_proxy", microsoft.proxy.url)
    monkeypatch.setenv("HTTPS_PROXY", microsoft.proxy.url)
    port = _free_port()
    handle = Bot(url=f"http://127.0.0.1:{port}/api/v1/teams/messages")

    async def messages(request: Request) -> Response:
        activity = json.loads(await request.body())
        authorization = request.headers["authorization"] if "authorization" in request.headers else ""
        refused = await validate(authorization, tenant.directory.bot_app_id, activity["serviceUrl"], microsoft)
        handle.received.append(Received(activity=activity, accepted=refused is None, reason=refused or ""))
        if refused is not None:
            return Response(status_code=401)
        if activity["type"] == "invoke":
            return JSONResponse(handle.answer(activity))
        return Response(status_code=200)

    app = Starlette(routes=[Route("/api/v1/teams/messages", messages, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield handle
    finally:
        server.should_exit = True
        await serving


@dataclass
class Webhook:
    url: str
    validations: list[str] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
async def webhook() -> AsyncIterator[Webhook]:
    """The service's notification route: the validation handshake echoed as text, notifications kept."""
    port = _free_port()
    handle = Webhook(url=f"http://127.0.0.1:{port}/api/v1/webhook/sharepoint")

    async def receive(request: Request) -> Response:
        if "validationToken" in request.query_params:
            handle.validations.append(request.query_params["validationToken"])
            return PlainTextResponse(request.query_params["validationToken"])
        handle.notifications.append(json.loads(await request.body()))
        return Response(status_code=202)

    app = Starlette(routes=[Route("/api/v1/webhook/sharepoint", receive, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    yield handle
    server.should_exit = True
    await serving
