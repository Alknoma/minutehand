"""The Slack provider: the Web API and the seeded workspace."""

from __future__ import annotations

from minutehand.adapters.providers.slack.app import build_app
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.seed import seed
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class SlackProvider:
    manifest: Manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)


def build() -> SlackProvider:
    return SlackProvider()
