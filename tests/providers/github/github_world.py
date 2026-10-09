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
    SeedBranch,
    SeedChange,
    SeedComment,
    SeedCommit,
    SeedFile,
    SeedIssue,
    SeedLabel,
    SeedLineCommit,
    SeedOrganization,
    SeedPull,
    SeedRepository,
    SeedReview,
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
                message="Start the ledger",
                author="iris-calder",
                before=timedelta(days=30),
                paths=["README.md", "docs/guide.md", "assets/logo.png", "vendor/bundle.min.js"],
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
                commits=[
                    SeedCommit(
                        message="Start the notes", author="iris-calder", before=timedelta(0), paths=["README.md"]
                    )
                ],
            ),
            SeedRepository(
                owner="lanternworks",
                name="plans",
                private=True,
                collaborators=[wire.Collaborator(login="tomas-b", permission=wire.Permission.PULL)],
                files=[SeedFile(path="plan.md", text="# Plans\n\nretry the launch later\n")],
                commits=[
                    SeedCommit(message="Plan the launch", author="iris-calder", before=timedelta(0), paths=["plan.md"])
                ],
            ),
            SeedRepository(owner="iris-calder", name="empty"),
        ],
    )
    return base.model_copy(update=changes)


TIMEOUT = "PAYMENT_TIMEOUT = 60\nRETRY_LIMIT = 5\n"
"""`CONFIG` with the timeout raised: what the seeded pull request changes."""
TIMEOUT_NOTES = "# Timeouts\n\nThe payment timeout is 60 seconds.\n"


def tracker_ledger() -> SeedRepository:
    """The ledger with a tracker: labels, issues (one assigned to each person, one closed), a branch with commits of its
    own and the pull request opened from it, reviewed once and with a reviewer still asked for."""
    return ledger(
        labels=[
            SeedLabel(name="bug", color="d73a4a", description="Something isn't working"),
            SeedLabel(name="docs", color="0075ca"),
            SeedLabel(name="Needs triage", color="ededed"),
            SeedLabel(name="alpha", color="cfd3d7", default=True),
        ],
        issues=[
            SeedIssue(
                number=1,
                title="Retries wait too long",
                body="The doubling wait reaches a minute.\n\n- seen in `retry_with_backoff`",
                author="tomas-b",
                labels=["bug"],
                assignees=["iris-calder"],
                before=timedelta(days=4),
                comments=[
                    SeedComment(author="iris-calder", body="Looking at it.", before=timedelta(days=3)),
                    SeedComment(author="tomas-b", body="Thanks!", before=timedelta(days=2)),
                ],
            ),
            SeedIssue(
                number=2,
                title="Document the retry helper",
                author="iris-calder",
                labels=["docs"],
                state=wire.IssueState.CLOSED,
                state_reason=wire.StateReason.COMPLETED,
                before=timedelta(days=5),
                closed_before=timedelta(days=1),
                closed_by="tomas-b",
            ),
            SeedIssue(
                number=3,
                title="Checkout button label",
                author="iris-calder",
                assignees=["tomas-b"],
                before=timedelta(days=1),
            ),
        ],
        diverged_branches=[
            SeedBranch(
                name="notes",
                commits=[
                    SeedLineCommit(
                        message="Describe the notes",
                        author="iris-calder",
                        before=timedelta(hours=4, minutes=30),
                        changes=[SeedChange(path="docs/notes.md", text="# Notes\n\nA note.\n")],
                    )
                ],
            ),
            SeedBranch(
                name="timeout",
                commits=[
                    SeedLineCommit(
                        message="Raise the payment timeout",
                        author="tomas-b",
                        before=timedelta(hours=4),
                        changes=[
                            SeedChange(path="services/billing/config.py", text=TIMEOUT),
                            SeedChange(path="docs/timeouts.md", text=TIMEOUT_NOTES),
                        ],
                    )
                ],
            ),
        ],
        pulls=[
            SeedPull(
                number=4,
                title="Raise the payment timeout",
                body="Sixty seconds is what the processor allows.",
                author="tomas-b",
                head="timeout",
                assignees=["iris-calder"],
                requested_reviewers=["iris-calder"],
                before=timedelta(hours=3),
                comments=[SeedComment(author="iris-calder", body="On it.", before=timedelta(hours=2))],
                reviews=[
                    SeedReview(
                        author="outsider",
                        state=wire.ReviewState.COMMENTED,
                        body="Why sixty?",
                        before=timedelta(hours=1),
                    )
                ],
            )
        ],
    )


def tracker_seed() -> GitHubSeed:
    """The shared GitHub with the tracker in the ledger."""
    base = github_seed()
    return validated(base.model_copy(update={"repositories": [tracker_ledger(), *base.repositories[1:]]}))


def validated(seed: GitHubSeed) -> GitHubSeed:
    """A seed built by copying, which checks nothing, read through the seed's own validation."""
    return GitHubSeed.model_validate(seed.model_dump())


RETRY_TWICE = RETRY.replace("TIMEOUT_SECONDS = 30", "TIMEOUT_SECONDS = 60").replace("attempts=5", "attempts=7")
"""`RETRY` changed in two places, between which it keeps lines: a change git may align more than one way."""


def branch(name: str, *commits: tuple[str, str, timedelta, list[SeedChange]]) -> SeedBranch:
    return SeedBranch(
        name=name,
        commits=[
            SeedLineCommit(message=message, author=author, before=before, changes=changes)
            for message, author, before, changes in commits
        ],
    )


def pulls_seed() -> GitHubSeed:
    """The tracker's GitHub with more branches to make pull requests from: two commits that delete a file and add
    another, a change to a file the first pull request also changes, a change to a binary file, a change in two
    places, and, in the public notes, a branch to ask a merge of from a repository its author does not push to."""
    base = tracker_seed()
    ledger_repo = tracker_ledger()
    extra = [
        branch(
            "cleanup",
            ("Drop the guide", "iris-calder", timedelta(hours=3), [SeedChange(path="docs/guide.md", delete=True)]),
            (
                "Say where the guide went",
                "iris-calder",
                timedelta(hours=2),
                [SeedChange(path="docs/moved.md", text="Moved.\n")],
            ),
        ),
        branch(
            "retry-config",
            (
                "Allow more retries",
                "tomas-b",
                timedelta(hours=3),
                [SeedChange(path="services/billing/config.py", text="PAYMENT_TIMEOUT = 45\nRETRY_LIMIT = 7\n")],
            ),
        ),
        branch(
            "logo",
            (
                "New logo",
                "iris-calder",
                timedelta(hours=3),
                [SeedChange(path="assets/logo.png", base64_bytes=base64.b64encode(bytes(range(255, -1, -1))).decode())],
            ),
        ),
        branch(
            "edgy",
            (
                "Tune the retries",
                "tomas-b",
                timedelta(hours=3),
                [SeedChange(path="services/billing/retry.py", text=RETRY_TWICE)],
            ),
        ),
    ]
    ledger_repo = ledger_repo.model_copy(update={"diverged_branches": [*ledger_repo.diverged_branches, *extra]})
    notes = base.repositories[1].model_copy(
        update={
            "commits": [
                SeedCommit(
                    message="Start the notes", author="iris-calder", before=timedelta(hours=1), paths=["README.md"]
                )
            ],
            "diverged_branches": [
                branch(
                    "fix",
                    (
                        "Fix the notes",
                        "iris-calder",
                        timedelta(minutes=30),
                        [SeedChange(path="README.md", text="# Notes\n\nFixed.\n")],
                    ),
                )
            ],
        }
    )
    return validated(base.model_copy(update={"repositories": [ledger_repo, notes, *base.repositories[2:]]}))


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
