"""The GitHub provider: the REST and GraphQL reads, and the GitHub a scenario starts with."""

from __future__ import annotations

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.app import build_app
from minutehand.adapters.providers.github.manifest import MANIFEST
from minutehand.adapters.providers.github.seed import GitHubSeed, github_seed, seed, write_faults, write_limits
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.domain.errors import Rendered
from minutehand.domain.provider import Manifest, fault_fragment
from minutehand.domain.scenario import Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class GitHubProvider:
    manifest: Manifest = MANIFEST
    seed_model = GitHubSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def error(self, status: int, code: str, message: str) -> Rendered:
        del code
        return wire.error_answer(status, message)

    def seed(self, scenario: Scenario, world: Store) -> None:
        """The scenario's own `GitHubSeed` (its `ProviderSeed` for `github`), its people the scenario's. The shared
        scenario names nothing GitHub holds, so a scenario without one has an empty GitHub."""
        seed(github_seed(scenario), scenario, world)

    def seed_with(self, given: GitHubSeed, scenario: Scenario, world: Store) -> None:
        """Seed a `GitHubSeed` given apart from the scenario, its people the scenario's."""
        seed(given, scenario, world)

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`GitHubSeed.faults` armed after those already armed, and `.limits` lowering a repository's tree or
        directory limits, on a world already open."""
        found = fault_fragment(GitHubSeed, faults, frozenset({"faults", "limits"}))
        github = GitHubWorld(world)
        write_faults(github, found.faults, declared=True)
        write_limits(github, found.limits)
        del clock


def build() -> GitHubProvider:
    """A `Provider` that `DeclaresFaults`: GitHub pushes nothing to the agent here, holds no tickets it answers,
    and books nothing."""
    return GitHubProvider()
