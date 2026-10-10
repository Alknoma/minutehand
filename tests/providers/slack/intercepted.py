"""Slack reached the way an agent reaches it: stock `slack_sdk` clients with no base URL, through the real proxy, over
TLS signed by the proxy's CA; and an agent's own Slack endpoint, which checks every request with `slack_sdk`'s own
`SignatureVerifier` before it reads it.

The proxy runs on the test's event loop, so the blocking `WebClient` is called from a worker thread (`off_loop`).
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qs

import httpx
import pytest
import uvicorn
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from slack_sdk.web import SlackResponse
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.domain.people import InboundTarget
from tests.providers.slack.slack_workspace import TOKEN, Workspace

T = TypeVar("T")
SECRET = "8f742231b10e8888abcd99yyyzzz85a5"


@dataclass
class Intercepted:
    proxy: Proxy
    trust: ssl.SSLContext

    def sync(self, token: str = TOKEN) -> WebClient:
        return WebClient(token=token, proxy=self.proxy.url, ssl=self.trust)

    def asynchronous(self, token: str = TOKEN) -> AsyncWebClient:
        return AsyncWebClient(token=token, proxy=self.proxy.url, ssl=self.trust)

    def http(self) -> httpx.AsyncClient:
        """A plain HTTP client, as the agent's own code posts a `response_url` or downloads a file."""
        return httpx.AsyncClient(proxy=self.proxy.url, verify=self.trust, trust_env=False, timeout=30.0)  # slow runners


@pytest.fixture
async def slack(workspace: Workspace, tmp_path: Path) -> AsyncIterator[Intercepted]:
    proxy = Proxy(Routing(Registry.installed()), workspace.store, workspace.clock, confdir=tmp_path / "ca")
    async with proxy:
        proxy.mount(
            workspace.store, workspace.clock, {"slack": workspace.provider.app(workspace.store, workspace.clock)}
        )
        yield Intercepted(proxy=proxy, trust=ssl.create_default_context(cafile=str(proxy.ca_bundle)))


def data(response: SlackResponse | AsyncSlackResponse) -> dict[str, Any]:
    """The JSON Slack answered with."""
    assert isinstance(response.data, dict)
    return response.data


async def off_loop(call: Callable[[], T]) -> T:
    return await asyncio.to_thread(call)


@dataclass
class Received:
    """One request the agent's endpoint took, after its signature verified."""

    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def payload(self) -> Any:
        """An interaction's `payload` form field, decoded."""
        return json.loads(parse_qs(self.body.decode())["payload"][0])

    @property
    def form(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.body.decode()).items()}


Answer = Callable[[Received], Awaitable[Response]]


async def _empty(_: Received) -> Response:
    return Response(status_code=200)


@dataclass
class AgentEndpoint:
    """The agent's Slack request URL. Every request is checked by `SignatureVerifier`; one that fails is answered
    401 and kept in `forged`, so a test sees a bad signature rather than a silent pass."""

    answer: Answer = _empty
    received: list[Received] = field(default_factory=list)
    forged: list[Received] = field(default_factory=list)
    status: int = 200
    url: str = ""

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        got = Received(headers=dict(request.headers), body=body)
        if not SignatureVerifier(SECRET).is_valid_request(body, dict(request.headers)):
            self.forged.append(got)
            return JSONResponse({"error": "bad signature"}, status_code=401)
        self.received.append(got)
        if self.status != 200:
            return Response(status_code=self.status)
        return await self.answer(got)

    def target(self, *, interactivity: str | None = None) -> InboundTarget:
        return InboundTarget(provider="slack", url=self.url, interactivity_url=interactivity)


@pytest.fixture
async def agent() -> AsyncIterator[AgentEndpoint]:
    endpoint = AgentEndpoint()
    app = Starlette(routes=[Route("/slack/events", endpoint.handle, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    endpoint.url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/slack/events"
    yield endpoint
    server.should_exit = True
    await serving
