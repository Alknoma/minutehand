"""The Slack workspace as entities in the run's store.

| Slack thing | `EntityKind` | external id | parent |
|---|---|---|---|
| user        | RECORD   | user id                     | the team |
| channel, IM | CHANNEL  | channel id                  | the team |
| membership  | RECORD   | `<channel>.<user>`          | the channel |
| message     | MESSAGE  | its `ts`, unique in the run | the channel |
| file        | DOCUMENT | file id                     | the team |
| file content (its bytes, base64) | RECORD | `content.<file>` | `files` |
| a scenario post's key | RECORD | `post.<key>`        | `posts` |
| view (modal, Home tab) | RECORD | `view.<id>`         | `views` |
| trigger_id  | RECORD   | `trigger.<id>`              | `triggers` |
| response_url | RECORD  | `hook.<id>`                 | `hooks` |
| a person's press or submission | RECORD | `interaction.<trigger_id>` | `interactions` |
| a slash command | RECORD | `command.<trigger_id>`    | `commands` |
| a scheduled message | RECORD | `scheduled.<id>` | `scheduled` |
| a file's upload ticket | RECORD | `upload.<file>` | `uploads` |
| a pinned message | RECORD | `pin.<channel>.<ts>` | `pins.<channel>` |
| the app's install | RECORD | `install`               | `app` |
| a fault     | RECORD   | `fault.<position>`          | `faults` |
| a workspace the app is in | RECORD | `workspace.<team>` | `workspaces` |
| a person's absences | RECORD | `away.<user>`            | `away` |
| the email of a member whose profile shows none | RECORD | `email.<user>` | `emails` |

A world holds one workspace or several (`SlackSeed.workspaces`). Users, channels and files are listed under their
workspace's team id; a channel's messages and members under the channel, whose id differs per workspace. Every
`SlackWorld` reads and writes as one workspace (`team`): the one the call's token is for, the one a person acts in.

Only users are RECORDs under the team and only memberships RECORDs under a channel: everything else Minutehand
keeps is listed under a parent of its own, so no listing of users or members ever meets it.

Nothing here is held between calls: every read is a query of the store, so a new
app over the same store sees the same workspace, and a fork sees it as of the fork.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from zoneinfo import ZoneInfo

from minutehand.adapters.providers.slack import wire
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.domain.absence import first_ask, placed
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    Operation,
    Snapshot,
    Stored,
    WorldEvent,
)
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

TEAM_ID = "T0WORKSPACE"
TEAM_NAME = "Simulated Workspace"
TEAM_DOMAIN = "simulated"
FILES_HOST = "files.slack.com"
HOOKS_HOST = "hooks.slack.com"
BOT_USER_ID = "U0AGENTBOT"
BOT_ID = "B0AGENTBOT"
APP_ID = "A0AGENTAPP"
BOT_NAME = "agent"
GENERAL = "general"

StoredBody = TypeVar("StoredBody", bound=wire.Model)

_SCAN = 1000
_MAX_SEQ_IN_TS = 999_999


def _derived(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:10].upper()


def user_id(person_key: str, team: str = TEAM_ID) -> str:
    """A person's member id in a workspace: the same for the same `Person.key` in every run; another workspace
    gives the same person another id, as Slack does."""
    return _derived("U", "person", person_key) if team == TEAM_ID else _derived("U", "person", team, person_key)


def other_bot_id(person_key: str) -> str:
    """The bot id of another app's bot user the scenario seeds."""
    return _derived("B", "bot", person_key)


def client_msg_id(ts: str) -> str:
    """The id a person's Slack client gives a message it sends, in UUID shape, fixed by the message's ts."""
    digest = hashlib.sha256(f"client|{ts}".encode()).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def named_channel_id(name: str, team: str = TEAM_ID) -> str:
    return _derived("C", "channel", name) if team == TEAM_ID else _derived("C", "channel", team, name)


def created_channel_id(seq: int, team: str = TEAM_ID) -> str:
    """The id of a channel the agent made whose name's own id is taken, named by the log position that made it."""
    return _derived("C", "created", str(seq)) if team == TEAM_ID else _derived("C", "created", team, str(seq))


def conversation_id(users: list[str]) -> str:
    """An IM (two members) or a group DM (more): one id per set of members, so opening one is idempotent."""
    members = sorted(set(users))
    return _derived("D" if len(members) <= 2 else "G", "conversation", *members)


def _ref(kind: EntityKind, external_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=kind, external_id=external_id)


def team_ref(team: str = TEAM_ID) -> EntityRef:
    """What a listing or a search of the whole workspace reads."""
    return _ref(EntityKind.RECORD, team)


def workspace_ref(team: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"workspace.{team}")


def away_ref(user: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"away.{user}")


def user_ref(user: str) -> EntityRef:
    return _ref(EntityKind.RECORD, user)


def channel_ref(channel: str) -> EntityRef:
    return _ref(EntityKind.CHANNEL, channel)


def membership_ref(channel: str, user: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"{channel}.{user}")


def message_ref(ts: str) -> EntityRef:
    return _ref(EntityKind.MESSAGE, ts)


def file_ref(file: str) -> EntityRef:
    return _ref(EntityKind.DOCUMENT, file)


def content_ref(file: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"content.{file}")


def post_ref(key: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"post.{key}")


def upload_ref(file: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"upload.{file}")


def upload_url(team: str, file: str, ticket: str) -> str:
    """Where the agent uploads a file's bytes: Slack's examples are `https://files.slack.com/upload/v1/` and an opaque key."""
    return f"https://{FILES_HOST}/upload/v1/{team}-{file}-{ticket}"


def pin_ref(channel: str, ts: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"pin.{channel}.{ts}")


def pins_of(channel: str) -> str:
    """The parent of a channel's pins: not the channel itself, whose children are its members."""
    return f"pins.{channel}"


def scheduled_ref(scheduled: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"scheduled.{scheduled}")


def scheduled_id(seq: int) -> str:
    """A `scheduled_message_id`: Slack's examples are a `Q` and ten digits, here the log position that scheduled it."""
    return f"Q{1_000_000_000 + seq}"


def view_ref(view: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"view.{view}")


def trigger_ref(trigger: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"trigger.{trigger}")


def hook_ref(hook: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"hook.{hook}")


def interaction_ref(trigger: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"interaction.{trigger}")


def command_ref(trigger: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"command.{trigger}")


def install_ref(team: str = TEAM_ID) -> EntityRef:
    return _ref(EntityKind.RECORD, "install" if team == TEAM_ID else f"install.{team}")


def fault_ref(position: int) -> EntityRef:
    return _ref(EntityKind.RECORD, f"fault.{position}")


def unlisted_email_ref(user: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"email.{user}")


FILES = "files"
SCHEDULED = "scheduled"
UPLOADS = "uploads"
EMAILS = "emails"
POSTS = "posts"
VIEWS = "views"
TRIGGERS = "triggers"
HOOKS = "hooks"
INTERACTIONS = "interactions"
COMMANDS = "commands"
APP = "app"
FAULTS = "faults"
WORKSPACES = "workspaces"
AWAY = "away"

DEFAULT_WORKSPACE = wire.SlackWorkspace(
    id=TEAM_ID,
    name=TEAM_NAME,
    domain=TEAM_DOMAIN,
    bot_user_id=BOT_USER_ID,
    bot_id=BOT_ID,
    app_id=APP_ID,
    bot_name=BOT_NAME,
)
"""The workspace a world is when its seed names none: any token, or none, is its bot."""

SLACKBOT_ID = "USLACKBOT"
SLACKBOT_TZ = "America/Los_Angeles"


def slackbot(team: str, at: datetime) -> wire.SlackUser:
    """Slackbot, as `users.list` and `users.info` serve it in every workspace: the same id everywhere, named
    `slackbot`, not a bot (`is_bot` is false for it), and no email, as Slack's own `users.list` example lists it
    (https://docs.slack.dev/apis/web-api/pagination). Nobody seeds it and it is never stored: every workspace has it."""
    offset = ZoneInfo(SLACKBOT_TZ).utcoffset(at)
    return wire.SlackUser(
        id=SLACKBOT_ID,
        team_id=team,
        name="slackbot",
        real_name="slackbot",
        tz=SLACKBOT_TZ,
        tz_offset=int(offset.total_seconds()) if offset is not None else 0,
        profile=wire.SlackProfile(real_name="slackbot", display_name=""),
    )


def file_id(seed: str) -> str:
    return _derived("F", "file", seed)


def url_private(file: str, name: str, team: str = TEAM_ID) -> str:
    return f"https://{FILES_HOST}/files-pri/{team}-{file}/{name}"


def url_private_download(file: str, name: str, team: str = TEAM_ID) -> str:
    return f"https://{FILES_HOST}/files-pri/{team}-{file}/download/{name}"


def response_url(hook: str, secret: str, *, command: bool, team: str = TEAM_ID) -> str:
    return f"https://{HOOKS_HOST}/{'commands' if command else 'actions'}/{team}/{hook}/{secret}"


def bot_profile(updated: int, workspace: wire.SlackWorkspace = DEFAULT_WORKSPACE) -> wire.BotProfile:
    """The `bot_profile` Slack attaches to every message the app posts."""
    icon = "https://a.slack-edge.com/80588/img/plugins/app/bot_36.png"
    return wire.BotProfile(
        id=workspace.bot_id,
        app_id=workspace.app_id,
        name=workspace.bot_name,
        icons=wire.BotIcons(image_36=icon, image_48=icon.replace("36", "48"), image_72=icon.replace("36", "72")),
        updated=updated,
        team_id=workspace.id,
    )


def view_id(seq: int) -> str:
    return _derived("V", "view", str(seq))


def home_view_id(user: str) -> str:
    return _derived("V", "home", user)


def view_hash(view: str, version: int) -> str:
    """A view's `hash`: changes with every update, so a stale one is refused."""
    return f"{version}.{hashlib.sha256(f'{view}|{version}'.encode()).hexdigest()[:8]}"


def minted(kind: str, seq: int, at: int) -> str:
    """An id of the shape Slack gives a trigger or a view, unique in the run because `seq` is."""
    tail = hashlib.sha256(f"{kind}|{seq}|{at}".encode()).hexdigest()[:20]
    return f"{seq}.{at}.{tail}"


@dataclass(frozen=True)
class Away:
    """A stretch a person is away, in epoch seconds, and what their status says of it."""

    reason: str | None
    starts: int
    ends: int


class SlackWorld:
    """Typed reads and writes of one run's Slack entities, as one of its workspaces: `team`, or with none given the
    first the world holds."""

    def __init__(self, store: Store, team: wire.SlackWorkspace | None = None) -> None:
        self._store = store
        self.team = team if team is not None else self._first()

    @property
    def store(self) -> Store:
        return self._store

    def _first(self) -> wire.SlackWorkspace:
        return self.workspaces()[0]

    # ------------------------------------------------------------------ workspaces

    def workspaces(self) -> list[wire.SlackWorkspace]:
        """Every workspace the world holds, in the order seeded; the default one when it holds none."""
        found = [wire.parse(wire.SlackWorkspace, s.body) for s in self._pages(EntityKind.RECORD, WORKSPACES)]
        return sorted(found, key=lambda w: w.position) or [DEFAULT_WORKSPACE]

    def as_team(self, team: wire.SlackWorkspace) -> SlackWorld:
        return SlackWorld(self._store, team)

    def team_of(self, team_id: str) -> SlackWorld | None:
        found = next((w for w in self.workspaces() if w.id == team_id), None)
        return None if found is None else self.as_team(found)

    def for_token(self, token: str | None) -> SlackWorld:
        """The workspace a call is answered in. A token decides it only by naming a workspace: one the workspace
        declares or its install minted. Any other token, or none, is the app's in the first workspace that declares
        no tokens, else the first workspace: Minutehand never refuses a credential."""
        every = self.workspaces()
        for workspace in every:
            install = self.body(install_ref(workspace.id), wire.SlackInstall)
            if token is not None and (token in workspace.tokens or (install is not None and token in install.tokens)):
                return self.as_team(workspace)
        return self.as_team(next((w for w in every if not w.tokens), every[0]))

    def channel_team(self, channel: str) -> SlackWorld | None:
        """The workspace a channel is in."""
        stored = self._store.get(channel_ref(channel))
        return None if stored is None or stored.parent is None else self.team_of(stored.parent)

    @property
    def bot(self) -> str:
        return self.team.bot_user_id

    # ------------------------------------------------------------------ reads

    def _pages(self, kind: EntityKind, parent: str, after: str | None = None) -> Iterator[Stored]:
        while True:
            page = self._store.children(MANIFEST.key, kind, parent, after=after, limit=_SCAN)
            yield from page
            if len(page) < _SCAN:
                return
            after = page[-1].entity.external_id

    def user(self, user: str) -> wire.SlackUser | None:
        stored = self._store.get(user_ref(user))
        if stored is None or stored.parent != self.team.id:
            return None
        return wire.parse(wire.SlackUser, stored.body)

    def users(self, *, after: str | None, limit: int) -> list[wire.SlackUser]:
        page = self._store.children(MANIFEST.key, EntityKind.RECORD, self.team.id, after=after, limit=limit)
        return [wire.parse(wire.SlackUser, s.body) for s in page]

    def every_user(self, after: str | None = None) -> Iterator[wire.SlackUser]:
        for stored in self._pages(EntityKind.RECORD, self.team.id, after):
            yield wire.parse(wire.SlackUser, stored.body)

    def channel(self, channel: str) -> wire.SlackChannel | None:
        stored = self._store.get(channel_ref(channel))
        if stored is None or stored.parent != self.team.id:
            return None
        return wire.parse(wire.SlackChannel, stored.body)

    def channels_after(self, after: str | None) -> Iterator[wire.SlackChannel]:
        for stored in self._pages(EntityKind.CHANNEL, self.team.id, after):
            yield wire.parse(wire.SlackChannel, stored.body)

    def is_member(self, channel: str, user: str) -> bool:
        return self._store.get(membership_ref(channel, user)) is not None

    def members(self, channel: str, *, after: str | None, limit: int) -> list[tuple[str, wire.SlackMembership]]:
        """One page of memberships, each with the position a cursor resumes after."""
        page = self._store.children(MANIFEST.key, EntityKind.RECORD, channel, after=after, limit=limit)
        return [(s.entity.external_id, wire.parse(wire.SlackMembership, s.body)) for s in page]

    def every_member(self, channel: str) -> list[str]:
        return [wire.parse(wire.SlackMembership, s.body).user for s in self._pages(EntityKind.RECORD, channel)]

    def human_emails(self, channel: str, *, besides: str) -> list[str]:
        """Who a message in `channel` reaches: its human members other than the author."""
        emails: list[str] = []
        for member in self.every_member(channel):
            user = self.user(member)
            email = self.email_of(user) if user is not None and member != besides else None
            if email is not None:
                emails.append(email)
        return emails

    def email_of(self, user: wire.SlackUser) -> str | None:
        """Whom a message to this member reaches: the person's email, whether or not their profile shows it (the
        world keeps one it hides beside the profile); None for a bot."""
        if user.is_bot:
            return None
        if user.profile.email is not None:
            return user.profile.email
        found = self.body(unlisted_email_ref(user.id), wire.SlackUnlistedEmail)
        return None if found is None else found.email

    def messages(self, channel: str) -> list[wire.SlackMessage]:
        """Every live message in the channel that anyone can list, roots and replies, oldest first: an
        ephemeral one was shown to one member once and is in no listing."""
        found = (wire.parse(wire.SlackMessage, s.body) for s in self._pages(EntityKind.MESSAGE, channel))
        return [m for m in found if m.ephemeral_to is None]

    def message(self, channel: str, ts: str) -> wire.SlackMessage | None:
        """A message the Web API can find by its ts; an ephemeral one it cannot."""
        stored = self._store.get(message_ref(ts))
        if stored is None or stored.parent != channel:
            return None
        found = wire.parse(wire.SlackMessage, stored.body)
        return found if found.ephemeral_to is None else None

    def located(self, ts: str) -> tuple[str, wire.SlackMessage] | None:
        """A message and the channel it is in, from its ts alone, ephemeral or not."""
        stored = self._store.get(message_ref(ts))
        if stored is None or stored.parent is None:
            return None
        return stored.parent, wire.parse(wire.SlackMessage, stored.body)

    def next_ts(self, clock: Clock) -> str:
        """A Slack `ts`: the clock's second, and the sequence of the event about to be written.

        Two messages in one simulated instant still get distinct, ordered stamps,
        because no two events share a sequence number.
        """
        return self.ts_at(int(clock.now().timestamp()))

    def ts_at(self, second: int) -> str:
        """A `ts` at `second`, unique by the next event's sequence; one a seeded message already holds is passed
        over for the next free one, since seeded stamps are not numbered by the log (`seeded_ts`)."""
        seq = self.next_seq()
        while True:
            if seq > _MAX_SEQ_IN_TS:
                raise OverflowError(f"event {seq} no longer fits the six digits of a Slack ts")
            ts = f"{second}.{seq:06d}"
            if self._store.get(message_ref(ts)) is None and not self._store.versions(message_ref(ts)):
                return ts
            seq += 1

    def seeded_ts(self, channel: str, second: int, nth: int) -> str:
        """The `ts` of the `nth` (from 1) message seeded in `channel` at `second`: named by the channel and its order
        there, never by the log's position, so the same seed gives the same stamps however far into a world it is
        written. The channel's own three digits keep two channels' messages in one second apart; when two channels'
        digits meet, the later-seeded message takes the next free stamp."""
        slot = int(hashlib.sha256(f"seeded|{self.team.id}|{channel}".encode()).hexdigest(), 16) % 1000
        for n in range(nth, 1000):
            ts = f"{second}.{slot * 1000 + n:06d}"
            if self._store.get(message_ref(ts)) is None and not self._store.versions(message_ref(ts)):
                return ts
        raise OverflowError(f"more than 999 messages are seeded in {channel} at the second {second}")

    def next_seq(self) -> int:
        return self._store.head() + 1

    def body(self, ref: EntityRef, model: type[StoredBody]) -> StoredBody | None:
        stored = self._store.get(ref)
        return None if stored is None else wire.parse(model, stored.body)

    def bodies(self, kind: EntityKind, parent: str, model: type[StoredBody]) -> list[StoredBody]:
        return [wire.parse(model, s.body) for s in self._pages(kind, parent)]

    def away(self, user: str, now: int) -> Away | None:
        """The absence the person is in at `now`, anchored as the scenario's checks anchor it: at the scenario's
        start, or at the agent's first message to them (on any provider), each plus its own `starts_after`."""
        found = self.body(away_ref(user), wire.SlackAway)
        if found is None:
            return None
        asked = first_ask(found.email, self._store.events()) if any(s.on_first_ask for s in found.stretches) else None
        for stretch in found.stretches:
            span = placed(
                from_start=None if stretch.on_first_ask else datetime.fromtimestamp(found.starts_at, UTC),
                asked=asked,
                starts_after=timedelta(seconds=stretch.starts_after),
                lasts=timedelta(seconds=stretch.lasts),
            )
            if span is not None and span[0].timestamp() <= now < span[1].timestamp():
                starts = int(span[0].timestamp())
                return Away(reason=stretch.reason, starts=starts, ends=starts + stretch.lasts)
        return None

    def post(self, key: str) -> wire.SlackPostKey | None:
        return self.body(post_ref(key), wire.SlackPostKey)

    def file(self, file: str) -> wire.SlackFile | None:
        stored = self._store.get(file_ref(file))
        return None if stored is None or stored.parent != self.team.id else wire.parse(wire.SlackFile, stored.body)

    def every_file(self) -> list[wire.SlackFile]:
        """Every file of the workspace that has not been deleted, oldest first."""
        found = [wire.parse(wire.SlackFile, s.body) for s in self._pages(EntityKind.DOCUMENT, self.team.id)]
        return sorted(found, key=lambda f: (f.created, f.id))

    def was_file(self, file: str) -> bool:
        """Whether a file of this id once was, and is gone."""
        return self._store.get(file_ref(file)) is None and bool(self._store.versions(file_ref(file)))

    # ------------------------------------------------------------------ writes

    def write(
        self,
        ref: EntityRef,
        body: wire.Model,
        *,
        operation: Operation,
        actor: Actor,
        parent: str,
        after: Snapshot | None = None,
    ) -> WorldEvent:
        return self._store.apply(
            Change(entity=ref, operation=operation, actor=actor, body=wire.dump(body), parent=parent, after=after)
        )

    def delete(self, ref: EntityRef, *, actor: Actor, parent: str, before: Snapshot | None = None) -> WorldEvent:
        """`before`: what the deleted thing was, as the checks read it (a message's text, who it reached, its
        thread), so its deletion reads as what was deleted."""
        return self._store.apply(
            Change(entity=ref, operation=Operation.DELETE, actor=actor, parent=parent, after=before)
        )

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))

    def open_conversation(self, members: list[str], *, created: int, actor: Actor) -> wire.SlackChannel:
        """Write an IM (the app and one other) or a group DM, and a membership for each member."""
        cid = conversation_id(members)
        bot = self.team.bot_user_id
        others = [m for m in members if m != bot]
        if len(set(members)) <= 2:
            channel = wire.SlackChannel(
                id=cid,
                is_im=True,
                is_private=True,
                created=created,
                creator=bot,
                user=others[0] if others else bot,
            )
        else:
            channel = wire.SlackChannel(
                id=cid,
                name="mpdm-" + "--".join(sorted(members)) + "-1",
                is_mpim=True,
                is_group=True,
                is_private=True,
                created=created,
                creator=bot,
            )
        self.write(channel_ref(cid), channel, operation=Operation.CREATE, actor=actor, parent=self.team.id)
        for member in sorted(set(members)):
            self.write(
                membership_ref(cid, member),
                wire.SlackMembership(channel=cid, user=member),
                operation=Operation.CREATE,
                actor=actor,
                parent=cid,
            )
        return channel
