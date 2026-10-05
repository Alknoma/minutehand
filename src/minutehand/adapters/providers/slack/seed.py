"""The workspace a scenario starts in: its people, the agent's bot user, `#general`, an IM with each person who can
be messaged, the channels and history the scenario seeds, who installed the app, and the faults it declares."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.state import BOT_ID, BOT_USER_ID, SlackWorld
from minutehand.domain.scenario import (
    Account,
    Person,
    RateLimited,
    Scenario,
    SeededChannel,
    SeededFile,
    SeededPost,
)
from minutehand.domain.world import Actor, DocumentSnapshot, MessageSnapshot, Operation
from minutehand.ports.store import Store

REACHABLE = (Account.MEMBER, Account.GUEST)
"""Who the agent can open a DM with: a bot cannot be DMed and a deactivated account cannot be reached."""


def _member(person: Person, at: datetime) -> wire.SlackUser:
    tz = person.working_hours.timezone if person.working_hours is not None else "UTC"
    offset = ZoneInfo(tz).utcoffset(at)
    bot = person.account is Account.BOT
    return wire.SlackUser(
        id=state.user_id(person.key),
        team_id=state.TEAM_ID,
        name=person.key,
        real_name=person.name,
        deleted=person.account is Account.DEACTIVATED,
        is_restricted=person.account is Account.GUEST,
        is_bot=bot,
        tz=tz,
        tz_offset=int(offset.total_seconds()) if offset is not None else 0,
        profile=wire.SlackProfile(
            real_name=person.name,
            display_name=person.name,
            email=None if bot else person.email,
            title=person.title or "",
            bot_id=state.other_bot_id(person.key) if bot else None,
        ),
    )


def _bot() -> wire.SlackUser:
    return wire.SlackUser(
        id=BOT_USER_ID,
        team_id=state.TEAM_ID,
        name=state.BOT_NAME,
        real_name=state.BOT_NAME,
        is_bot=True,
        profile=wire.SlackProfile(real_name=state.BOT_NAME, display_name=state.BOT_NAME, bot_id=BOT_ID),
    )


def seed(scenario: Scenario, world: Store) -> None:
    slack = SlackWorld(world)
    created = int(scenario.starts_at.timestamp())
    users = [_bot(), *(_member(p, scenario.starts_at) for p in scenario.people)]
    for user in users:
        slack.write(
            state.user_ref(user.id), user, operation=Operation.CREATE, actor=Actor.SCENARIO, parent=state.TEAM_ID
        )
    slack.write(
        state.install_ref(),
        wire.SlackInstall(installer=state.user_id(scenario.owner)),
        operation=Operation.CREATE,
        actor=Actor.SCENARIO,
        parent=state.APP,
    )

    general = wire.SlackChannel(
        id=state.named_channel_id(state.GENERAL),
        name=state.GENERAL,
        is_channel=True,
        is_general=True,
        created=created,
        creator=BOT_USER_ID,
    )
    slack.write(
        state.channel_ref(general.id), general, operation=Operation.CREATE, actor=Actor.SCENARIO, parent=state.TEAM_ID
    )
    for member in [BOT_USER_ID, *(state.user_id(p.key) for p in scenario.people if p.account is Account.MEMBER)]:
        _join(slack, general.id, member)
    for person in (p for p in scenario.people if p.account in REACHABLE):
        slack.open_conversation([BOT_USER_ID, state.user_id(person.key)], created=created, actor=Actor.SCENARIO)

    for channel in (c for c in scenario.channels if c.provider == MANIFEST.key):
        _channel(slack, channel, scenario, created)
    for position, fault in enumerate(scenario.faults):
        if fault.provider != MANIFEST.key:
            continue
        answer = fault.answer
        limited = answer if isinstance(answer, RateLimited) else None
        slack.write(
            state.fault_ref(position),
            wire.SlackFault(
                position=position,
                call=fault.call,
                error="ratelimited" if isinstance(answer, RateLimited) else answer.error,
                retry_after=max(1, int(limited.retry_after.total_seconds())) if limited is not None else None,
                remaining=fault.times,
                from_time=int((scenario.starts_at + fault.after).timestamp()),
                only_rich=fault.only_rich,
            ),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=state.FAULTS,
        )


def _join(slack: SlackWorld, channel: str, user: str) -> None:
    slack.write(
        state.membership_ref(channel, user),
        wire.SlackMembership(channel=channel, user=user),
        operation=Operation.CREATE,
        actor=Actor.SCENARIO,
        parent=channel,
    )


def _channel(slack: SlackWorld, seeded: SeededChannel, scenario: Scenario, created: int) -> None:
    members = [state.user_id(k) for k in seeded.members]
    if seeded.name is None:
        cid = state.conversation_id([BOT_USER_ID, *members])
        if slack.channel(cid) is None:
            slack.open_conversation([BOT_USER_ID, *members], created=created, actor=Actor.SCENARIO)
    else:
        creator = members[0] if members else BOT_USER_ID
        channel = wire.SlackChannel(
            id=state.named_channel_id(seeded.name),
            name=seeded.name,
            is_channel=not seeded.private,
            is_group=seeded.private,
            is_private=seeded.private,
            is_archived=seeded.archived,
            created=created,
            creator=creator,
            topic=wire.SlackTopic(value=seeded.topic, creator=creator, last_set=created) if seeded.topic else None,
            purpose=wire.SlackTopic(value=seeded.purpose, creator=creator, last_set=created)
            if seeded.purpose
            else None,
        )
        cid = channel.id
        slack.write(
            state.channel_ref(cid), channel, operation=Operation.CREATE, actor=Actor.SCENARIO, parent=state.TEAM_ID
        )
        for member in sorted(set(members + ([BOT_USER_ID] if seeded.agent_member else []))):
            _join(slack, cid, member)
    for post in sorted(seeded.history, key=lambda p: p.ago, reverse=True):
        root = _post(slack, cid, post, scenario, None)
        for reply in sorted(post.replies, key=lambda p: p.ago, reverse=True):
            _post(slack, cid, reply, scenario, root)


def _post(slack: SlackWorld, channel: str, post: SeededPost, scenario: Scenario, thread_ts: str | None) -> str:
    """A seeded message, as its author wrote it before the run began, with its files."""
    at = int((scenario.starts_at - post.ago).timestamp())
    author = state.user_id(post.by)
    files = [
        write_file(slack, f, author, at, seed=f"{channel}|{post.ago}|{i}", actor=Actor.SCENARIO)
        for i, f in enumerate(post.files)
    ]
    ts = write_post(slack, channel, author, post.text, thread_ts, files, at=at, actor=Actor.SCENARIO)
    if post.key is not None:
        remember_post(slack, post.key, channel, ts, actor=Actor.SCENARIO)
    return ts


def write_post(
    slack: SlackWorld,
    channel: str,
    author: str,
    text: str,
    thread_ts: str | None,
    files: list[wire.SlackFile],
    *,
    at: int,
    actor: Actor,
) -> str:
    """A person's message in the store, as Slack keeps it, recorded as reaching the channel's other humans."""
    ts = slack.ts_at(at)
    slack.write(
        state.message_ref(ts),
        person_message(ts, author, text, thread_ts, files),
        operation=Operation.CREATE,
        actor=actor,
        parent=channel,
        after=MessageSnapshot(
            text=text,
            channel=channel,
            recipient_emails=slack.human_emails(channel, besides=author),
            thread_of=thread_ts,
        ),
    )
    return ts


def person_message(
    ts: str, author: str, text: str, thread_ts: str | None, files: list[wire.SlackFile]
) -> wire.SlackMessage:
    return wire.SlackMessage(
        ts=ts,
        user=author,
        text=text,
        team=state.TEAM_ID,
        thread_ts=thread_ts,
        client_msg_id=state.client_msg_id(ts),
        subtype="file_share" if files else None,
        files=files or None,
        upload=False if files else None,
    )


def remember_post(slack: SlackWorld, key: str, channel: str, ts: str, *, actor: Actor) -> None:
    slack.write(
        state.post_ref(key),
        wire.SlackPostKey(key=key, channel=channel, ts=ts),
        operation=Operation.CREATE,
        actor=actor,
        parent=state.POSTS,
    )


def write_file(
    slack: SlackWorld, seeded: SeededFile, author: str, at: int, *, seed: str, actor: Actor
) -> wire.SlackFile:
    """A file and its content in the store, as `author` uploaded it at `at`; answers the file as Slack serves it."""
    file = state.file_id(seed)
    extension = seeded.name.rsplit(".", 1)[-1].lower() if "." in seeded.name else "text"
    served = wire.SlackFile(
        id=file,
        created=at,
        timestamp=at,
        name=seeded.name,
        title=seeded.title or seeded.name,
        mimetype=seeded.mime_type,
        filetype=extension,
        pretty_type=extension.upper(),
        user=author,
        user_team=state.TEAM_ID,
        size=len(seeded.text.encode()),
        is_public=False,
        url_private=state.url_private(file, seeded.name),
        url_private_download=state.url_private_download(file, seeded.name),
        permalink=f"https://{state.TEAM_DOMAIN}.slack.com/files/{author}/{file}/{seeded.name}",
    )
    slack.write(
        state.file_ref(file),
        served,
        operation=Operation.CREATE,
        actor=actor,
        parent=state.TEAM_ID,
        after=DocumentSnapshot(title=served.title, mime_type=served.mimetype),
    )
    slack.write(
        state.content_ref(file),
        wire.SlackFileContent(file=file, mimetype=seeded.mime_type, text=seeded.text),
        operation=Operation.CREATE,
        actor=actor,
        parent=state.FILES,
    )
    return served
