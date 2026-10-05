"""The tenant a scenario starts in.

- **The directory** (`state.directory_of`): one tenant, its `onmicrosoft.com` domain and SharePoint host, and the
  agent's bot registered as an app with a secret, all derived from the scenario's name, so a service's environment
  can be given them before the run.
- **People**: every person is a user of the tenant (a guest's principal name carries `#EXT#`); a person whose
  account is `bot` is another app's and is not a user.
- **Teams**: one team holding everyone, its General channel, and a 1:1 chat with the bot for each person, with the
  bot installed in all of them, so it has every conversation reference from the start. The scenario's channels for
  this provider add named channels to the team, or chats with no name, with their history.
- **Files**: a team site with its document library, and a OneDrive for each person; every document the scenario
  seeds for this provider is a Word file (`.docx`, real bytes) in the library, in its folder.
- **Faults** the scenario's Microsoft seed (`MicrosoftSeed.faults`) declares, each answered in front of the surface its
  `call` names.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field

from minutehand.adapters.providers.microsoft import docx, wire
from minutehand.adapters.providers.microsoft.cards import message_actions
from minutehand.adapters.providers.microsoft.graph_files import Files, item_id, mime_of
from minutehand.adapters.providers.microsoft.manifest import MANIFEST
from minutehand.adapters.providers.microsoft.state import (
    APPS,
    DRIVES,
    FAULTS,
    POSTS,
    SERVICE_URL,
    SITES,
    TEAMS,
    TENANTS,
    USERS,
    AppRecord,
    AwayRecord,
    ConversationRecord,
    Directory,
    DriveRecord,
    MicrosoftWorld,
    PostRecord,
    SiteRecord,
    TeamRecord,
    TenantRecord,
    UserRecord,
    app_ref,
    derived_uuid,
    directory_of,
    drive_ref,
    fault_ref,
    graph_time,
    message_ref,
    post_ref,
    site_ref,
    team_ref,
    tenant_ref,
    user_ref,
)
from minutehand.domain.scenario import (
    AbsenceTrigger,
    Account,
    Model,
    Person,
    Scenario,
    SeededChannel,
    SeededPost,
)
from minutehand.domain.world import Actor, MessageSnapshot, Operation

ERROR_STATUS = {
    "itemNotFound": 404,
    "accessDenied": 403,
    "resyncRequired": 410,
    "resourceLocked": 423,
    "serviceNotAvailable": 503,
    "activityLimitReached": 429,
    "TooManyRequests": 429,
    "ConversationNotFound": 404,
    "BotNotInConversationRoster": 403,
    "InvalidAuthenticationToken": 401,
    "generalException": 500,
}
"""The status each error code Graph or the connector sends is answered with; any other code is a 400."""


class RateLimited(Model):
    kind: Literal["rate_limited"] = "rate_limited"
    retry_after: timedelta = Field(default=timedelta(seconds=1), gt=timedelta(0))


class Refused(Model):
    kind: Literal["refused"] = "refused"
    error: str = Field(min_length=1, description="Graph's or the connector's own error code, as it sends it")


class WithoutId(Model):
    """A send the connector carries out and answers 201 as usual, but with no `id` (nor a new conversation's
    `activityId`) in its answer: what a bot that keeps the id of what it sent must survive."""

    kind: Literal["without_id"] = "without_id"


class FaultSeed(Model):
    """A call Graph or the connector fails on purpose, in that surface's own error shape; or, `without_id`, a
    connector send answered without the id of what it sent."""

    call: str | None = Field(default=None, description="'METHOD /path prefix' or '/path prefix'; None: every call")
    answer: Annotated[RateLimited | Refused | WithoutId, Field(discriminator="kind")]
    times: int | None = Field(default=1, ge=1, description="How many calls it fails; None is every one")
    after: timedelta = Field(default=timedelta(0), ge=timedelta(0), description="From this offset on")
    only_rich: bool = Field(default=False, description="Only a connector POST: a send, a reply or a new conversation")


class HoldSeed(Model):
    """A person holds a seeded document open for editing, so every write to it is refused 423 `resourceLocked`
    while they do: a failure the scenario sets up on purpose, not something they do to the document."""

    document: str = Field(description="The title of a seeded Microsoft document")
    by: str = Field(description="Person.key of whoever holds it")
    after: timedelta = Field(default=timedelta(0), ge=timedelta(0), description="From this offset on")
    lasts: timedelta | None = Field(default=None, gt=timedelta(0), description="None: for good")


class MicrosoftSeed(Model):
    """What only Microsoft seeds, as the body of the scenario's `ProviderSeed` for `microsoft`."""

    faults: list[FaultSeed] = []
    holds: list[HoldSeed] = []
    not_installed_for: list[str] = Field(
        default=[],
        description="Person.key of each person whose own chat with the bot does not exist yet: the bot reaches them "
        "only once they install it (`PersonAddsAgent` with no channel)",
    )


def microsoft_seed(scenario: Scenario) -> MicrosoftSeed:
    found = scenario.provider_seed(MANIFEST.key)
    return MicrosoftSeed() if found is None else MicrosoftSeed.model_validate_json(found.body)


class _At:
    """The clock while a world is seeded: the scenario's start, which is where the run's clock stands then."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def wake(self) -> int:
        return 0


def _graph_user(person: Person, directory: Directory) -> wire.GraphUser:
    local = person.email.split("@")[0]
    guest = person.account is Account.GUEST
    upn = (
        f"{person.email.replace('@', '_')}#EXT#@{directory.tenant_domain}"
        if guest
        else f"{local}@{directory.tenant_domain}"
    )
    given, _, surname = person.name.partition(" ")
    return wire.GraphUser(
        id=derived_uuid(directory.tenant_id, "user", person.key),
        displayName=person.name,
        givenName=given or None,
        surname=surname or None,
        mail=person.email,
        userPrincipalName=upn,
        jobTitle=person.title,
    )


def _drive(
    world: MicrosoftWorld,
    directory: Directory,
    *,
    name: str,
    kind: str,
    web_url: str,
    stamp: str,
    owner: wire.IdentitySet,
    site_id: str | None,
    owner_id: str | None,
) -> DriveRecord:
    drive_id = "b!" + derived_uuid(directory.tenant_id, "drive", web_url).replace("-", "")
    root = wire.DriveItem(
        id=item_id(drive_id, 0),
        name="root",
        eTag=f'"{{{item_id(drive_id, 0)}}},1"',
        cTag=f'"c:{{{item_id(drive_id, 0)}}},1"',
        size=0,
        createdDateTime=stamp,
        lastModifiedDateTime=stamp,
        webUrl=web_url,
        createdBy=owner,
        lastModifiedBy=owner,
        parentReference=wire.ItemReference(driveId=drive_id, driveType=kind),
        folder=wire.FolderFacet(childCount=0),
        root=wire.RootFacet(),
    )
    world.write_item(wire.StoredItem(item=root), operation=Operation.CREATE, actor=Actor.SCENARIO)
    record = DriveRecord(
        drive=wire.Drive(
            id=drive_id,
            name=name,
            driveType=kind,  # type: ignore[arg-type]
            webUrl=web_url,
            createdDateTime=stamp,
            lastModifiedDateTime=stamp,
            owner=wire.DriveOwner(user=owner.user, group=owner.application),
            quota=wire.Quota(total=1 << 40, used=0, remaining=1 << 40),
        ),
        root_id=root.id,
        site_id=site_id,
        owner_id=owner_id,
    )
    world.write(drive_ref(drive_id), record, operation=Operation.CREATE, actor=Actor.SCENARIO, parent=DRIVES)
    return record


def _history(
    world: MicrosoftWorld,
    conversation: ConversationRecord,
    posts: list[SeededPost],
    users: dict[str, UserRecord],
    start: datetime,
    thread: str | None,
    seconds: dict[int, int],
) -> None:
    """Each post and its replies; `seconds` counts the seeded messages already given each second, so a message's id
    is its second and its place among the scenario's posts of that second: the same wherever seeding starts in the
    log, and unmoved by anything seeded after it. A seeded post is before the start, so no minted id (the start or
    later) can be one of these."""
    for post in posts:
        author = users[post.by]
        at = start - post.ago
        second = int(at.timestamp())
        seconds[second] = seconds[second] + 1 if second in seconds else 1
        activity = wire.Activity(
            type=wire.ActivityType.MESSAGE,
            id=f"{second}{seconds[second]:06d}",
            timestamp=graph_time(at),
            serviceUrl=SERVICE_URL,
            sender=wire.ChannelAccount(id=author.mri, name=author.user.displayName, aadObjectId=author.user.id),
            conversation=wire.ConversationAccount(
                id=conversation.id, conversationType=conversation.type, tenantId=conversation.tenant_id
            ),
            recipient=wire.ChannelAccount(id=conversation.id),
            text=post.text,
            replyToId=thread,
            attachments=[wire.Attachment(contentType="text/plain", name=f.name, content=f.text) for f in post.files]
            or None,
        )
        recipients = [
            u.user.mail
            for oid in conversation.members
            if oid != author.user.id and (u := world.user(oid)) and u.user.mail
        ]
        world.write(
            message_ref(activity.id),
            activity,
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=conversation.id,
            after=MessageSnapshot(
                text=post.text,
                channel=conversation.id,
                recipient_emails=recipients,
                thread_of=thread,
                actions=message_actions(activity),
            ),
        )
        if post.key is not None:
            world.write(
                post_ref(post.key),
                PostRecord(key=post.key, activity_id=activity.id),
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
                parent=POSTS,
            )
        if conversation.type is wire.ConversationType.CHANNEL:
            _history(world, conversation, post.replies, users, start, activity.id, seconds)
        else:
            _history(world, conversation, post.replies, users, start, None, seconds)


def _channel(
    world: MicrosoftWorld, directory: Directory, seeded: SeededChannel, users: dict[str, UserRecord], stamp: str
) -> ConversationRecord:
    members = [users[k].user.id for k in seeded.members]
    if seeded.name is not None:
        graph_id = f"19:{derived_uuid(directory.tenant_id, 'channel', seeded.name).replace('-', '')}@thread.tacv2"
        return ConversationRecord(
            id=graph_id,
            graph_id=graph_id,
            type=wire.ConversationType.CHANNEL,
            tenant_id=directory.tenant_id,
            display_name=seeded.name,
            team_id=directory.team_id,
            members=members,
            bot_installed=seeded.agent_member,
            created=stamp,
        )
    if len(members) == 1:
        existing = world.personal_with(members[0], directory.tenant_id)
        assert existing is not None
        return existing.model_copy(update={"bot_installed": seeded.agent_member})
    group = f"19:{derived_uuid(directory.tenant_id, 'group', *sorted(members)).replace('-', '')}@thread.v2"
    return ConversationRecord(
        id=group,
        graph_id=group,
        type=wire.ConversationType.GROUP_CHAT,
        tenant_id=directory.tenant_id,
        display_name=seeded.topic or None,
        members=members,
        bot_installed=seeded.agent_member,
        created=stamp,
    )


def seed(scenario: Scenario, world: MicrosoftWorld) -> None:
    directory = directory_of(scenario)
    stamp = graph_time(scenario.starts_at)
    world.write(
        tenant_ref(directory.tenant_id),
        TenantRecord(
            id=directory.tenant_id,
            domain=directory.tenant_domain,
            sharepoint_host=directory.sharepoint_host,
            display_name=scenario.name,
        ),
        operation=Operation.CREATE,
        actor=Actor.SCENARIO,
        parent=TENANTS,
    )
    world.write(
        app_ref(directory.bot_app_id),
        AppRecord(
            app_id=directory.bot_app_id,
            secret=directory.bot_app_secret,
            display_name=directory.bot_name,
            tenant_id=directory.tenant_id,
        ),
        operation=Operation.CREATE,
        actor=Actor.SCENARIO,
        parent=APPS,
    )
    users: dict[str, UserRecord] = {}
    for person in scenario.people:
        if person.account is Account.BOT:
            continue
        user = UserRecord(
            user=_graph_user(person, directory),
            tenant_id=directory.tenant_id,
            person_key=person.key,
            absences=[
                AwayRecord(
                    starts=scenario.starts_at if a.trigger is AbsenceTrigger.AT_START else None,
                    starts_after=a.starts_after,
                    lasts=a.lasts,
                    reason=a.reason,
                )
                for a in person.absences
            ]
            or None,
        )
        users[person.key] = user
        world.write(user_ref(user.user.id), user, operation=Operation.CREATE, actor=Actor.SCENARIO, parent=USERS)
    spec = microsoft_seed(scenario)
    unknown = sorted(set(spec.not_installed_for) - set(users))
    if unknown:
        raise ValueError(f"the Microsoft seed names no user of the tenant: {', '.join(unknown)}")
    for user in users.values():
        personal = ConversationRecord(
            id="a:" + derived_uuid(directory.tenant_id, "personal", user.user.id).replace("-", ""),
            graph_id=f"19:{user.user.id}_{directory.bot_app_id}@unq.gbl.spaces",
            type=wire.ConversationType.PERSONAL,
            tenant_id=directory.tenant_id,
            members=[user.user.id],
            bot_installed=user.person_key not in spec.not_installed_for,
            created=stamp,
        )
        world.write_conversation(personal, operation=Operation.CREATE, actor=Actor.SCENARIO)
    everyone = [u.user.id for u in users.values() if u.person_key is not None]
    team = TeamRecord(
        id=directory.team_id,
        display_name=f"{scenario.name.replace('_', ' ').title()} Team",
        tenant_id=directory.tenant_id,
        general_channel_id=directory.general_channel_id,
        members=everyone,
    )
    world.write(team_ref(team.id), team, operation=Operation.CREATE, actor=Actor.SCENARIO, parent=TEAMS)
    general = ConversationRecord(
        id=directory.general_channel_id,
        graph_id=directory.general_channel_id,
        type=wire.ConversationType.CHANNEL,
        tenant_id=directory.tenant_id,
        team_id=team.id,
        members=everyone,
        bot_installed=True,
        created=stamp,
    )
    world.write_conversation(general, operation=Operation.CREATE, actor=Actor.SCENARIO)
    seconds: dict[int, int] = {}
    for seeded in scenario.channels:
        if seeded.provider != MANIFEST.key:
            continue
        conversation = general if seeded.name == "general" else _channel(world, directory, seeded, users, stamp)
        if conversation is not general:
            exists = world.conversation(conversation.id) is not None
            world.write_conversation(
                conversation, operation=Operation.UPDATE if exists else Operation.CREATE, actor=Actor.SCENARIO
            )
        _history(world, conversation, seeded.history, users, scenario.starts_at, None, seconds)

    site_id = f"{directory.sharepoint_host},{derived_uuid(directory.tenant_id, 'site')},{derived_uuid(directory.tenant_id, 'web')}"
    site_url = f"https://{directory.sharepoint_host}/sites/{team.display_name.replace(' ', '')}"
    owner = wire.IdentitySet(application=wire.Identity(id=directory.bot_app_id, displayName=directory.bot_name))
    library = _drive(
        world,
        directory,
        name="Documents",
        kind="documentLibrary",
        web_url=f"{site_url}/Shared%20Documents",
        stamp=stamp,
        owner=owner,
        site_id=site_id,
        owner_id=None,
    )
    world.write(
        site_ref(site_id),
        SiteRecord(
            site=wire.Site(
                id=site_id,
                name=team.display_name.replace(" ", ""),
                displayName=team.display_name,
                webUrl=site_url,
                createdDateTime=stamp,
                lastModifiedDateTime=stamp,
                siteCollection=wire.SiteCollection(hostname=directory.sharepoint_host),
            ),
            drive_id=library.drive.id,
        ),
        operation=Operation.CREATE,
        actor=Actor.SCENARIO,
        parent=SITES,
    )
    for user in users.values():
        local = user.user.userPrincipalName.split("@")[0].replace(".", "_")
        _drive(
            world,
            directory,
            name="OneDrive",
            kind="business",
            web_url=f"https://{directory.sharepoint_host.replace('.sharepoint.com', '-my.sharepoint.com')}/personal/{local}/Documents",
            stamp=stamp,
            owner=wire.IdentitySet(user=wire.Identity(id=user.user.id, displayName=user.user.displayName)),
            site_id=None,
            owner_id=user.user.id,
        )
    _documents(world, scenario, library, owner)
    _faults(world, scenario)


def _documents(world: MicrosoftWorld, scenario: Scenario, library: DriveRecord, owner: wire.IdentitySet) -> None:
    files = Files(world, _At(scenario.starts_at), seeding=True)
    root = world.item(library.root_id)
    assert root is not None
    for position, document in enumerate(scenario.documents):
        if document.provider != MANIFEST.key:
            continue
        folder = root
        if document.folder is not None:
            found = next((c for c in world.children(root.item.id) if c.item.name == document.folder), None)
            if found is None:
                made = files.new_item(library, root, document.folder, folder=True, content=b"", by=owner)
                world.write_item(made, operation=Operation.CREATE, actor=Actor.SCENARIO)
                found = made
            folder = found
        name = document.title if "." in document.title else f"{document.title}.docx"
        content = docx.build(document.text) if mime_of(name) == docx.DOCX else document.text.encode()
        made, _ = files.write_content(
            library, folder, name, content, behaviour="replace", by=owner, actor=Actor.SCENARIO
        )
        world.write_seeded(position, document.title, made.item.id)


def _faults(world: MicrosoftWorld, scenario: Scenario) -> None:
    spec = microsoft_seed(scenario)
    write_faults(world, spec.faults, scenario.starts_at)
    write_holds(world, spec.holds, scenario.starts_at)


def write_holds(world: MicrosoftWorld, holds: list[HoldSeed], starts_at: datetime) -> None:
    """Record each hold after any already recorded; the document and the person must both be there, for every
    hold, before any is written."""
    made: list[wire.StoredHold] = []
    for position, hold in enumerate(holds, start=len(world.holds())):
        item = world.seeded(hold.document)
        if item is None:
            raise ValueError(f"a hold names {hold.document!r}, which is no seeded Microsoft document")
        holder = world.person(hold.by)
        if holder is None:
            raise ValueError(f"a hold names {hold.by}, who is no user of the tenant")
        start = starts_at + hold.after
        made.append(
            wire.StoredHold(
                position=position,
                item=item,
                by=holder.user.mail or holder.user.userPrincipalName,
                from_time=int(start.timestamp()),
                until_time=int((start + hold.lasts).timestamp()) if hold.lasts is not None else None,
            )
        )
    for hold in made:
        world.write_hold(hold)


DECLARED = 1_000_000
"""Where the numbers of faults declared on an open world (`provider-faults`) start: above every number a seed gives
its own faults, which count from 0 in the seed's order, so a fault a later seed fragment adds never takes the
number of one declared before it, and the seed's are armed ahead of the declared ones."""


def write_faults(
    world: MicrosoftWorld, faults: list[FaultSeed], starts_at: datetime, *, declared: bool = False
) -> None:
    """Record declared faults after any already recorded, each from `starts_at` plus its own offset."""
    first = (DECLARED if declared else 0) + len(world.faults())
    for position, fault in enumerate(faults, start=first):
        answer = fault.answer
        limited = isinstance(answer, RateLimited)
        stripped = isinstance(answer, WithoutId)
        error = "TooManyRequests" if limited else ("" if isinstance(answer, WithoutId) else answer.error)
        world.write(
            fault_ref(position),
            wire.StoredFault(
                position=position,
                call=fault.call,
                error=error,
                status=429 if limited else 201 if stripped else (ERROR_STATUS[error] if error in ERROR_STATUS else 400),
                retry_after=int(answer.retry_after.total_seconds()) if isinstance(answer, RateLimited) else None,
                remaining=fault.times,
                from_time=int((starts_at + fault.after).timestamp()),
                only_rich=fault.only_rich,
                without_id=stripped,
            ),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=FAULTS,
        )
