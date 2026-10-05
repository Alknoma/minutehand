"""Base-URL mode: a client given `http://<proxy>/_host/<real-host>` instead of a proxy is answered, captured and
recorded exactly as a client reaching `<real-host>` through the proxy is, and absolute URLs in what it is answered
lead back the same way."""

from __future__ import annotations

import json
import ssl
from pathlib import Path

import httpx

from minutehand.adapters.proxy.base_url import base_url, rewrite
from minutehand.adapters.proxy.capture import Capturing
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.outbound import Acknowledge, Answer, PassThrough
from minutehand.domain.world import AnsweredBy, CaptureMode
from tests.proxy.support import client, exchanges
from tests.proxy.upstream import Authority, make_authority, model_api


def direct(**kwargs: object) -> httpx.AsyncClient:
    """A client that is given no proxy at all."""
    return httpx.AsyncClient(trust_env=False, **kwargs)  # pyright: ignore[reportArgumentType]


async def test_a_base_url_call_is_answered_by_the_provider_and_recorded_as_a_call_to_the_real_host(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with direct(base_url=base_url(proxy.url, "acme.ledger.test")) as http:
            whoami = await http.get("/api/v2/whoami?x=1")
            created = await http.post("/api/v2/entries", json={"text": "filed"})
        async with client(proxy, proxy.ca_cert) as proxied:
            through_the_proxy = await proxied.get("https://acme.ledger.test/api/v2/whoami?x=1")
    assert whoami.status_code == 200 and whoami.json() == through_the_proxy.json()
    assert whoami.json()["host"] == "acme.ledger.test" and whoami.json()["path"] == "/whoami"
    assert created.status_code == 201
    [event] = store.events()
    assert event.exchange is not None
    assert (event.exchange.host, event.exchange.path) == ("acme.ledger.test", "/api/v2/entries")
    calls = [(e.method, e.host, e.path, e.status) for _, _, e in exchanges(world_path)]
    assert calls == [
        ("GET", "acme.ledger.test", "/api/v2/whoami?x=1", 200),
        ("POST", "acme.ledger.test", "/api/v2/entries", 201),
        ("GET", "acme.ledger.test", "/api/v2/whoami?x=1", 200),
    ]


async def test_a_base_url_over_https_is_answered_on_the_proxys_own_certificate(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        trust = ssl.create_default_context(cafile=str(proxy.ca_cert))
        async with direct(verify=trust) as http:
            answered = await http.get(f"https://localhost:{proxy.port}/_host/ledger.example/api/v2/whoami")
    assert answered.status_code == 200
    assert answered.json()["host"] == "ledger.example" and answered.json()["path"] == "/whoami"


async def test_a_base_url_that_names_no_host_is_refused_saying_how_one_is_written(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with direct() as http:
            refused = await http.get(f"{proxy.url}/_host/no_such host!/x")
    assert refused.status_code == 400
    assert "/_host/<host>[:<port>]/<path>" in refused.json()["error"]
    assert exchanges(world_path) == []


async def test_a_declared_host_is_captured_by_base_url_as_through_the_proxy(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    """Acknowledged: answered as declared and never sent; passed through: reaches the real host; both kept under the
    real host's name."""
    authority: Authority = make_authority(tmp_path / "upstream-ca")
    clock.begin_wake()
    declared = [
        Acknowledge(host="mail.example", answer=Answer(status=202, json_body={"id": "queued"})),
        PassThrough(host="localhost"),
    ]
    async with (
        model_api(authority, host="127.0.0.1") as real,
        Proxy(
            Routing(registry),
            store,
            clock,
            confdir=tmp_path / "ca",
            upstream_ca=authority.ca_cert,
            capturing=Capturing(declared),
        ) as proxy,
    ):
        async with direct() as http:
            sent = await http.post(f"{base_url(proxy.url, 'mail.example')}/v3/mail/send", json={"to": "sofia"})
            looked = await http.get(f"{base_url(proxy.url, f'localhost:{real.port}')}/venues?q=hall")
    assert (sent.status_code, sent.json()) == (202, {"id": "queued"})
    assert (looked.status_code, looked.json()) == (200, {"received": 1})
    assert [(r.method, r.path) for r in real.received] == [("GET", "/venues?q=hall")]
    assert real.headers[0]["host"] == f"localhost:{real.port}"
    kept = [e for _, _, e in exchanges(world_path)]
    assert [(e.host, e.path, e.captured.mode if e.captured else None) for e in kept] == [
        ("mail.example", "/v3/mail/send", CaptureMode.ACKNOWLEDGE),
        ("localhost", "/venues?q=hall", CaptureMode.PASS_THROUGH),
    ]
    assert kept[1].captured is not None and kept[1].captured.answered_by is AnsweredBy.REAL_HOST


async def test_a_proxied_call_whose_path_starts_like_a_base_url_is_not_taken_for_one(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            answered = await http.get("https://ledger.example/api/v2/_host/elsewhere.example/x")
    assert answered.status_code == 404  # the ledger's own answer for a path it does not serve


def test_only_urls_on_hosts_the_proxy_routes_are_rewritten_and_escaped_slashes_stay_escaped() -> None:
    def routable(authority: str, rest: bytes) -> bool:
        return authority == "api.example.com" and rest.startswith((b"/v1", b"\\/v1"))

    text = (
        b'{"next": "https://api.example.com/v1/items?page=2", "web": "https://api.example.com/home", '
        b'"escaped": "https:\\/\\/api.example.com\\/v1\\/items", "other": "https://elsewhere.example/v1"}'
    )
    rewritten = json.loads(rewrite(text, "http://127.0.0.1:9", routable))
    assert rewritten == {
        "next": "http://127.0.0.1:9/_host/api.example.com/v1/items?page=2",
        "web": "https://api.example.com/home",
        "escaped": "http://127.0.0.1:9/_host/api.example.com/v1/items",
        "other": "https://elsewhere.example/v1",
    }
