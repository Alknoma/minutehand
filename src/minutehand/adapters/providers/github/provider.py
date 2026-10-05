"""The GitHub provider: the REST and GraphQL reads, and the GitHub a scenario starts with."""

from __future__ import annotations

from minutehand.adapters.providers.github.app import build_app
from minutehand.adapters.providers.github.manifest import MANIFEST
from minutehand.adapters.providers.github.seed import GitHubSeed, seed
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class GitHubProvider:
    manifest: Manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def seed(self, scenario: Scenario, world: Store) -> None:
        """The shared scenario names nothing GitHub holds: its people become accounts only through a
        `GitHubSeed`, so a world seeded from the scenario alone has an empty GitHub."""
        seed(GitHubSeed(), scenario, world)

    def seed_with(self, given: GitHubSeed, scenario: Scenario, world: Store) -> None:
        """Seed this provider's own model, its people the scenario's. What `seed` will do once a scenario
        carries a provider's own seed."""
        seed(given, scenario, world)


def build() -> GitHubProvider:
    """A `Provider` and nothing more: GitHub pushes nothing to the agent here, holds no tickets it answers, and
    books nothing."""
    return GitHubProvider()
