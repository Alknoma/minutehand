"""The Slack workspace as entities in the run's store.

| Slack thing | `EntityKind` | external id | parent |
|---|---|---|---|
| user        | RECORD   | user id                     | the team |
| channel, IM | CHANNEL  | channel id                  | the team |
| membership  | RECORD   | `<channel>.<user>`          | the channel |
| message     | MESSAGE  | its `ts`, unique in the run | the channel |
| file        | DOCUMENT | file id                     | the team |
| file content | RECORD  | `content.<file>`            | `files` |
| a scenario post's key | RECORD | `post.<key>`        | `posts` |
| view (modal, Home tab) | RECORD | `view.<id>`         | `views` |
| trigger_id  | RECORD   | `trigger.<id>`              | `triggers` |
| response_url | RECORD  | `hook.<id>`                 | `hooks` |
| a person's press or submission | RECORD | `interaction.<trigger_id>` | `interactions` |
| a slash command | RECORD | `command.<trigger_id>`    | `commands` |
| the app's install | RECORD | `install`               | `app` |
| a fault     | RECORD   | `fault.<position>`          | `faults` |
| a declared sign-in | RECORD | `sign_in.<digest of the token>` | `sign_ins` |

Only users are RECORDs under the team and only memberships RECORDs under a channel: everything else Minutehand
keeps is listed under a parent of its own, so no listing of users or members ever meets it.

Nothing here is held between calls: every read is a query of the store, so a new
app over the same store sees the same workspace, and a fork sees it as of the fork.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import TypeVar

from minutehand.adapters.providers.slack import wire
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, Snapshot, Stored, WorldEvent
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


def user_id(person_key: str) -> str:
    """A person's member id: the same for the same `Person.key` in every run."""
    return _derived("U", "person", person_key)


def other_bot_id(person_key: str) -> str:
    """The bot id of another app's bot user the scenario seeds."""
    return _derived("B", "bot", person_key)


def client_msg_id(ts: str) -> str:
    """The id a person's Slack client gives a message it sends, in UUID shape, fixed by the message's ts."""
    digest = hashlib.sha256(f"client|{ts}".encode()).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def named_channel_id(name: str) -> str:
    return _derived("C", "channel", name)


def conversation_id(users: list[str]) -> str:
    """An IM (two members) or a group DM (more): one id per set of members, so opening one is idempotent."""
    members = sorted(set(users))
    return _derived("D" if len(members) <= 2 else "G", "conversation", *members)


def _ref(kind: EntityKind, external_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=kind, external_id=external_id)


def team_ref() -> EntityRef:
    """What a listing or a search of the whole workspace reads."""
    return _ref(EntityKind.RECORD, TEAM_ID)


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


def install_ref() -> EntityRef:
    return _ref(EntityKind.RECORD, "install")


def fault_ref(position: int) -> EntityRef:
    return _ref(EntityKind.RECORD, f"fault.{position}")


def sign_in_ref(token: str) -> EntityRef:
    """Where a token the scenario declares is kept: under its digest, never the token itself."""
    return _ref(EntityKind.RECORD, "sign_in." + hashlib.sha256(f"token|{token}".encode()).hexdigest())


SIGN_INS = "sign_ins"


FILES = "files"
POSTS = "posts"
VIEWS = "views"
TRIGGERS = "triggers"
HOOKS = "hooks"
INTERACTIONS = "interactions"
COMMANDS = "commands"
APP = "app"
FAULTS = "faults"


def file_id(seed: str) -> str:
    return _derived("F", "file", seed)


def url_private(file: str, name: str) -> str:
    return f"https://{FILES_HOST}/files-pri/{TEAM_ID}-{file}/{name}"


def url_private_download(file: str, name: str) -> str:
    return f"https://{FILES_HOST}/files-pri/{TEAM_ID}-{file}/download/{name}"


def response_url(hook: str, secret: str, *, command: bool) -> str:
    return f"https://{HOOKS_HOST}/{'commands' if command else 'actions'}/{TEAM_ID}/{hook}/{secret}"


def bot_profile(updated: int) -> wire.BotProfile:
    """The `bot_profile` Slack attaches to every message the app posts."""
    icon = "https://a.slack-edge.com/80588/img/plugins/app/bot_36.png"
    return wire.BotProfile(
        id=BOT_ID,
        app_id=APP_ID,
        name=BOT_NAME,
        icons=wire.BotIcons(image_36=icon, image_48=icon.replace("36", "48"), image_72=icon.replace("36", "72")),
        updated=updated,
        team_id=TEAM_ID,
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


class SlackWorld:
    """Typed reads and writes of one run's Slack entities."""

    def __init__(self, store: Store) -> None:
        self._store = store

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
        if stored is None or stored.parent != TEAM_ID:
            return None
        return wire.parse(wire.SlackUser, stored.body)

    def users(self, *, after: str | None, limit: int) -> list[wire.SlackUser]:
        page = self._store.children(MANIFEST.key, EntityKind.RECORD, TEAM_ID, after=after, limit=limit)
        return [wire.parse(wire.SlackUser, s.body) for s in page]

    def every_user(self) -> Iterator[wire.SlackUser]:
        for stored in self._pages(EntityKind.RECORD, TEAM_ID):
            yield wire.parse(wire.SlackUser, stored.body)

    def channel(self, channel: str) -> wire.SlackChannel | None:
        stored = self._store.get(channel_ref(channel))
        return None if stored is None else wire.parse(wire.SlackChannel, stored.body)

    def channels_after(self, after: str | None) -> Iterator[wire.SlackChannel]:
        for stored in self._pages(EntityKind.CHANNEL, TEAM_ID, after):
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
            if user is None or user.is_bot or member == besides or user.profile.email is None:
                continue
            emails.append(user.profile.email)
        return emails

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
        """A `ts` at `second` (a seeded message's past moment), unique by the next event's sequence."""
        seq = self.next_seq()
        if seq > _MAX_SEQ_IN_TS:
            raise OverflowError(f"event {seq} no longer fits the six digits of a Slack ts")
        return f"{second}.{seq:06d}"

    def next_seq(self) -> int:
        return self._store.head() + 1

    def body(self, ref: EntityRef, model: type[StoredBody]) -> StoredBody | None:
        stored = self._store.get(ref)
        return None if stored is None else wire.parse(model, stored.body)

    def bodies(self, kind: EntityKind, parent: str, model: type[StoredBody]) -> list[StoredBody]:
        return [wire.parse(model, s.body) for s in self._pages(kind, parent)]

    def post(self, key: str) -> wire.SlackPostKey | None:
        return self.body(post_ref(key), wire.SlackPostKey)

    def knows_token(self, token: str) -> bool:
        """Whether the workspace issued this token: any token when the scenario declares no Slack sign-in, and
        once it declares one, only the tokens it names."""
        if self._store.get(sign_in_ref(token)) is not None:
            return True
        return not self._store.children(MANIFEST.key, EntityKind.RECORD, SIGN_INS, after=None, limit=1)

    def file(self, file: str) -> wire.SlackFile | None:
        stored = self._store.get(file_ref(file))
        return None if stored is None or stored.parent != TEAM_ID else wire.parse(wire.SlackFile, stored.body)

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

    def delete(self, ref: EntityRef, *, actor: Actor, parent: str) -> WorldEvent:
        return self._store.apply(Change(entity=ref, operation=Operation.DELETE, actor=actor, parent=parent))

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))

    def open_conversation(self, members: list[str], *, created: int, actor: Actor) -> wire.SlackChannel:
        """Write an IM (the app and one other) or a group DM, and a membership for each member."""
        cid = conversation_id(members)
        others = [m for m in members if m != BOT_USER_ID]
        if len(set(members)) <= 2:
            channel = wire.SlackChannel(
                id=cid,
                is_im=True,
                is_private=True,
                created=created,
                creator=BOT_USER_ID,
                user=others[0] if others else BOT_USER_ID,
            )
        else:
            channel = wire.SlackChannel(
                id=cid,
                name="mpdm-" + "--".join(sorted(members)) + "-1",
                is_mpim=True,
                is_group=True,
                is_private=True,
                created=created,
                creator=BOT_USER_ID,
            )
        self.write(channel_ref(cid), channel, operation=Operation.CREATE, actor=actor, parent=TEAM_ID)
        for member in sorted(set(members)):
            self.write(
                membership_ref(cid, member),
                wire.SlackMembership(channel=cid, user=member),
                operation=Operation.CREATE,
                actor=actor,
                parent=cid,
            )
        return channel
