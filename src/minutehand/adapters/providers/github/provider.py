"""The GitHub provider: the REST and GraphQL reads, and the GitHub a scenario starts with."""

from __future__ import annotations

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.app import build_app
from minutehand.adapters.providers.github.hooks import Pusher
from minutehand.adapters.providers.github.manifest import MANIFEST
from minutehand.adapters.providers.github.seed import GitHubSeed, github_seed, seed, write_faults, write_limits
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.adapters.providers.github.transitions import GitHubTransitions
from minutehand.domain.errors import Rendered
from minutehand.domain.people import InboundTarget
from minutehand.domain.provider import Manifest, fault_fragment
from minutehand.domain.scenario import Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class GitHubProvider:
    manifest: Manifest = MANIFEST
    seed_model = GitHubSeed

    def __init__(self) -> None:
        self._listener: Pusher | None = None

    def listen(self, target: InboundTarget | None, secret: str | None) -> None:
        """`ListensForAgent`: where the agent takes GitHub's webhooks, so the ones its own writes set off reach it,
        signed with `secret` only where the world declares one for the target, as a person's move is."""
        self._listener = None if target is None else Pusher(target, secret if target.secret is not None else None)

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock, lambda: self._listener)

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

    def talking(self, target: InboundTarget | None, secret: str | None) -> GitHubTransitions:
        """`TalksToAgent`: what people do on GitHub (`transitions.py`), each move pushed as the webhooks GitHub sends to
        `target` when the agent declares one, signed with `secret` when the world declares one for it."""
        return GitHubTransitions(target, secret)


def build() -> GitHubProvider:
    """A `Provider` that `DeclaresFaults`, `TalksToAgent` (people's moves, pushed as webhooks) and `ListensForAgent`
    (the agent's own writes, pushed as webhooks): GitHub holds no tickets it answers and books nothing."""
    return GitHubProvider()
