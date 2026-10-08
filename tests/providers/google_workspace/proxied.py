"""Google's own clients, in a process of their own, reaching the Drive provider through Minutehand's proxy.

The client process is configured by nothing but the environment `session.agent_environment` hands an agent:
`HTTPS_PROXY`, `NO_PROXY` and the CA bundle in every library's variable. It runs stock `googleapiclient` over
`httplib2` and stock `google-auth`, with production defaults: no `http=`, no `client_options`, the real
`https://oauth2.googleapis.com/token`. One more variable is the test's own: `PYTHONPATH` names `offline/`,
whose `sitecustomize` refuses any connection that is not to this machine, so a library that ignored the proxy
fails here instead of reaching Google.

A program talks to the test one JSON line at a time on stdout (`say(...)`) and waits for a line on stdin
(`wait()`), so the test can move the clock or change the world between two of its calls.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from mitmproxy import certs
from mitmproxy.options import CONF_BASENAME

from minutehand.adapters.providers.google_workspace.provider import GoogleWorkspaceProvider, build
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.proxy.trust import KEY_SIZE
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import (
    Access,
    AccessRole,
    AfterScript,
    DocumentKind,
    Person,
    Scenario,
    Scripted,
    SeededDocument,
    SharedSpace,
    SignIn,
)
from minutehand.domain.world import Exchange
from minutehand.session import Listen, agent_environment

START = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
REFRESH = "1//mara-refresh-token"
ROBOT = "robot@sim-project.iam.gserviceaccount.com"
OFFLINE = Path(__file__).parent / "offline"

SUPPLIERS = SeededDocument(
    provider="google_workspace",
    title="Supplier Shortlist",
    text="# Shortlist\nThree suppliers remain.\n- Acme\n- Globex\n",
    folder="Procurement/2026",
    shared_with=[Access(person="dov", role=AccessRole.READER)],
    modified_before_start=timedelta(days=3),
    modified_by="dov",
)

SCENARIO = Scenario(
    name="supplier_review",
    goal="Every supplier has a reviewed contract summary.",
    owner="mara",
    starts_at=START,
    people=[
        Person(key="mara", name="Mara Lindqvist", email="mara@example.com", reply=Scripted(then=AfterScript.SILENT)),
        Person(key="dov", name="Dov Aranha", email="dov@example.com", reply=Scripted(then=AfterScript.SILENT)),
        Person(key="rosa", name="Rosa Field", email="rosa@example.com", reply=Scripted(then=AfterScript.SILENT)),
    ],
    sign_ins=[
        SignIn(provider="google_workspace", credential=REFRESH, person="mara"),
        SignIn(provider="google_workspace", credential=ROBOT),
    ],
    spaces=[
        SharedSpace(
            provider="google_workspace",
            name="Partners",
            members=[Access(person="mara", role=AccessRole.ORGANIZER), Access(person="rosa", role=AccessRole.READER)],
        )
    ],
    documents=[
        SUPPLIERS,
        SeededDocument(
            provider="google_workspace",
            title="Rates",
            kind=DocumentKind.SPREADSHEET,
            rows=[["supplier", "rate"], ["Acme", "12"], ["Globex, Ltd", "14"]],
            folder="Procurement",
        ),
        SeededDocument(
            provider="google_workspace", title="Kickoff Deck", kind=DocumentKind.PRESENTATION, text="Kickoff\nWhy now"
        ),
        SeededDocument(provider="google_workspace", title="notes.txt", kind=DocumentKind.FILE, text="call Acme back\n"),
        SeededDocument(
            provider="google_workspace", title="Partner Plan", text="Partners sign by October.", space="Partners"
        ),
    ],
)

PRELUDE = f"""
import io, json, sys
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/drive", "openid",
          "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"]

def credentials():
    return Credentials(token=None, refresh_token={REFRESH!r}, token_uri="https://oauth2.googleapis.com/token",
                       client_id="1234.apps.googleusercontent.com", client_secret="client-secret", scopes=SCOPES)

def say(**found):
    print(json.dumps(found), flush=True)

def wait():
    return sys.stdin.readline().strip()

def refused(call):
    try:
        call()
    except HttpError as error:
        details = error.error_details
        return [error.resp.status, details[0]["reason"] if isinstance(details, list) and details else None]
    return None

creds = credentials()
drive = build("drive", "v3", credentials=creds, cache_discovery=False)
"""


@dataclass
class Google:
    """The proxy, the world behind it, and the provider for changes made beside the client."""

    proxy: Proxy
    store: SqliteStore
    clock: RunClock
    provider: GoogleWorkspaceProvider
    environment: dict[str, str]

    def exchanges(self) -> list[tuple[str | None, Exchange]]:
        return [(call.provider, call.exchange) for call in self.store.calls()]

    async def client(self, program: str) -> Client:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            PRELUDE + program,
            env=self.environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return Client(child)


class Client:
    def __init__(self, child: asyncio.subprocess.Process) -> None:
        self._child = child

    async def heard(self) -> dict[str, object]:
        assert self._child.stdout is not None
        line = await asyncio.wait_for(self._child.stdout.readline(), timeout=45)
        if not line:
            raise AssertionError(f"the client ended without answering: {await self.errors()}")
        found = json.loads(line)
        assert isinstance(found, dict)
        return found

    async def go(self) -> None:
        assert self._child.stdin is not None
        self._child.stdin.write(b"go\n")
        await self._child.stdin.drain()

    async def errors(self) -> str:
        assert self._child.stderr is not None
        await asyncio.wait_for(self._child.wait(), timeout=45)
        return (await self._child.stderr.read()).decode()

    async def finished(self) -> None:
        returned = await asyncio.wait_for(self._child.wait(), timeout=45)
        assert returned == 0, await self.errors()


def client_environment(proxy: Proxy) -> dict[str, str]:
    """What Minutehand hands an agent it starts, over a clean copy of this process's environment."""
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.lower().endswith("_proxy") and "CERT" not in name and "CA_BUNDLE" not in name
    }
    handed = agent_environment(Listen(), proxy.port, proxy.ca_bundle, {}, telemetry_port=None)
    return {**inherited, **handed, "PYTHONPATH": str(OFFLINE)}


def receiver_certificate(proxy: Proxy, directory: Path) -> tuple[Path, Path]:
    """A certificate for 127.0.0.1 signed by the run's CA, and its key: what an agent's HTTPS receiver serves so a
    Google push to it verifies, as a real receiver serves one a public CA signed. (certificate.pem, key.pem)"""
    store = certs.CertStore.from_store(proxy.ca_cert.parent, CONF_BASENAME, KEY_SIZE)
    entry = store.get_cert("127.0.0.1", [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "receiver.pem", directory / "receiver-key.pem"
    cert.write_bytes(entry.cert.to_pem())
    key.write_bytes(
        entry.privatekey.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return cert, key


@pytest.fixture
async def google(tmp_path: Path) -> AsyncIterator[Google]:
    async with serving(tmp_path, SCENARIO) as found:
        yield found


@asynccontextmanager
async def serving(tmp_path: Path, scenario: Scenario) -> AsyncIterator[Google]:
    """The proxy over a world seeded with `scenario`, with the Drive provider mounted on it."""
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    provider = build()
    provider.seed(scenario, store)
    registry = Registry()
    registry.discover("minutehand.adapters.providers")
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(store, clock, {"google_workspace": provider.app(store, clock)}, scenario=scenario)
        yield Google(proxy, store, clock, provider, client_environment(proxy))
