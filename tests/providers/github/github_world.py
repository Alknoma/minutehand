"""A seeded GitHub over a real `SqliteStore` and `RunClock`, reached through the real proxy over TLS, with the
headers a code-reading client sends on every call."""

from __future__ import annotations

import base64
import json
import ssl
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.provider import GitHubProvider, build
from minutehand.adapters.providers.github.seed import (
    GitHubSeed,
    SeedCommit,
    SeedFile,
    SeedOrganization,
    SeedRepository,
    SeedToken,
    SeedUser,
)
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, Scenario

START = datetime(2026, 8, 24, 10, 50, 3, tzinfo=UTC)
API = "https://api.github.com"

IRIS = "ghp_iris0000000000000000000000000000000000"
NO_SCOPES = "ghp_irisnoscope00000000000000000000000000"
TOMAS = "github_pat_tomas_selects_only_the_ledger_0000000000000000"
"""A fine-grained token its issuer limited to one repository: a limit Minutehand does not enforce."""
OUTSIDER = "ghp_outsider000000000000000000000000000000"

HEADERS = {"Accept": "application/vnd.github.v3+json", "X-GitHub-Api-Version": "2022-11-28"}
"""What the client under study sends on every call, beside its `Authorization: Bearer`."""

RETRY = (
    '"""Retries for the billing service."""\n'
    "\n"
    "import time\n"
    "\n"
    "TIMEOUT_SECONDS = 30\n"
    "\n"
    "\n"
    "def retry_with_backoff(call, attempts=5):\n"
    '    """Call `call` until it succeeds, doubling the wait each time."""\n'
    "    for attempt in range(attempts):\n"
    "        try:\n"
    "            return call()\n"
    "        except ConnectionError:\n"
    "            time.sleep(2**attempt)\n"
    "    raise RuntimeError('checkout gave up')\n"
)
CONFIG = "PAYMENT_TIMEOUT = 45\nRETRY_LIMIT = 5\n"
APP = "export function checkoutButton(): string {\n  return 'Pay now';\n}\n"
README = "# Ledger\n\nThe billing and checkout services.\n"
GUIDE = "# Operating the ledger\n\nRestart billing with the retry_with_backoff helper in mind.\n"
LOGO = bytes(range(256)) * 4
HUGE = "// generated bundle\n" + "var a=1;\n" * 140_000
"""Over 1 MiB: the contents endpoint carries none of it, and code search never indexes it."""

PEOPLE = [
    Person(key="iris", name="Iris Calder", email="iris@example.com"),
    Person(key="tomas", name="Tomas Brandt", email="tomas@example.com"),
]

SCENARIO = Scenario(
    name="code_questions",
    goal="Answer where the billing retries are configured.",
    owner="iris",
    starts_at=START,
    people=PEOPLE,
)


def ledger(**changes: object) -> SeedRepository:
    base = SeedRepository(
        owner="lanternworks",
        name="ledger",
        private=True,
        description="Billing and checkout",
        topics=["billing", "payments"],
        license=wire.License(key="mit", name="MIT License", spdx_id="MIT"),
        branches=["release"],
        collaborators=[wire.Collaborator(login="tomas-b", permission=wire.Permission.PUSH)],
        files=[
            SeedFile(path="README.md", text=README),
            SeedFile(path="services/billing/retry.py", text=RETRY),
            SeedFile(path="services/billing/config.py", text=CONFIG),
            SeedFile(path="web/app.ts", text=APP),
            SeedFile(path="docs/guide.md", text=GUIDE),
            SeedFile(path="assets/logo.png", base64_bytes=base64.b64encode(LOGO).decode()),
            SeedFile(path="vendor/bundle.min.js", text=HUGE),
        ],
        commits=[
            SeedCommit(
                message="Start the ledger", author="iris-calder", before=timedelta(days=30), paths=["README.md"]
            ),
            SeedCommit(
                message="Retry billing calls\n\nWith a doubling wait.",
                author="tomas-b",
                before=timedelta(days=3),
                paths=["services/billing/retry.py", "services/billing/config.py"],
            ),
            SeedCommit(
                message="Add the checkout button", author="iris-calder", before=timedelta(hours=5), paths=["web/app.ts"]
            ),
        ],
        stargazers_count=4,
    )
    return base.model_copy(update=changes)


def github_seed(**changes: object) -> GitHubSeed:
    base = GitHubSeed(
        users=[
            SeedUser(login="iris-calder", person="iris"),
            SeedUser(login="tomas-b", person="tomas"),
            SeedUser(login="outsider", name="Visiting Contractor"),
        ],
        organizations=[SeedOrganization(login="lanternworks", name="Lantern Works", members=["iris-calder"])],
        tokens=[
            SeedToken(token=IRIS, kind=wire.TokenKind.CLASSIC, login="iris-calder"),
            SeedToken(token=NO_SCOPES, kind=wire.TokenKind.CLASSIC, login="iris-calder", scopes=[]),
            SeedToken(token=TOMAS, kind=wire.TokenKind.FINE_GRAINED, login="tomas-b"),
            SeedToken(token=OUTSIDER, kind=wire.TokenKind.CLASSIC, login="outsider"),
        ],
        repositories=[
            ledger(),
            SeedRepository(
                owner="iris-calder",
                name="notes",
                description="Public notes",
                files=[SeedFile(path="README.md", text="# Notes\n\nNothing about billing here.\n")],
                commits=[SeedCommit(message="Start the notes", author="iris-calder", before=timedelta(0))],
            ),
            SeedRepository(
                owner="lanternworks",
                name="plans",
                private=True,
                collaborators=[wire.Collaborator(login="tomas-b", permission=wire.Permission.PULL)],
                files=[SeedFile(path="plan.md", text="# Plans\n\nretry the launch later\n")],
                commits=[SeedCommit(message="Plan the launch", author="iris-calder", before=timedelta(0))],
            ),
            SeedRepository(owner="iris-calder", name="empty"),
        ],
    )
    return base.model_copy(update=changes)


@dataclass
class Hub:
    provider: GitHubProvider
    store: SqliteStore
    clock: RunClock
    proxy: Proxy
    path: Path

    def client(self, token: str | None = IRIS, **headers: str) -> httpx.AsyncClient:
        """httpx through the proxy, trusting only the proxy's CA, as a service handed its environment would."""
        sent = {**HEADERS, **headers}
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(
            base_url=API,
            headers=sent,
            proxy=self.proxy.url,
            verify=ssl.create_default_context(cafile=str(self.proxy.ca_cert)),
            trust_env=False,
        )


SeedFor = Callable[[], GitHubSeed]


@pytest.fixture
def seeded() -> GitHubSeed:
    """Override in a module to start from a different GitHub."""
    return github_seed()


@pytest.fixture
async def hub(tmp_path: Path, seeded: GitHubSeed) -> AsyncIterator[Hub]:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed_with(seeded, SCENARIO, store)
    async with Proxy(Routing(Registry.installed()), store, clock, confdir=tmp_path / "ca") as proxy:
        yield Hub(provider=provider, store=store, clock=clock, proxy=proxy, path=tmp_path / "world.db")


Json = dict[str, object]


def body(response: httpx.Response, status: int = 200) -> Json:
    assert response.status_code == status, response.text
    found = json.loads(response.text)
    assert isinstance(found, dict)
    return found


def listing(response: httpx.Response) -> list[Json]:
    assert response.status_code == 200, response.text
    found = json.loads(response.text)
    assert isinstance(found, list)
    return found


def refusal(response: httpx.Response, status: int, message: str) -> Json:
    found = body(response, status)
    assert found["message"] == message, found
    assert isinstance(found["documentation_url"], str)
    return found
