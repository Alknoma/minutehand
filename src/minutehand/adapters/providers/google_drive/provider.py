"""The Google Drive provider: Drive v3, Docs v1, Slides v1, Google's sign-in, and the seeded Drive.

It pushes no message to the agent the way Slack does, so it is not `PushesEvents`. It is `ChangesDocuments`:
a person's change to a seeded document lands at its moment, and when the agent has asked Drive to be told of
changes (`changes.watch`), Drive tells it, the way Drive's push notifications do.
"""

from __future__ import annotations

from minutehand.adapters.providers.google_drive.app import DriveApi, build_app
from minutehand.adapters.providers.google_drive.manifest import MANIFEST
from minutehand.adapters.providers.google_drive.seed import seed
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import DocumentChange, Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class GoogleDriveProvider:
    manifest: Manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(DriveApi(world, clock))

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    def change(self, change: DocumentChange, world: Store, clock: Clock) -> None:
        DriveApi(world, clock).person_change(change)

    def watched(self, world: Store, clock: Clock) -> bool:
        return bool(DriveApi(world, clock).watching())

    async def notify(self, world: Store, clock: Clock) -> None:
        await DriveApi(world, clock).notify()


def build() -> GoogleDriveProvider:
    return GoogleDriveProvider()
