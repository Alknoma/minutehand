"""GitHub lives in the store and nowhere else; the provider claims its host, meets its port and claims no other;
its seed refuses what GitHub could never hold."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.manifest import MANIFEST
from minutehand.adapters.providers.github.provider import build
from minutehand.adapters.providers.github.seed import GitHubSeed, SeedFile, SeedRepository, SeedToken, SeedUser
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.ports.provider import BooksWakes, Provider, PushesEvents
from minutehand.ports.transitions import ProvidesTransitions
from tests.providers.github.github_world import IRIS, SCENARIO, START, Hub, body, github_seed


def test_the_provider_is_found_by_its_directory_and_claims_api_github_com() -> None:
    registry = Registry.installed()
    claimant = registry.claimant("api.github.com")
    assert claimant is not None and claimant.key == "github"
    assert registry.claimant("github.com") is None
    assert registry.claimant("raw.githubusercontent.com") is None


def test_the_provider_meets_its_port_and_claims_no_other() -> None:
    provider: Provider = build()
    assert provider.manifest == MANIFEST
    for port in (PushesEvents, ProvidesTransitions, BooksWakes):
        assert not isinstance(provider, port)


def test_a_scenario_alone_seeds_an_empty_github(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "world.db", "root", RunClock(START))
    build().seed(SCENARIO, store)
    assert store.events() == []


async def test_github_survives_a_new_app_over_a_new_connection(hub: Hub) -> None:
    import httpx

    reopened = SqliteStore(hub.path, "root", RunClock(START))
    app = build().app(reopened, RunClock(START))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {IRIS}"},
    ) as http:
        assert body(await http.get("/repos/lanternworks/ledger"))["full_name"] == "lanternworks/ledger"


def test_a_token_is_never_written_down(hub: Hub) -> None:
    stored = b"".join(p.read_bytes() for p in hub.path.parent.glob(hub.path.name + "*"))
    assert IRIS.encode() not in stored


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"tokens": [SeedToken.model_construct(token="pat-1234", kind=wire.TokenKind.CLASSIC, login="iris-calder")]},
            "a classic token starts with 'ghp_'",
        ),
        ({"users": [SeedUser(login="iris-calder"), SeedUser(login="Iris-Calder")]}, "two accounts share a login"),
        ({"repositories": [SeedRepository(owner="nobody", name="x")]}, "no such owner: nobody"),
    ],
)
def test_a_seed_github_could_not_hold_is_rejected(change: dict[str, object], message: str) -> None:
    raw = github_seed().model_dump(mode="json", by_alias=True)
    for key, value in change.items():
        raw[key] = [v.model_dump(mode="json") for v in value]  # type: ignore[attr-defined]
    with pytest.raises(ValidationError, match=message):
        GitHubSeed.model_validate(raw)


def test_a_github_user_who_is_nobody_in_the_scenario_is_refused(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "world.db", "root", RunClock(START))
    with pytest.raises(ValueError, match="who is nobody in the scenario"):
        build().seed_with(GitHubSeed(users=[SeedUser(login="ghost", person="ghost")]), SCENARIO, store)


def test_a_repository_with_files_and_no_commit_is_rejected_naming_it() -> None:
    """GitHub holds no file outside a commit, and the provider makes none up: the seed must declare one."""
    with pytest.raises(ValueError, match=r"lanternworks/ghost: a repository with files has the commits that made them"):
        SeedRepository(owner="lanternworks", name="ghost", files=[SeedFile(path="a.md", text="a")])
