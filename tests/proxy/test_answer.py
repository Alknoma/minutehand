"""Claimed hosts are answered by their provider, through a real proxy, over real TLS."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy, ProxyRunning
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.agent import WakeReason
from minutehand.domain.checks import Effectiveness, Finding
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, EntityKind, Operation, RecordedCall, WorldEvent
from tests.proxy.support import PROVIDERS, START, client, exchanges, stored_bytes


class RecordingTelemetry:
    """Keeps what it is told, so a test can read it back."""

    def __init__(self) -> None:
        self.events: list[WorldEvent] = []

    def run_started(self, run_id: str, scenario: Scenario) -> None:
        raise AssertionError("the proxy never starts a run")

    def wake_started(self, wake: int, reason: WakeReason, now: datetime) -> None:
        raise AssertionError("the proxy never starts a wake")

    def recorded(self, event: WorldEvent) -> None:
        self.events.append(event)

    def captured(self, call: RecordedCall) -> None:
        raise AssertionError("the proxy hands captured calls to the run loop, which tells telemetry")

    def wake_ended(self, wake: int) -> None:
        raise AssertionError("the proxy never ends a wake")

    def found(self, finding: Finding) -> None:
        raise AssertionError("the proxy never reports a finding")

    def run_ended(self, record: RunRecord, effectiveness: Effectiveness | None) -> None:
        raise AssertionError("the proxy never ends a run")


async def test_port_zero_reports_the_bound_port(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca", port=0) as proxy:
        assert proxy.port > 0
        assert proxy.ca_cert.is_file()
        _, writer = await asyncio.open_connection(proxy.host, proxy.port)
        writer.close()


async def test_exact_host_is_answered_with_the_path_prefix_stripped(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            response = await http.get("https://ledger.example/api/v2/whoami")
    assert response.status_code == 200
    assert response.json()["path"] == "/whoami"
    assert response.json()["host"] == "ledger.example"
    [(first, last, exchange)] = exchanges(world_path)
    assert exchange.path == "/api/v2/whoami"
    assert exchange.host == "ledger.example"
    assert exchange.status == 200
    assert first > last  # a read that wrote nothing produced no event


async def test_a_provider_is_handed_the_path_decoded_so_a_colon_custom_method_routes(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            colon = await http.get("https://ledger.example/api/v2/entries/e1:archive")
            spaced = await http.get("https://ledger.example/api/v2/entries/two%20words:archive?x=1")
    assert colon.status_code == 200 and colon.json() == {"entry": "e1", "action": "archive"}
    assert spaced.status_code == 200 and spaced.json() == {"entry": "two words", "action": "archive"}


async def test_wildcard_host_is_answered_and_its_events_recorded(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    telemetry = RecordingTelemetry()
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca", telemetry=telemetry) as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            response = await http.post("https://acme.ledger.test/api/v2/entries", json={"text": "filed"})
    assert response.status_code == 201
    [event] = store.events()
    assert (event.actor, event.operation, event.entity.kind) == (Actor.AGENT, Operation.CREATE, EntityKind.RECORD)
    assert event.sim_time == START
    assert event.exchange is not None
    assert (event.exchange.host, event.exchange.path, event.exchange.status) == (
        "acme.ledger.test",
        "/api/v2/entries",
        201,
    )
    assert telemetry.events == [event]


async def test_wildcard_does_not_claim_its_own_apex_which_is_refused(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            response = await http.get("https://ledger.test/api/v2/whoami")
    assert response.status_code == 502


async def test_provider_module_is_imported_on_its_first_request(
    store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    ledger, tally = f"{PROVIDERS}.ledger.provider", f"{PROVIDERS}.tally.provider"
    for name in (ledger, tally):
        sys.modules.pop(name, None)
    registry = Registry()
    registry.discover(PROVIDERS)
    assert {m.key for m in registry.manifests} == {"ledger", "tally"}
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        assert ledger not in sys.modules
        async with client(proxy, proxy.ca_cert) as http:
            await http.get("https://nobody.invalid/")
            assert ledger not in sys.modules
            await http.get("https://ledger.example/api/v2/whoami")
            assert ledger in sys.modules
    assert tally not in sys.modules


async def test_exchange_keeps_traceparent_and_strips_credentials(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    secrets = {
        "header": "Bearer header-secret-1",
        "cookie": "session=cookie-secret-2",
        "api_key_header": "apikey-header-secret-3",
        "query": "query-secret-4",
        "body": "body-secret-5",
        "form": "form-secret-6",
    }
    headers = {
        "authorization": secrets["header"],
        "cookie": secrets["cookie"],
        "x-api-key": secrets["api_key_header"],
        "traceparent": traceparent,
    }
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            created = await http.post(
                f"https://ledger.example/api/v2/entries?token={secrets['query']}&n=1",
                json={"text": "hello", "api_key": secrets["body"]},
                headers=headers,
            )
            seen = await http.post(
                f"https://ledger.example/api/v2/whoami?token={secrets['query']}",
                content=f"token={secrets['form']}&channel=C1",
                headers={**headers, "content-type": "application/x-www-form-urlencoded"},
            )
            whoami = await http.get("https://ledger.example/api/v2/whoami", headers=headers)
    assert created.status_code == 201
    assert seen.status_code == 405  # the route is GET only; the call is recorded all the same
    assert whoami.json()["authorized"] is True  # the provider itself sees the credentials
    assert whoami.json()["access_token"] == "issued-by-ledger"

    recorded = [exchange for _, _, exchange in exchanges(world_path)]
    assert [e.traceparent for e in recorded] == [traceparent] * 3
    assert recorded[0].path == "/api/v2/entries?token=%5Bredacted%5D&n=1"
    assert recorded[0].request_body is not None
    assert json.loads(recorded[0].request_body) == {"text": "hello", "api_key": "[redacted]"}
    assert recorded[1].request_body == "token=%5Bredacted%5D&channel=C1"
    assert recorded[2].response_body is not None
    assert json.loads(recorded[2].response_body)["access_token"] == "[redacted]"
    on_disk = stored_bytes(world_path)
    for secret in [*secrets.values(), "issued-by-ledger"]:
        assert secret.split("=")[-1].encode() not in on_disk, secret


async def test_bodies_that_differ_only_in_a_redacted_secret_are_stored_once_and_no_secret_is_kept(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    """Redaction happens before a body is hashed: two long requests that differ only in their key share one
    stored body, and neither key is anywhere on disk, compressed bodies and snapshot pool included."""
    text = "A long entry the agent files twice. " * 40
    keys = ["first-body-secret-41", "second-body-secret-42"]
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            for n, key in enumerate(keys):
                created = await http.post(
                    f"https://ledger.example/api/v2/entries?token=query-secret-{n}",
                    json={"text": text, "api_key": key},
                )
                assert created.status_code == 201
    recorded = [exchange for _, _, exchange in exchanges(world_path)]
    assert recorded[0].request_body == recorded[1].request_body
    assert recorded[0].request_body is not None and json.loads(recorded[0].request_body)["api_key"] == "[redacted]"
    with sqlite3.connect(world_path) as db:
        assert db.execute("SELECT COUNT(DISTINCT request_ref), COUNT(request_ref) FROM exchange").fetchone() == (1, 2)
    on_disk = stored_bytes(world_path)
    assert text.encode() in on_disk, "the search reaches inside compressed bodies"
    for secret in [*keys, "query-secret-0", "query-secret-1"]:
        assert secret.encode() not in on_disk, secret


async def test_a_client_configured_only_by_environment_is_answered(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy") and k != "SSL_CERT_DIR"}
        env |= {"HTTPS_PROXY": proxy.url, "SSL_CERT_FILE": str(proxy.ca_cert)}
        program = "import httpx; r = httpx.get('https://ledger.example/api/v2/whoami'); print(r.status_code, r.text)"
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(child.communicate(), timeout=30)
    assert child.returncode == 0, err.decode()
    status, body = out.decode().split(" ", 1)
    assert status == "200"
    assert json.loads(body)["path"] == "/whoami"


async def test_a_second_proxy_while_one_runs_is_refused(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca"):
        with pytest.raises(ProxyRunning, match="already running"):
            async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca2"):
                pass
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as again:
        assert again.port > 0


async def test_a_mounted_run_records_into_its_own_store(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    other = SqliteStore(world_path, "second", clock)
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(other, clock, {})
        async with client(proxy, proxy.ca_cert) as http:
            await http.post("https://acme.ledger.test/api/v2/entries", json={"text": "filed"})
    assert store.events() == [] and len(other.events()) == 1


TALLY_HOST = "tally.test"
FILE = (
    b"PK\x03\x04\x14\x00\x06\x00" + bytes(range(256)) * 4
)  # what tally's /file answers; importing it would load tally
MISLABELLED = b'{"name": "Ren\xe9e", "bad": "\xff\xfe"}'


@pytest.mark.parametrize("path", ["/file", "/mislabel"], ids=["a .docx download", "json invalid in its charset"])
async def test_a_call_answered_with_bytes_that_are_not_text_is_recorded_with_exactly_those_bytes(
    path: str, registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    """A provider's answer that is not UTF-8 text reached the agent and was missing from the run: its body, read with
    lone surrogates, could not be serialised. Every call is recorded, and these bytes read back as they crossed."""
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            answered = await http.get(f"https://{TALLY_HOST}{path}")
            after = await http.get(f"https://{TALLY_HOST}/")

    assert answered.status_code == 200 and after.status_code == 200
    first, second = store.calls()
    assert first.exchange.path == path and first.exchange.response_body is None
    assert first.exchange.response_bytes == answered.content == (FILE if path == "/file" else MISLABELLED)
    assert second.exchange.response_body is not None and second.exchange.response_bytes is None
