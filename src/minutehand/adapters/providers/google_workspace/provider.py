"""The Google Workspace provider: Drive v3, Docs v1, Slides v1, Gmail v1, Calendar v3, Google's sign-in, and what
the scenario seeds of each.

It pushes no message to the agent the way Slack does, so it is not `PushesEvents`. It is `LandsReplies`: a person's
reply to the agent's email lands in the agent's mailbox, and a guest's answer to its invitation on the event, at
their moment, where the agent finds them on its next read. It is `ChangesDocuments`: a person's change to a seeded
document (a `DocumentHappening`) lands at its moment. It is `NotifiesChanges`: when the agent has asked Drive to be
told of changes (`changes.watch`), Drive tells it, the way Drive's push notifications do.
"""

from __future__ import annotations

from minutehand.adapters.providers.google_workspace import calendar_wire, wire
from minutehand.adapters.providers.google_workspace.app import DriveApi, build_app
from minutehand.adapters.providers.google_workspace.calendars import CalendarApi, CalendarWorld
from minutehand.adapters.providers.google_workspace.gmail import GmailApi, MailWorld
from minutehand.adapters.providers.google_workspace.manifest import MANIFEST
from minutehand.adapters.providers.google_workspace.seed import WorkspaceSeed, seed, write_faults
from minutehand.adapters.providers.google_workspace.state import DriveWorld
from minutehand.domain.errors import Rendered
from minutehand.domain.people import PersonReply
from minutehand.domain.provider import Manifest, fault_fragment
from minutehand.domain.scenario import DocumentHappening, Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class GoogleWorkspaceProvider:
    manifest: Manifest = MANIFEST
    seed_model = WorkspaceSeed

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(DriveApi(world, clock), GmailApi(world, clock), CalendarApi(world, clock))

    def error(self, status: int, code: str, message: str) -> Rendered:
        return wire.error_answer(status, code, message)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed(scenario, world)

    def lands(self, reply: PersonReply, world: Store) -> bool:
        """Every reply on Google Workspace lands: it pushes no message to the agent."""
        del reply, world
        return True

    def heard(self, reply: PersonReply, world: Store, clock: Clock) -> bool:
        """Gmail and Calendar push nothing (`users.watch` and `events.watch` answer 501): the agent polls."""
        del reply, world, clock
        return False

    async def land(self, reply: PersonReply, world: Store, clock: Clock) -> None:
        """A reply to an email lands as the person's email in the asking mailbox; an answer to an invitation as the
        guest's response on the event."""
        held = world.get(reply.in_reply_to)
        if held is None:
            return
        if isinstance(calendar_wire.KEPT.validate_json(held.body), calendar_wire.StoredEvent):
            CalendarWorld(world).land(reply, clock)
        else:
            MailWorld(world).land(reply, clock)

    def change(self, happening: DocumentHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        DriveApi(world, clock).person_change(happening)

    def watched(self, world: Store, clock: Clock) -> bool:
        return bool(DriveApi(world, clock).watching())

    async def notify(self, world: Store, clock: Clock) -> None:
        await DriveApi(world, clock).notify()

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`WorkspaceSeed.faults`, on a world already open."""
        write_faults(
            DriveWorld(world),
            fault_fragment(WorkspaceSeed, faults, frozenset({"faults"})).faults,
            clock.now(),
            declared=True,
        )


def build() -> GoogleWorkspaceProvider:
    return GoogleWorkspaceProvider()
