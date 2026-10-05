"""The Microsoft provider: sign-in, the Bot Framework connector, Graph for Teams and files, and the people of the
tenant acting in Teams and on files.

It implements `Provider`, `PushesEvents` and `PushesInteractions`. What a person does to a file (edit, rename,
move, delete, share, hold it open) has no shared port yet: those are this provider's own methods, recorded as
actor PERSON and notified to Graph subscriptions exactly as an agent's change is.
"""

from __future__ import annotations

from minutehand.adapters.providers.microsoft import docx, seed, wire
from minutehand.adapters.providers.microsoft.app import build_app
from minutehand.adapters.providers.microsoft.graph_files import Files, mime_of
from minutehand.adapters.providers.microsoft.inbound import People
from minutehand.adapters.providers.microsoft.manifest import MANIFEST
from minutehand.adapters.providers.microsoft.state import DriveRecord, MicrosoftWorld, UserRecord
from minutehand.domain.people import InboundTarget, PersonMessage, PersonReply
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Happening, Scenario
from minutehand.domain.world import Actor, Operation, WorldEvent
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


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

    # ------------------------------------------------------------------ this provider's own: Teams

    async def install(self, person: str, conversation: str, target: InboundTarget, world: Store, clock: Clock) -> None:
        """`person` adds the bot to `conversation` (a connector conversation id) where it was not installed."""
        people = People(world, clock)
        found = people.world.conversation(conversation)
        if found is None:
            raise LookupError(f"no conversation {conversation}")
        await people.install(people.person(person), found, target)

    # ------------------------------------------------------------------ this provider's own: files

    async def edit_file(self, person: str, item: str, text: str, world: Store, clock: Clock) -> WorldEvent:
        """`person` saves new content into a file: a Word file's paragraphs, or a text file's bytes."""
        files, user, stored, drive = self._file(person, item, world, clock)
        content = docx.build(text) if mime_of(stored.item.name) == docx.DOCX else text.encode()
        updated = files.replace_content(stored, content, by=_by(user), actor=Actor.PERSON)
        await files.notify(drive, updated)
        return _last(world)

    async def rename_file(
        self, person: str, item: str, world: Store, clock: Clock, *, name: str | None = None, folder: str | None = None
    ) -> WorldEvent:
        """`person` renames a file, moves it into the folder with id `folder`, or both."""
        files, user, stored, drive = self._file(person, item, world, clock)
        updated = files.move(drive, stored, name=name, parent=folder, by=_by(user), actor=Actor.PERSON)
        await files.notify(drive, updated)
        return _last(world)

    async def delete_file(self, person: str, item: str, world: Store, clock: Clock) -> WorldEvent:
        files, user, stored, drive = self._file(person, item, world, clock)
        files.delete(stored, by=_by(user), actor=Actor.PERSON)
        await files.notify(drive, stored)
        return _last(world)

    async def share_file(
        self, person: str, item: str, with_email: str, roles: list[str], world: Store, clock: Clock
    ) -> WorldEvent:
        files, _, stored, drive = self._file(person, item, world, clock)
        files.share(stored, with_email, roles, actor=Actor.PERSON)
        await files.notify(drive, stored)
        return _last(world)

    def hold_file(self, person: str, item: str, world: Store, clock: Clock, *, held: bool) -> WorldEvent:
        """`person` opens a file for editing (`held`), so every write to it is refused 423, or closes it."""
        _, user, stored, _ = self._file(person, item, world, clock)
        mw = MicrosoftWorld(world)
        changed = stored.model_copy(update={"locked_by": user.user.mail if held else None})
        return mw.write_item(changed, operation=Operation.UPDATE, actor=Actor.PERSON)

    @staticmethod
    def _file(
        person: str, item: str, world: Store, clock: Clock
    ) -> tuple[Files, UserRecord, wire.StoredItem, DriveRecord]:
        mw = MicrosoftWorld(world)
        user = mw.person(person)
        if user is None:
            raise LookupError(f"{person} is not a user of the tenant")
        stored = mw.item(item)
        if stored is None:
            raise LookupError(f"no file {item}")
        drive = mw.drive(stored.item.parentReference.driveId)
        assert drive is not None
        return Files(mw, clock), user, stored, drive


def _by(user: UserRecord) -> wire.IdentitySet:
    return wire.IdentitySet(user=wire.Identity(id=user.user.id, displayName=user.user.displayName))


def _last(world: Store) -> WorldEvent:
    return world.events(since=world.head() - 1)[-1]


def build() -> MicrosoftProvider:
    """A `Provider` that also `PushesEvents` and `PushesInteractions`; the tests hold it to all three."""
    return MicrosoftProvider()
