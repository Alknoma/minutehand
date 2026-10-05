"""The Microsoft tenant as entities in the run's store.

| Thing                     | `EntityKind` | external id                    | parent               |
|---------------------------|--------------|--------------------------------|----------------------|
| tenant                    | RECORD       | `tenant:<tid>`                 | `TENANTS`            |
| app registration (a bot)  | RECORD       | `app:<app id>`                 | `APPS`               |
| user                      | RECORD       | `user:<object id>`             | `USERS`              |
| team                      | RECORD       | `team:<group id>`              | `TEAMS`              |
| conversation (chat, channel) | CHANNEL   | its connector id               | `CONVERSATIONS`      |
| message (an activity)     | MESSAGE      | its activity id                | its conversation     |
| a person pressing a card  | RECORD       | `action:<invoke activity id>`  | its conversation     |
| site                      | RECORD       | `site:<site id>`               | `SITES`              |
| drive                     | RECORD       | `drive:<drive id>`             | `DRIVES`             |
| drive item                | DOCUMENT     | its item id                    | its folder's item id |
| permission on an item     | RECORD       | `perm:<item>:<permission id>`  | `perm:<item id>`     |
| upload session            | RECORD       | `upload:<session id>`          | `UPLOADS`            |
| copy in progress          | RECORD       | `copy:<operation id>`          | `COPIES`             |
| subscription              | RECORD       | `sub:<subscription id>`        | `SUBSCRIPTIONS`      |
| declared fault            | RECORD       | `fault:<position>`             | `FAULTS`             |
| a scenario's post key     | RECORD       | `post:<key>`                   | `POSTS`              |
| a seeded document's item  | RECORD       | `seeded:<n>`                   | `SEEDED`             |
| a file held open          | RECORD       | `hold:<position>`              | `HOLDS`              |
| a person's change owed to subscriptions | RECORD | `owed:<seq>`         | `OWED`               |

The prefixes keep the external ids of different records apart in one namespace; nothing reads a prefix to learn
what a record is: every read knows the kind it asked for from the parent it listed under.

A deleted drive item is deleted from the store, and a tombstone (`tomb:<item id>` under `tombstones:<drive>`) keeps
the item as Graph reports a removal, with its `deleted` facet, for `delta` to list.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from minutehand.adapters.providers.microsoft import docx, wire
from minutehand.adapters.providers.microsoft.manifest import MANIFEST
from minutehand.domain.absence import first_ask, placed
from minutehand.domain.scenario import Model, Scenario
from minutehand.domain.world import (
    Actor,
    Change,
    DocumentSnapshot,
    EntityKind,
    EntityRef,
    Operation,
    RecordSnapshot,
    Snapshot,
    Stored,
    WorldEvent,
)
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

TENANTS = "tenants"
APPS = "apps"
USERS = "users"
TEAMS = "teams"
CONVERSATIONS = "conversations"
SITES = "sites"
DRIVES = "drives"
UPLOADS = "uploads"
COPIES = "copies"
SUBSCRIPTIONS = "subscriptions"
PERMISSION_PARENT = "perm:{item}"
TOMBSTONES = "tombstones:{drive}"
FAULTS = "faults"
POSTS = "posts"
SEEDED = "seeded"
HOLDS = "holds"
OWED = "owed"

SERVICE_URL = "https://smba.trafficmanager.net/teams/"
"""The connector every activity names as its `serviceUrl`; a bot sends its answers there."""
CONNECTOR_HOST = "smba.trafficmanager.net"
GRAPH = "https://graph.microsoft.com/v1.0"

_SCAN = 1000
_NAMESPACE = uuid.UUID("6f1d3b0e-2c49-4d7a-9a6e-1b4a8f0c5d21")


def derived_uuid(*parts: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, "\x1f".join(parts)))


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class Directory(Model):
    """Who a scenario's tenant is: every id a caller is configured with, derived from the scenario's name, so a
    test or a service's environment can name them before the world exists (`directory_of`)."""

    tenant_id: str
    tenant_domain: str
    sharepoint_host: str
    bot_app_id: str
    bot_app_secret: str
    bot_name: str
    team_id: str
    general_channel_id: str


def directory_of(scenario: Scenario) -> Directory:
    name = scenario.name
    slug = name.replace("_", "")[:20] or "tenant"
    team = derived_uuid(name, "team")
    return Directory(
        tenant_id=derived_uuid(name, "tenant"),
        tenant_domain=f"{slug}.onmicrosoft.com",
        sharepoint_host=f"{slug}.sharepoint.com",
        bot_app_id=derived_uuid(name, "bot"),
        bot_app_secret="mhs~" + _digest(name, "bot secret")[:36],
        bot_name="Agent",
        team_id=team,
        general_channel_id=f"19:{_digest(name, 'general')[:32]}@thread.tacv2",
    )


class TenantRecord(Model):
    id: str
    domain: str
    sharepoint_host: str
    display_name: str


class AppRecord(Model):
    app_id: str
    secret: str
    display_name: str
    tenant_id: str


class AwayRecord(Model):
    """A stretch a person is away, as the tenant shows it: out of office, with their automatic reply. `starts` is
    known at seeding for an absence from the start; one that begins on the agent's first message to them is
    anchored when the store shows that message (`MicrosoftWorld.away`)."""

    starts: datetime | None = Field(
        default=None, description="None: from the agent's first message to them, plus `starts_after`"
    )
    starts_after: timedelta = timedelta(0)
    lasts: timedelta
    reason: str | None = None


class UserRecord(Model):
    user: wire.GraphUser
    tenant_id: str
    person_key: str | None = None
    absences: list[AwayRecord] | None = None

    @property
    def mri(self) -> str:
        return mri_of(self.user.id)


def mri_of(object_id: str) -> str:
    """The Teams member id of a user: what the connector calls them in `from.id` and `members[].id`."""
    return "29:" + _digest("mri", object_id)[:43]


def bot_mri(app_id: str) -> str:
    return f"28:{app_id}"


class TeamRecord(Model):
    id: str
    display_name: str
    tenant_id: str
    general_channel_id: str
    members: list[str]


class ConversationRecord(Model):
    """A conversation the connector and Graph both know: a 1:1 chat, a group chat or a team's channel."""

    id: str
    graph_id: str
    type: wire.ConversationType
    tenant_id: str
    display_name: str | None = None
    team_id: str | None = None
    members: list[str]
    bot_installed: bool
    created: str


class ActionRecord(Model):
    """A person pressed a button or submitted an Adaptive Card, and what the bot answered."""

    activity: wire.Activity
    status: int
    answer: str


class SeededRecord(Model):
    """Which drive item a scenario's seeded document became, by its title."""

    title: str
    item: str


class OwedRecord(Model):
    """A person changed an item in a drive: every live subscription on the drive is owed a notification of it."""

    drive: str
    item: str


class PostRecord(Model):
    """What a scenario's post key names: the activity it became."""

    key: str
    activity_id: str


class SiteRecord(Model):
    site: wire.Site
    drive_id: str


class DriveRecord(Model):
    drive: wire.Drive
    root_id: str
    site_id: str | None = None
    owner_id: str | None = None


class CopyRecord(Model):
    id: str
    status: wire.AsyncOperationStatus


class SubscriptionRecord(Model):
    subscription: wire.Subscription
    tenant_id: str
    watches: str = Field(description="What a change must touch to be notified: a drive, or a conversation")


def ref(kind: EntityKind, external_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=kind, external_id=external_id)


def tenant_ref(tid: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"tenant:{tid}")


def app_ref(app_id: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"app:{app_id}")


def user_ref(oid: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"user:{oid}")


def team_ref(team: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"team:{team}")


def conversation_ref(conversation: str) -> EntityRef:
    return ref(EntityKind.CHANNEL, conversation)


def message_ref(activity: str) -> EntityRef:
    return ref(EntityKind.MESSAGE, activity)


def action_ref(activity: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"action:{activity}")


def site_ref(site: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"site:{site}")


def drive_ref(drive: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"drive:{drive}")


def item_ref(item: str) -> EntityRef:
    return ref(EntityKind.DOCUMENT, item)


def permission_ref(item: str, permission: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"perm:{item}:{permission}")


def upload_ref(session: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"upload:{session}")


def copy_ref(operation: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"copy:{operation}")


def tombstone_ref(item: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"tomb:{item}")


def fault_ref(position: int) -> EntityRef:
    return ref(EntityKind.RECORD, f"fault:{position}")


def post_ref(key: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"post:{key}")


def reaction_ref(activity: str, oid: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"reaction:{activity}:{oid}")


def subscription_ref(subscription: str) -> EntityRef:
    return ref(EntityKind.RECORD, f"sub:{subscription}")


def item_text(stored: wire.StoredItem) -> str:
    """What can be read of a file as text: a Word document's paragraphs, a text file's bytes; nothing else."""
    if stored.item.file is None:
        return ""
    content = wire.content_of(stored)
    if stored.item.file.mimeType == docx.DOCX:
        return docx.text_of(content) or ""
    if stored.item.file.mimeType.startswith("text/"):
        return content.decode("utf-8", errors="replace")
    return ""


def graph_time(at: datetime) -> str:
    """Graph's timestamps: UTC, milliseconds, `Z`."""
    utc = at.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


class MicrosoftWorld:
    """Typed reads and writes of one run's Microsoft entities."""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ generic

    def next_seq(self) -> int:
        return self.store.head() + 1

    def _get(self, model: type[wire.M], entity: EntityRef, parent: str | None = None) -> wire.M | None:
        stored = self.store.get(entity)
        if stored is None or (parent is not None and stored.parent != parent):
            return None
        return wire.parse(model, stored.body)

    def _pages(self, kind: EntityKind, parent: str) -> Iterator[Stored]:
        after: str | None = None
        while True:
            page = self.store.children(MANIFEST.key, kind, parent, after=after, limit=_SCAN)
            yield from page
            if len(page) < _SCAN:
                return
            after = page[-1].entity.external_id

    def _all(self, model: type[wire.M], kind: EntityKind, parent: str) -> list[wire.M]:
        return [wire.parse(model, s.body) for s in self._pages(kind, parent)]

    def write(
        self,
        entity: EntityRef,
        body: BaseModel,
        *,
        operation: Operation,
        actor: Actor,
        parent: str | None,
        after: Snapshot | None = None,
    ) -> WorldEvent:
        return self.store.apply(
            Change(entity=entity, operation=operation, actor=actor, body=wire.dump(body), parent=parent, after=after)
        )

    def remove(self, entity: EntityRef, *, actor: Actor, parent: str | None) -> WorldEvent:
        return self.store.apply(Change(entity=entity, operation=Operation.DELETE, actor=actor, parent=parent))

    def saw(self, entity: EntityRef, operation: Operation, actor: Actor = Actor.AGENT) -> WorldEvent:
        return self.store.apply(Change(entity=entity, operation=operation, actor=actor))

    # ------------------------------------------------------------------ directory

    def tenant(self, tid: str) -> TenantRecord | None:
        return self._get(TenantRecord, tenant_ref(tid), TENANTS)

    def tenants(self) -> list[TenantRecord]:
        return self._all(TenantRecord, EntityKind.RECORD, TENANTS)

    def app(self, app_id: str) -> AppRecord | None:
        return self._get(AppRecord, app_ref(app_id), APPS)

    def apps(self) -> list[AppRecord]:
        return self._all(AppRecord, EntityKind.RECORD, APPS)

    def user(self, oid: str) -> UserRecord | None:
        return self._get(UserRecord, user_ref(oid), USERS)

    def users(self) -> list[UserRecord]:
        return self._all(UserRecord, EntityKind.RECORD, USERS)

    def user_by(self, key: str) -> UserRecord | None:
        """A user by object id, user principal name or mail, as Graph's `/users/{id | userPrincipalName}`."""
        found = self.user(key)
        if found is not None:
            return found
        wanted = key.lower()
        return next(
            (
                u
                for u in self.users()
                if u.user.userPrincipalName.lower() == wanted or (u.user.mail or "").lower() == wanted
            ),
            None,
        )

    def user_by_mri(self, mri: str) -> UserRecord | None:
        return next((u for u in self.users() if u.mri == mri), None)

    def person(self, key: str) -> UserRecord | None:
        return next((u for u in self.users() if u.person_key == key), None)

    # ------------------------------------------------------------------ teams and conversations

    def team(self, team: str) -> TeamRecord | None:
        return self._get(TeamRecord, team_ref(team), TEAMS)

    def teams(self) -> list[TeamRecord]:
        return self._all(TeamRecord, EntityKind.RECORD, TEAMS)

    def team_by_thread(self, thread: str) -> TeamRecord | None:
        return next((t for t in self.teams() if t.general_channel_id == thread), None)

    def conversation(self, conversation: str) -> ConversationRecord | None:
        return self._get(ConversationRecord, conversation_ref(conversation), CONVERSATIONS)

    def conversations(self) -> list[ConversationRecord]:
        return self._all(ConversationRecord, EntityKind.CHANNEL, CONVERSATIONS)

    def conversation_by_graph_id(self, graph_id: str) -> ConversationRecord | None:
        return next((c for c in self.conversations() if c.graph_id == graph_id), None)

    def personal_with(self, oid: str, tenant_id: str) -> ConversationRecord | None:
        return next(
            (
                c
                for c in self.conversations()
                if c.type is wire.ConversationType.PERSONAL and c.members == [oid] and c.tenant_id == tenant_id
            ),
            None,
        )

    def channels_of(self, team: str) -> list[ConversationRecord]:
        return [c for c in self.conversations() if c.type is wire.ConversationType.CHANNEL and c.team_id == team]

    def write_conversation(self, conversation: ConversationRecord, *, operation: Operation, actor: Actor) -> None:
        self.write(
            conversation_ref(conversation.id), conversation, operation=operation, actor=actor, parent=CONVERSATIONS
        )

    # ------------------------------------------------------------------ messages

    def message(self, activity: str) -> tuple[str, wire.Activity] | None:
        """An activity and the conversation it is in."""
        stored = self.store.get(message_ref(activity))
        if stored is None or stored.parent is None:
            return None
        return stored.parent, wire.parse(wire.Activity, stored.body)

    def messages(self, conversation: str) -> list[wire.Activity]:
        """Every live message in a conversation, oldest first: activity ids are ordered by creation."""
        return self._all(wire.Activity, EntityKind.MESSAGE, conversation)

    def next_activity_id(self, clock: Clock) -> str:
        """Teams' message id: the moment in milliseconds, here the simulated second and the next event's seq, so two
        messages in one instant are still distinct and ordered."""
        return f"{int(clock.now().timestamp())}{self.next_seq():06d}"

    # ------------------------------------------------------------------ files

    def site(self, site: str) -> SiteRecord | None:
        return self._get(SiteRecord, site_ref(site), SITES)

    def sites(self) -> list[SiteRecord]:
        return self._all(SiteRecord, EntityKind.RECORD, SITES)

    def drive(self, drive: str) -> DriveRecord | None:
        return self._get(DriveRecord, drive_ref(drive), DRIVES)

    def drives(self) -> list[DriveRecord]:
        return self._all(DriveRecord, EntityKind.RECORD, DRIVES)

    def item(self, item: str) -> wire.StoredItem | None:
        """A live item: one whose last version is not marked deleted."""
        found = self._get(wire.StoredItem, item_ref(item))
        return None if found is None or found.item.deleted is not None else found

    def item_any(self, item: str) -> wire.StoredItem | None:
        return self._get(wire.StoredItem, item_ref(item))

    def children(self, folder: str) -> list[wire.StoredItem]:
        found = self._all(wire.StoredItem, EntityKind.DOCUMENT, folder)
        return [s for s in found if s.item.deleted is None]

    def item_history(self, item: str) -> list[Stored]:
        return self.store.versions(item_ref(item))

    def write_item(self, stored: wire.StoredItem, *, operation: Operation, actor: Actor) -> WorldEvent:
        snapshot = DocumentSnapshot(
            title=stored.item.name,
            mime_type=stored.item.file.mimeType if stored.item.file is not None else None,
            text=item_text(stored) or None,
            last_edited_by=self._editor(stored.item.lastModifiedBy),
            last_edited_at=datetime.fromisoformat(stored.item.lastModifiedDateTime.replace("Z", "+00:00")),
            owner=self._editor(stored.item.createdBy),
            space=self._space(stored.item.parentReference.driveId),
        )
        return self.write(
            item_ref(stored.item.id),
            stored,
            operation=operation,
            actor=actor,
            parent=stored.item.parentReference.id,
            after=snapshot,
        )

    def _editor(self, by: wire.IdentitySet) -> str | None:
        """Who last changed an item as every document provider names them: a person's email, or the app's name."""
        if by.user is not None:
            found = self.user(by.user.id)
            return (found.user.mail if found is not None else None) or by.user.displayName
        if by.application is None:
            return None
        app = self.app(by.application.id)
        return by.application.displayName or (app.display_name if app is not None else None)

    def _space(self, drive_id: str) -> str | None:
        """The shared place a drive is: its site's name for a team's library; None for a person's own OneDrive."""
        drive = self.drive(drive_id)
        if drive is None or drive.site_id is None:
            return None
        site = self.site(drive.site_id)
        return site.site.displayName if site is not None else drive.drive.name

    def write_user(self, user: UserRecord, *, actor: Actor, text: str) -> WorldEvent:
        """A user changed while the world is open, as an administrator changes them."""
        return self.write(
            user_ref(user.user.id),
            user,
            operation=Operation.UPDATE,
            actor=actor,
            parent=USERS,
            after=RecordSnapshot(resource="user", text=text),
        )

    def away(self, user: UserRecord, now: datetime) -> tuple[datetime, datetime, AwayRecord] | None:
        """The stretch `user` is away at `now`, or else the next one already known, as (start, end, absence): an
        absence from the start is known from seeding; one from the agent's first message to them is known once
        the store holds that message, in any provider."""
        if not user.absences:
            return None
        email = user.user.mail
        asked = (
            first_ask(email, self.store.events())
            if email is not None and any(a.starts is None for a in user.absences)
            else None
        )
        known: list[tuple[datetime, datetime, AwayRecord]] = []
        for absence in user.absences:
            span = placed(
                from_start=absence.starts, asked=asked, starts_after=absence.starts_after, lasts=absence.lasts
            )
            if span is not None:
                known.append((span[0], span[1], absence))
        current = next((k for k in known if k[0] <= now < k[1]), None)
        if current is not None:
            return current
        return min((k for k in known if k[0] > now), key=lambda k: k[0], default=None)

    def permissions(self, item: str) -> list[wire.Permission]:
        return self._all(wire.Permission, EntityKind.RECORD, PERMISSION_PARENT.format(item=item))

    def upload(self, session: str) -> wire.StoredUploadSession | None:
        return self._get(wire.StoredUploadSession, upload_ref(session), UPLOADS)

    def copy(self, operation: str) -> CopyRecord | None:
        return self._get(CopyRecord, copy_ref(operation), COPIES)

    def faults(self) -> list[wire.StoredFault]:
        return self._all(wire.StoredFault, EntityKind.RECORD, FAULTS)

    def holds(self) -> list[wire.StoredHold]:
        return self._all(wire.StoredHold, EntityKind.RECORD, HOLDS)

    def write_hold(self, hold: wire.StoredHold) -> WorldEvent:
        return self.write(
            ref(EntityKind.RECORD, f"hold:{hold.position}"),
            hold,
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=HOLDS,
        )

    def seeded(self, title: str) -> str | None:
        """The item a seeded document of this title became."""
        return next((s.item for s in self._all(SeededRecord, EntityKind.RECORD, SEEDED) if s.title == title), None)

    def write_seeded(self, position: int, title: str, item: str) -> WorldEvent:
        return self.write(
            ref(EntityKind.RECORD, f"seeded:{position}"),
            SeededRecord(title=title, item=item),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=SEEDED,
        )

    def owe(self, drive: str, item: str, *, actor: Actor) -> WorldEvent:
        return self.write(
            ref(EntityKind.RECORD, f"owed:{self.next_seq()}"),
            OwedRecord(drive=drive, item=item),
            operation=Operation.CREATE,
            actor=actor,
            parent=OWED,
        )

    def owed(self) -> list[tuple[EntityRef, OwedRecord]]:
        return [(s.entity, wire.parse(OwedRecord, s.body)) for s in self._pages(EntityKind.RECORD, OWED)]

    def paid(self, owed: EntityRef) -> WorldEvent:
        return self.remove(owed, actor=Actor.SCENARIO, parent=OWED)

    def post(self, key: str) -> str | None:
        """The activity id a scenario's post key names."""
        found = self._get(PostRecord, post_ref(key), POSTS)
        return found.activity_id if found is not None else None

    # ------------------------------------------------------------------ subscriptions

    def subscription(self, sub: str) -> SubscriptionRecord | None:
        return self._get(SubscriptionRecord, subscription_ref(sub), SUBSCRIPTIONS)

    def subscriptions(self) -> list[SubscriptionRecord]:
        return self._all(SubscriptionRecord, EntityKind.RECORD, SUBSCRIPTIONS)
