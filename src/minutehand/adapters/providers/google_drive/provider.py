"""The Google Drive provider: Drive v3, the Docs read, sign-in, and the seeded Drive.

Drive pushes nothing to the agent, so this is a `Provider` and nothing more: a
person's change to a document is a state change in the store that the agent finds
on its next read.
"""

from __future__ import annotations

from minutehand.adapters.providers.google_drive.app import build_app
from minutehand.adapters.providers.google_drive.manifest import MANIFEST
from minutehand.adapters.providers.google_drive.seed import seed
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class GoogleDriveProvider:
    manifest: Manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)


def build() -> GoogleDriveProvider:
    return GoogleDriveProvider()
