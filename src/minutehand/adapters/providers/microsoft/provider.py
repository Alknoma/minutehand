"""The Microsoft provider: sign-in, the Bot Framework connector, Graph for Teams and files, and the people of the
tenant acting in Teams and on files.

It implements `Provider`, `PushesEvents` (a person installing the bot is `PersonAddsAgent`), `PushesInteractions`,
`ChangesDocuments`, `NotifiesChanges`, `DeclaresFaults`, `ChangesPeople` (a user removed, disabled or enabled again
by an administrator) and `MintsInboundCredentials` (the Bot Framework's token for an activity a test posts itself). A person's change to a seeded document (edit, rename, move, share, delete)
lands at its moment as that person, recorded as actor PERSON, and owes every live Graph subscription on the drive a
notification; `notify` sends what is owed, through the same `subscriptions.notify` an agent's own change goes
through. A file held open is not something a person does here: it is a fault the scenario declares
(`MicrosoftSeed.holds`).
"""

from __future__ import annotations

from minutehand.adapters.providers.microsoft import docx, seed, subscriptions, wire
from minutehand.adapters.providers.microsoft.app import build_app
from minutehand.adapters.providers.microsoft.graph_files import DRIVE_ITEM_TYPE, Files, mime_of
from minutehand.adapters.providers.microsoft.inbound import People, activity_token
from minutehand.adapters.providers.microsoft.manifest import MANIFEST
from minutehand.adapters.providers.microsoft.state import (
    USERS,
    DriveRecord,
    MicrosoftWorld,
    UserRecord,
    item_text,
    user_ref,
)
from minutehand.domain.people import (
    Header,
    InboundCredential,
    InboundCredentialAsk,
    InboundTarget,
    PersonMessage,
    PersonReply,
)
from minutehand.domain.provider import Manifest, PersonChange, fault_fragment
from minutehand.domain.scenario import (
    DocumentHappening,
    Edited,
    Happening,
    Moved,
    Person,
    Renamed,
    Scenario,
    Shared,
    Trashed,
)
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store


class MicrosoftProvider:
    manifest: Manifest = MANIFEST
    seed_model = seed.MicrosoftSeed

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
            files.share(stored, email, seed.ROLES[action.access.role], actor=Actor.PERSON)
        elif isinstance(action, Trashed):
            files.delete(stored, by=by, actor=Actor.PERSON)
        else:
            raise ValueError(f"a person cannot {action.kind} a Microsoft file; the scenario is refused at load")
        mw.owe(drive.drive.id, stored.item.id, actor=Actor.PERSON)

    # ------------------------------------------------------------------ DeclaresFaults

    def declare(self, faults: str, world: Store, clock: Clock) -> None:
        """`MicrosoftSeed.faults` and `.holds`, on a world already open, each counted from now."""
        found = fault_fragment(seed.MicrosoftSeed, faults, frozenset({"faults", "holds"}))
        mw = MicrosoftWorld(world)
        seed.write_faults(mw, found.faults, clock.now(), declared=True)
        seed.write_holds(mw, found.holds, clock.now())

    # ------------------------------------------------------------------ ChangesPeople

    def change_person(self, change: PersonChange, person: Person, world: Store, clock: Clock) -> None:
        """An administrator removes the person's user from the directory, disables it (`accountEnabled: false`:
        sign-in refused 50057, their 1:1 chat refuses the bot), or enables it again; actor SCENARIO."""
        mw = MicrosoftWorld(world)
        user = mw.person(person.key)
        if user is None:
            raise ValueError(f"{person.key} is no user of the tenant")
        email = user.user.mail or user.user.userPrincipalName
        if change is PersonChange.REMOVED:
            mw.remove(user_ref(user.user.id), actor=Actor.SCENARIO, parent=USERS)
            return
        enabled = change is PersonChange.REACTIVATED
        if (user.user.accountEnabled is not False) == enabled:
            raise ValueError(f"{email} is {'enabled' if enabled else 'disabled'} already")
        changed = user.model_copy(update={"user": user.user.model_copy(update={"accountEnabled": enabled})})
        mw.write_user(changed, actor=Actor.SCENARIO, text=f"{email} {'enabled' if enabled else 'disabled'}")
        del clock

    # ------------------------------------------------------------------ MintsInboundCredentials

    def credential(self, asked: InboundCredentialAsk, world: Store, clock: Clock, *, secret: str) -> InboundCredential:
        """The Bot Framework's bearer token for an activity a test posts to the bot itself, signed as every pushed
        activity's is: issued by `https://api.botframework.com` for the bot's app id, naming the activity's
        `serviceUrl`. `secret` is not used: the Bot Framework signs with its published key."""
        if not asked.service_url or not asked.audience:
            raise ValueError("a Bot Framework token names the activity's service_url and the bot's app id (audience)")
        apps = MicrosoftWorld(world).apps()
        app = next((a for a in apps if a.app_id == asked.audience), None)
        if app is None:
            known = ", ".join(a.app_id for a in apps) or "none"
            raise ValueError(f"{asked.audience} is no bot of this world; its bots are {known}")
        token = activity_token(app, service_url=asked.service_url)
        del secret, clock
        return InboundCredential(headers=[Header(name="Authorization", value=f"Bearer {token}")])

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
    """A `Provider` that also `PushesEvents`, `PushesInteractions`, `ChangesDocuments`, `NotifiesChanges`,
    `DeclaresFaults`, `ChangesPeople` and `MintsInboundCredentials`."""
    return MicrosoftProvider()
