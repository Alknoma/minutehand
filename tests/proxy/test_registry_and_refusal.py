"""Who claims a host, and what happens to a host nobody claims."""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from minutehand.adapters.proxy.policy import HostPolicy, Routing
from minutehand.adapters.proxy.registry import ENTRY_POINT_GROUP, ProviderConflict, Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.scenario import Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store
from tests.proxy.support import PROVIDERS, client, exchanges


class Loopback:
    """A provider registered in code rather than found on disk; it answers for one address."""

    def __init__(self, manifest: Manifest) -> None:
        self.manifest = manifest

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        async def hello(request: Request) -> PlainTextResponse:
            return PlainTextResponse("answered by loopback")

        return Starlette(routes=[Route("/", hello)])

    def seed(self, scenario: Scenario, world: Store) -> None:
        """Loopback keeps nothing a scenario could seed."""


class Listener:
    """A TCP port that counts who connects to it: the proof that nothing went upstream."""

    def __init__(self) -> None:
        self.connections = 0
        self.port = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> Listener:
        async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            self.connections += 1
            writer.close()

        self._server = await asyncio.start_server(accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


def test_two_providers_claiming_one_host_are_rejected(registry: Registry) -> None:
    with pytest.raises(ProviderConflict, match="ledger.example"):
        registry.register(Manifest(key="clash", tier=Tier.FINISHED, hosts=["ledger.example"]), lambda: Loopback(
            Manifest(key="clash", tier=Tier.FINISHED, hosts=["ledger.example"])))


def test_a_host_inside_another_providers_wildcard_is_rejected(registry: Registry) -> None:
    manifest = Manifest(key="inner", tier=Tier.FINISHED, hosts=["eu.ledger.test"])
    with pytest.raises(ProviderConflict, match=r"\*\.ledger\.test"):
        registry.register(manifest, lambda: Loopback(manifest))


def test_two_providers_with_one_key_are_rejected(registry: Registry) -> None:
    manifest = Manifest(key="ledger", tier=Tier.FINISHED, hosts=["elsewhere.example"])
    with pytest.raises(ProviderConflict, match="two providers are named 'ledger'"):
        registry.register(manifest, lambda: Loopback(manifest))


def test_a_model_host_a_provider_claims_is_rejected(registry: Registry) -> None:
    with pytest.raises(ProviderConflict, match="model host"):
        Routing(registry, model_hosts=["ledger.example"])


def test_an_entry_point_registers_its_package() -> None:
    registry = Registry()
    registry.load_entry_points([EntryPoint(name="tally", value=f"{PROVIDERS}.tally", group=ENTRY_POINT_GROUP)])
    manifest = registry.claimant("tally.test")
    assert manifest is not None and manifest.key == "tally"


def test_an_entry_point_named_for_another_provider_is_rejected() -> None:
    with pytest.raises(ProviderConflict, match="manifest is 'tally'"):
        Registry().load_entry_points([EntryPoint(name="ledger", value=f"{PROVIDERS}.tally", group=ENTRY_POINT_GROUP)])


def test_policy_for_each_kind_of_host(registry: Registry) -> None:
    routing = Routing(registry)
    assert routing.policy("ledger.example") is HostPolicy.ANSWER
    assert routing.policy("EU.Ledger.Test") is HostPolicy.ANSWER
    assert routing.policy("api.openai.com") is HostPolicy.TUNNEL
    assert routing.policy("api.anthropic.com") is HostPolicy.TUNNEL
    assert routing.policy("generativelanguage.googleapis.com") is HostPolicy.TUNNEL
    assert routing.policy("example.org") is HostPolicy.REFUSE


async def test_unclaimed_host_is_refused_and_recorded_without_contacting_it(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    async with Listener() as upstream, Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            refused = await http.get(f"https://127.0.0.1:{upstream.port}/v1/things?x=1",
                                     headers={"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"})
        await asyncio.sleep(0.05)
        assert upstream.connections == 0
    assert refused.status_code == 502
    assert refused.json() == {"error": "no provider claims this host", "host": "127.0.0.1"}
    [(first, last, exchange)] = exchanges(world_path)
    assert first > last
    assert (exchange.host, exchange.path, exchange.status) == ("127.0.0.1", "/v1/things?x=1", 502)
    assert exchange.traceparent == "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    assert exchange.response_body is not None and json.loads(exchange.response_body)["host"] == "127.0.0.1"


async def test_claimed_host_is_answered_without_contacting_it(
    store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    manifest = Manifest(key="loopback", tier=Tier.FINISHED, hosts=["127.0.0.1"])
    registry = Registry()
    registry.register(manifest, lambda: Loopback(manifest))
    async with Listener() as upstream, Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            answered = await http.get(f"https://127.0.0.1:{upstream.port}/")
        await asyncio.sleep(0.05)
        assert upstream.connections == 0
    assert answered.text == "answered by loopback"
