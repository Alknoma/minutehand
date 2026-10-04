"""The Slack workspace as entities in the run's store.

| Slack thing | `EntityKind` | external id | parent |
|---|---|---|---|
| user        | RECORD   | user id                     | the team |
| channel, IM | CHANNEL  | channel id                  | the team |
| membership  | RECORD   | `<channel>.<user>`          | the channel |
| message     | MESSAGE  | its `ts`, unique in the run | the channel |

Nothing here is held between calls: every read is a query of the store, so a new
app over the same store sees the same workspace, and a fork sees it as of the fork.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

from minutehand.adapters.providers.slack import wire
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, Snapshot, Stored, WorldEvent
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

TEAM_ID = "T0WORKSPACE"
TEAM_NAME = "Simulated Workspace"
BOT_USER_ID = "U0AGENTBOT"
BOT_ID = "B0AGENTBOT"
APP_ID = "A0AGENTAPP"
BOT_NAME = "agent"
GENERAL = "general"

_SCAN = 1000
_MAX_SEQ_IN_TS = 999_999


def _derived(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:10].upper()


def user_id(person_key: str) -> str:
    """A person's member id: the same for the same `Person.key` in every run."""
    return _derived("U", "person", person_key)


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
        """Every live message in the channel, roots and replies, oldest first."""
        return [wire.parse(wire.SlackMessage, s.body) for s in self._pages(EntityKind.MESSAGE, channel)]

    def message(self, channel: str, ts: str) -> wire.SlackMessage | None:
        stored = self._store.get(message_ref(ts))
        if stored is None or stored.parent != channel:
            return None
        return wire.parse(wire.SlackMessage, stored.body)

    def located(self, ts: str) -> tuple[str, wire.SlackMessage] | None:
        """A message and the channel it is in, from its ts alone."""
        stored = self._store.get(message_ref(ts))
        if stored is None or stored.parent is None:
            return None
        return stored.parent, wire.parse(wire.SlackMessage, stored.body)

    def next_ts(self, clock: Clock) -> str:
        """A Slack `ts`: the clock's second, and the sequence of the event about to be written.

        Two messages in one simulated instant still get distinct, ordered stamps,
        because no two events share a sequence number.
        """
        seq = self._store.head() + 1
        if seq > _MAX_SEQ_IN_TS:
            raise OverflowError(f"event {seq} no longer fits the six digits of a Slack ts")
        return f"{int(clock.now().timestamp())}.{seq:06d}"

    # ------------------------------------------------------------------ writes

    def write(
        self,
        ref: EntityRef,
        body: wire.SlackUser | wire.SlackChannel | wire.SlackMembership | wire.SlackMessage,
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
                id=cid, is_im=True, is_private=True, created=created, creator=BOT_USER_ID,
                user=others[0] if others else BOT_USER_ID,
            )
        else:
            channel = wire.SlackChannel(
                id=cid, name="mpdm-" + "--".join(sorted(members)) + "-1", is_mpim=True, is_group=True,
                is_private=True, created=created, creator=BOT_USER_ID,
            )
        self.write(channel_ref(cid), channel, operation=Operation.CREATE, actor=actor, parent=TEAM_ID)
        for member in sorted(set(members)):
            self.write(
                membership_ref(cid, member), wire.SlackMembership(channel=cid, user=member),
                operation=Operation.CREATE, actor=actor, parent=cid,
            )
        return channel
