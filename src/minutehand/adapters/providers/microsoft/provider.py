"""The Microsoft provider: sign-in, the Bot Framework connector, Graph for Teams and files, and the people of the
tenant acting in Teams and on files.

It implements `Provider`, `PushesEvents` (a person installing the bot is `PersonAddsAgent`), `PushesInteractions`,
`ChangesDocuments` and `NotifiesChanges`. A person's change to a seeded document (edit, rename, move, share, delete)
lands at its moment as that person, recorded as actor PERSON, and owes every live Graph subscription on the drive a
notification; `notify` sends what is owed, through the same `subscriptions.notify` an agent's own change goes
through. A file held open is not something a person does here: it is a fault the scenario declares
(`MicrosoftSeed.holds`).
"""

from __future__ import annotations

from minutehand.adapters.providers.microsoft import docx, seed, subscriptions, wire
from minutehand.adapters.providers.microsoft.app import build_app
from minutehand.adapters.providers.microsoft.graph_files import DRIVE_ITEM_TYPE, Files, mime_of
from minutehand.adapters.providers.microsoft.inbound import People
from minutehand.adapters.providers.microsoft.manifest import MANIFEST
from minutehand.adapters.providers.microsoft.state import DriveRecord, MicrosoftWorld, UserRecord, item_text
from minutehand.domain.people import InboundTarget, PersonMessage, PersonReply
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import (
    AccessRole,
    DocumentHappening,
    Edited,
    Happening,
    Moved,
    Renamed,
    Scenario,
    Shared,
    Trashed,
)
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store

ROLES = {
    AccessRole.READER: ["read"],
    AccessRole.COMMENTER: ["read"],
    AccessRole.WRITER: ["write"],
    AccessRole.ORGANIZER: ["owner"],
}
"""Graph's roles for each access a scenario gives: it has no commenter, so a commenter reads."""


class MicrosoftProvider:
    manifest: Manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        return build_app(world, clock)

    def seed(self, scenario: Scenario, world: Store) -> None:
        seed.seed(scenario, MicrosoftWorld(world))

    # ------------------------------------------------------------------ PushesEvents

    async def deliver(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        """`secret` is not used: the Bot Framework signs with its published key, never a shared secret."""
        await People(world, clock).deliver(reply, target)

    async def say(
        self, message: PersonMessage, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        await People(world, clock).say(message, target)

    async def happen(
        self, happening: Happening, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        await People(world, clock).happen(happening, target)

    # ------------------------------------------------------------------ PushesInteractions

    async def press(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        await People(world, clock).press(reply, target)

    # ------------------------------------------------------------------ ChangesDocuments

    def change(self, happening: DocumentHappening, scenario: Scenario, world: Store, clock: Clock) -> None:
        """The person does the happening's action to the file its seeded document became. A file the agent deleted
        is not there, and nothing is written."""
        mw = MicrosoftWorld(world)
        item = mw.seeded(scenario.happening_document(happening).title)
        stored = mw.item(item) if item is not None else None
        if stored is None:
            return
        user = mw.person(happening.person)
        if user is None:
            raise LookupError(f"{happening.person} is not a user of the tenant")
        drive = mw.drive(stored.item.parentReference.driveId)
        assert drive is not None
        files, by, action = Files(mw, clock), _by(user), happening.action
        if isinstance(action, Edited):
            text = item_text(stored)
            joined = f"{text}\n{action.append}" if text else action.append
            content = docx.build(joined) if mime_of(stored.item.name) == docx.DOCX else joined.encode()
            files.replace_content(stored, content, by=by, actor=Actor.PERSON)
        elif isinstance(action, Renamed):
            ending = stored.item.name[stored.item.name.rfind(".") :] if "." in stored.item.name else ""
            name = action.to if "." in action.to else f"{action.to}{ending}"
            files.move(drive, stored, name=name, parent=None, by=by, actor=Actor.PERSON)
        elif isinstance(action, Moved):
            folder = _folder(files, mw, drive, action.folder, by)
            files.move(drive, stored, name=None, parent=folder.item.id, by=by, actor=Actor.PERSON)
        elif isinstance(action, Shared):
            target = mw.person(action.access.person)
            if target is None:
                raise LookupError(f"{action.access.person} is not a user of the tenant")
            email = target.user.mail or target.user.userPrincipalName
            files.share(stored, email, ROLES[action.access.role], actor=Actor.PERSON)
        elif isinstance(action, Trashed):
            files.delete(stored, by=by, actor=Actor.PERSON)
        else:
            raise ValueError(f"a person cannot {action.kind} a Microsoft file; the scenario is refused at load")
        mw.owe(drive.drive.id, stored.item.id, actor=Actor.PERSON)

    # ------------------------------------------------------------------ NotifiesChanges

    def watched(self, world: Store, clock: Clock) -> bool:
        """Whether any drive has a live subscription."""
        mw, now = MicrosoftWorld(world), clock.now()
        drives = {subscriptions.drive_watch(d.drive.id) for d in mw.drives()}
        return any(r.watches in drives and subscriptions.live(r, now) for r in mw.subscriptions())

    async def notify(self, world: Store, clock: Clock) -> None:
        """Send every live subscription on a drive a notification of each person's change it is owed."""
        mw = MicrosoftWorld(world)
        for ref, owed in mw.owed():
            await subscriptions.notify(
                mw,
                clock,
                subscriptions.drive_watch(owed.drive),
                change="updated",
                odata_type=DRIVE_ITEM_TYPE,
                resource=f"/drives/{owed.drive}/root",
                item=owed.item,
            )
            mw.paid(ref)


def _folder(files: Files, mw: MicrosoftWorld, drive: DriveRecord, path: str, by: wire.IdentitySet) -> wire.StoredItem:
    """The folder at `path` ('/'-separated) from the drive's root, each part made by the person if it is not there."""
    folder = mw.item(drive.root_id)
    assert folder is not None
    for name in (part for part in path.split("/") if part):
        found = next((c for c in mw.children(folder.item.id) if c.item.name.lower() == name.lower()), None)
        if found is None:
            found = files.new_item(drive, folder, name, folder=True, content=b"", by=by)
            mw.write_item(found, operation=Operation.CREATE, actor=Actor.PERSON)
        folder = found
    return folder


def _by(user: UserRecord) -> wire.IdentitySet:
    return wire.IdentitySet(user=wire.Identity(id=user.user.id, displayName=user.user.displayName))


def build() -> MicrosoftProvider:
    """A `Provider` that also `PushesEvents`, `PushesInteractions`, `ChangesDocuments` and `NotifiesChanges`."""
    return MicrosoftProvider()
