"""A person declares the GitHub account they are (`Person.accounts`): its login, its id, its display name, and
whether its email is private. GitHub shows such a user as it shows any, its email null when private or absent."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.provider import build
from minutehand.adapters.providers.github.seed import GitHubSeed, SeedRepository, SeedToken, SeedUser
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, PersonAccount, Scenario
from tests.providers.github.github_world import API, HEADERS, START, body

JOHN = "ghp_john000000000000000000000000000000000"
ROBOT = "ghp_robot00000000000000000000000000000000"
MAYA = "ghp_maya000000000000000000000000000000000"


def scenario(*people: Person) -> Scenario:
    return Scenario(name="people", goal="Read code.", owner=people[0].key, starts_at=START, people=list(people))


JOHN_PERSON = Person(
    key="john",
    name="John Smith",
    email="john@example.com",
    accounts=[PersonAccount(provider="github", login="john-smith", id="583231", email_visible=False)],
)
ROBOT_PERSON = Person(
    key="release_bot",
    name="Release Robot",
    accounts=[PersonAccount(provider="github", login="release-robot", name="Releases")],
)
MAYA_PERSON = Person(key="maya", name="Maya Ortiz", email="maya@example.com")

SEED = GitHubSeed(
    users=[SeedUser(login="maya-o", person="maya")],
    tokens=[
        SeedToken(token=JOHN, kind=wire.TokenKind.CLASSIC, login="john-smith"),
        SeedToken(token=ROBOT, kind=wire.TokenKind.CLASSIC, login="release-robot"),
        SeedToken(token=MAYA, kind=wire.TokenKind.CLASSIC, login="maya-o"),
    ],
    repositories=[SeedRepository(owner="john-smith", name="tools")],
)


@pytest.fixture
async def proxy(tmp_path: Path) -> AsyncIterator[Proxy]:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    build().seed_with(SEED, scenario(JOHN_PERSON, ROBOT_PERSON, MAYA_PERSON), store)
    async with Proxy(Routing(Registry.installed()), store, clock, confdir=tmp_path / "ca") as found:
        yield found


def client(proxy: Proxy, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=API,
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
        proxy=proxy.url,
        verify=ssl.create_default_context(cafile=str(proxy.ca_cert)),
        trust_env=False,
    )


async def test_a_persons_account_entry_is_a_github_user_with_its_declared_login_and_id(proxy: Proxy) -> None:
    async with client(proxy, JOHN) as http:
        me = body(await http.get("/user"))
        tools = body(await http.get("/repos/john-smith/tools"))
    assert (me["login"], me["id"], me["name"]) == ("john-smith", 583231, "John Smith")
    assert tools["owner"]["id"] == 583231  # type: ignore[index]


async def test_a_private_email_reads_null(proxy: Proxy) -> None:
    async with client(proxy, JOHN) as http:
        assert body(await http.get("/user"))["email"] is None


async def test_a_person_with_no_email_reads_null_and_shows_the_entrys_name(proxy: Proxy) -> None:
    async with client(proxy, ROBOT) as http:
        me = body(await http.get("/user"))
    assert (me["login"], me["name"], me["email"]) == ("release-robot", "Releases", None)


async def test_a_seed_user_naming_a_person_without_an_entry_shows_their_public_email(proxy: Proxy) -> None:
    async with client(proxy, MAYA) as http:
        assert body(await http.get("/user"))["email"] == "maya@example.com"


def _seed(tmp_path: Path, given: GitHubSeed, *people: Person) -> SqliteStore:
    store = SqliteStore(tmp_path / "world.db", "root", RunClock(START))
    build().seed_with(given, scenario(*people), store)
    return store


def test_a_seed_user_and_a_persons_entry_naming_one_login_are_one_account(tmp_path: Path) -> None:
    store = _seed(tmp_path, GitHubSeed(users=[SeedUser(login="john-smith", person="john")]), JOHN_PERSON)
    assert len(store.events()) == 1


def test_a_github_entry_without_a_login_is_refused(tmp_path: Path) -> None:
    nameless = Person(key="ann", name="Ann", accounts=[PersonAccount(provider="github", id="7")])
    with pytest.raises(ValueError, match="names no login"):
        _seed(tmp_path, GitHubSeed(), nameless)


def test_a_login_github_does_not_allow_is_refused(tmp_path: Path) -> None:
    dotted = Person(key="ann", name="Ann", accounts=[PersonAccount(provider="github", login="ann.lee")])
    with pytest.raises(ValueError, match="not one GitHub allows"):
        _seed(tmp_path, GitHubSeed(), dotted)


def test_an_id_that_is_no_github_user_id_is_refused(tmp_path: Path) -> None:
    lettered = Person(key="ann", name="Ann", accounts=[PersonAccount(provider="github", login="ann", id="U123")])
    with pytest.raises(ValueError, match="not a GitHub user id"):
        _seed(tmp_path, GitHubSeed(), lettered)


def test_a_login_the_seed_gives_another_person_is_refused(tmp_path: Path) -> None:
    taken = GitHubSeed(users=[SeedUser(login="john-smith", person="maya")])
    with pytest.raises(ValueError, match="is maya in the GitHub seed and john's account"):
        _seed(tmp_path, taken, JOHN_PERSON, MAYA_PERSON)


def test_a_person_with_two_logins_is_refused(tmp_path: Path) -> None:
    twice = GitHubSeed(users=[SeedUser(login="johnny", person="john")])
    with pytest.raises(ValueError, match="is GitHub user johnny in the GitHub seed and john-smith"):
        _seed(tmp_path, twice, JOHN_PERSON)


def test_a_declared_id_another_account_has_is_refused(tmp_path: Path) -> None:
    from minutehand.adapters.providers.github.seed import number

    clash = Person(
        key="ann", name="Ann", accounts=[PersonAccount(provider="github", login="ann", id=str(number("maya-o")))]
    )
    with pytest.raises(ValueError, match="would both have the id"):
        _seed(tmp_path, GitHubSeed(users=[SeedUser(login="maya-o", person="maya")]), clash, MAYA_PERSON)
