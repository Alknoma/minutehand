"""The Slack provider: the Web API, the seeded workspace, and pushed message events."""

from __future__ import annotations

from minutehand.adapters.providers.slack import inbound
from minutehand.adapters.providers.slack.app import build_app
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.seed import seed
from minutehand.domain.people import InboundTarget, PersonMessage, PersonReply
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

    async def deliver(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        await inbound.deliver(reply, target, world, clock, secret=secret)

    async def say(
        self, message: PersonMessage, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        await inbound.say(message, target, world, clock, secret=secret)


def build() -> SlackProvider:
    """A `Provider` that also `PushesEvents`; the tests hold it to both protocols."""
    return SlackProvider()
