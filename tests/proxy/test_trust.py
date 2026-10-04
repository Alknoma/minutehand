"""The one CA file an agent is handed verifies both a host the proxy answers and a real host it tunnels.

The "public" CA is a throwaway one (`upstream.py`) standing in for the roots certifi carries; the tunnelled
host is a local HTTPS server it signed. Nothing here reaches the public internet.
"""

from __future__ import annotations

from pathlib import Path

import certifi
import httpx
import pytest

from minutehand.adapters.proxy.policy import HostPolicy, Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.proxy.trust import authority as proxy_authority
from minutehand.adapters.proxy.trust import write_bundle
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from tests.proxy.support import client
from tests.proxy.upstream import Authority, make_authority, model_api

TUNNELLED = "localhost"


@pytest.fixture
def public(tmp_path: Path) -> Authority:
    return make_authority(tmp_path / "public-ca")


async def test_one_bundle_verifies_an_answered_host_and_a_tunnelled_one(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, public: Authority
) -> None:
    routing = Routing(registry, model_hosts=[TUNNELLED])
    assert routing.policy(TUNNELLED) is HostPolicy.TUNNEL
    roots = public.ca_cert.read_text()
    async with (
        model_api(public) as upstream,
        Proxy(routing, store, clock, confdir=tmp_path / "ca", public_roots=roots) as proxy,
    ):
        async with client(proxy, proxy.ca_bundle) as http:
            answered = await http.get("https://ledger.example/api/v2/whoami")
            tunnelled = await http.post(f"https://{TUNNELLED}:{upstream.port}/v1/call", content=b"{}")
    assert answered.status_code == 200
    assert tunnelled.status_code == 200
    assert len(upstream.received) == 1


async def test_the_proxys_ca_alone_fails_the_tunnelled_host(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, public: Authority
) -> None:
    """What every agent was handed before the bundle: a real host's certificate does not verify against it."""
    routing = Routing(registry, model_hosts=[TUNNELLED])
    async with (
        model_api(public) as upstream,
        Proxy(routing, store, clock, confdir=tmp_path / "ca", public_roots=public.ca_cert.read_text()) as proxy,
    ):
        async with client(proxy, proxy.ca_cert) as http:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await http.post(f"https://{TUNNELLED}:{upstream.port}/v1/call", content=b"{}")


def test_the_default_bundle_is_certifis_roots_then_the_proxys_ca(tmp_path: Path) -> None:
    bundle = write_bundle(tmp_path / "ca").read_text(encoding="utf-8")

    assert bundle.startswith(certifi.contents().rstrip("\n"))
    assert bundle.rstrip("\n").endswith(proxy_authority(tmp_path / "ca").read_text().rstrip("\n"))


def test_the_ca_is_made_once_and_kept(tmp_path: Path) -> None:
    first = proxy_authority(tmp_path / "ca").read_bytes()
    write_bundle(tmp_path / "ca")
    assert proxy_authority(tmp_path / "ca").read_bytes() == first
