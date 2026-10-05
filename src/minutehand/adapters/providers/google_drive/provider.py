"""The Google Drive provider: Drive v3, Docs v1, Slides v1, Google's sign-in, and the seeded Drive.

It pushes no message to the agent the way Slack does, so it is not `PushesEvents`. It is `ChangesDocuments`:
a person's change to a seeded document (a `DocumentHappening`) lands at its moment. It is `NotifiesChanges`:
when the agent has asked Drive to be told of changes (`changes.watch`), Drive tells it, the way Drive's push
notifications do.
"""

from __future__ import annotations

from minutehand.adapters.providers.google_drive.app import DriveApi, build_app
from minutehand.adapters.providers.google_drive.manifest import MANIFEST
from minutehand.adapters.providers.google_drive.seed import DriveSeed, seed, write_faults
from minutehand.adapters.providers.google_drive.state import DriveWorld
from minutehand.domain.provider import Manifest, fault_fragment
from minutehand.domain.scenario import DocumentHappening, Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class GoogleDriveProvider:
    manifest: Manifest = MANIFEST
    seed_model = DriveSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(DriveApi(world, clock))

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    def change(self, happening: DocumentHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        DriveApi(world, clock).person_change(happening)

    def watched(self, world: Store, clock: Clock) -> bool:
        return bool(DriveApi(world, clock).watching())

    async def notify(self, world: Store, clock: Clock) -> None:
        await DriveApi(world, clock).notify()

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`DriveSeed.faults`, on a world already open."""
        write_faults(DriveWorld(world), fault_fragment(DriveSeed, faults, frozenset({"faults"})).faults, clock.now())


def build() -> GoogleDriveProvider:
    return GoogleDriveProvider()
