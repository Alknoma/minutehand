"""In `minutehand serve`, each personal access token is claimed by its world, and each world's GitHub is its own:
the credential routing finds a GitHub token in `Authorization: Bearer` with no change to it.

Each world's GitHub is seeded into its store directly, the way its provider will seed it from the scenario once
a scenario carries a provider's own seed."""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.provider import build
from minutehand.adapters.providers.github.seed import GitHubSeed, SeedFile, SeedRepository, SeedToken, SeedUser
from minutehand.domain.scenario import Seed
from minutehand.domain.world import Actor, Operation
from minutehand.serve import ServeOptions, serving

FIRST = "ghp_firstworld0000000000000000000000000000"
SECOND = "github_pat_secondworld_000000000000000000000000000000000"


def _github(token: str, kind: wire.TokenKind, repository: str) -> GitHubSeed:
    return GitHubSeed(
        users=[SeedUser(login="dev", name="A Developer")],
        tokens=[SeedToken(token=token, kind=kind, login="dev")],
        repositories=[
            SeedRepository(
                owner="dev", name=repository, private=True, files=[SeedFile(path="main.py", text="print('hi')\n")]
            )
        ],
    )


async def test_two_worlds_with_their_own_tokens_do_not_see_each_other_s_repositories(tmp_path: Path) -> None:
    seed = Seed.model_validate({"people": [{"key": "dev", "name": "A Developer", "email": "dev@example.com"}]})
    options = ServeOptions(proxy_port=0, control_port=0, telemetry_port=0, receive_telemetry=False)
    async with serving(tmp_path / "state", options) as running:
        first = running.standing.create(CreateWorld(seed=seed, claims=Claims(tokens=[FIRST])))
        second = running.standing.create(CreateWorld(seed=seed, claims=Claims(tokens=[SECOND])))
        scenario = seed.starting(first.standing.scenario.starts_at)
        build().seed_with(_github(FIRST, wire.TokenKind.CLASSIC, "alpha"), scenario, first.store)
        build().seed_with(_github(SECOND, wire.TokenKind.FINE_GRAINED, "beta"), scenario, second.store)

        verify = ssl.create_default_context(cafile=str(running.proxy.ca_cert))
        answers: dict[str, list[int]] = {}
        for token in (FIRST, SECOND):
            async with httpx.AsyncClient(
                base_url="https://api.github.com",
                proxy=running.proxy.url,
                verify=verify,
                trust_env=False,
                headers={"Authorization": f"Bearer {token}"},
            ) as http:
                answers[token] = [
                    (await http.get("/repos/dev/alpha")).status_code,
                    (await http.get("/repos/dev/beta")).status_code,
                ]

        assert answers == {FIRST: [200, 404], SECOND: [404, 200]}
        reads = [e.entity.external_id for e in first.store.events() if e.actor is Actor.AGENT]
        assert reads == ["repo/dev/alpha"]
        assert all(e.operation is Operation.READ for e in second.store.events() if e.actor is Actor.AGENT)
        assert [c.exchange.status for c in second.store.calls()] == [404, 200]
