"""What a person does, put into the workspace and pushed to the agent as Slack's Events API pushes it.

A person writes back to something the agent sent (`deliver`), writes to the agent's bot unprompted (`say`), or does
what the scenario has them do at a set moment (`happen`): post in a channel or a DM, @-mention the agent, share a
file, edit or delete what they wrote, react, join a channel, open the agent's Home tab, run a slash command. Each is
recorded as actor PERSON. Every event goes to the agent only where Slack would send it: a conversation the agent's
bot is in, or the bot's own DM and Home tab.

The world stamps from the run's clock: a message's `ts` and the callback's `event_time` are simulated.
`X-Slack-Request-Timestamp` is not world time. It is the moment the request is sent, and the agent's
`SignatureVerifier` refuses any timestamp more than five minutes from its own clock, which is the machine's.

An event the agent does not answer with 2xx is sent again, as Slack does, up to three more times with
`X-Slack-Retry-Num` and `X-Slack-Retry-Reason`; the retries are not spaced out, since no simulated time passes
while the agent is being called. A delivery still refused after the last retry fails the agent.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import httpx

from minutehand.adapters.providers.slack import seed, socket_mode, state, wire
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.application.refusals import AgentFailed
from minutehand.domain.people import Delivery, InboundTarget, PersonMessage, PersonReply
from minutehand.domain.scenario import (
    MessagingHappening,
    PersonAddsAgent,
    PersonCommands,
    PersonDeletes,
    PersonEdits,
    PersonJoins,
    PersonOpensAgent,
    PersonPosts,
    PersonReacts,
)
from minutehand.domain.world import Actor, Change, EntityKind, MessageSnapshot, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

RETRIES = 3
"""How many more times Slack sends an event the agent did not acknowledge."""
TIMEOUT = 30.0


class DeliveryRefused(AgentFailed):
    """The agent answered a pushed event with something other than 2xx, or could not be reached for it."""

    def __init__(self, url: str, status: int | None, body: str, *, what: str = "a Slack event") -> None:
        answered = f"answered {status}" if status is not None else "could not be reached"
        super().__init__(f"{url} {answered} to {what}: {body[:200]}")
        self.status = status


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """`X-Slack-Signature`: v0= and the HMAC-SHA256 of `v0:<timestamp>:<body>` under the signing secret."""
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def refuse_foreign(target: InboundTarget) -> None:
    if target.provider != MANIFEST.key:
        raise ValueError(f"a {target.provider} target is not Slack's to deliver to")


async def post_signed(
    url: str, body: bytes, content_type: str, secret: str, *, retry: int | None = None, reason: str | None = None
) -> httpx.Response:
    """One signed request from Slack to the agent; raises only when it could not be made."""
    stamp = str(int(time.time()))  # clock-lint: exempt the request's send time, checked against the agent's own clock
    headers = {
        "Content-Type": content_type,
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": sign(secret, stamp, body),
        "User-Agent": "Slackbot 1.0 (+https://api.slack.com/robots)",
    }
    if retry is not None and reason is not None:
        headers["X-Slack-Retry-Num"] = str(retry)
        headers["X-Slack-Retry-Reason"] = reason
    async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
        return await client.post(url, content=body, headers=headers)


async def push_event(slack: SlackWorld, target: InboundTarget, callback: wire.EventCallback, secret: str) -> None:
    """An Events API callback, sent again on failure as Slack sends it, until the agent acknowledges it: a signed
    request to the target's URL, or, for a target in Socket Mode, an envelope on the connection the agent holds open
    (`socket_mode`)."""
    if target.delivery is Delivery.SOCKET_MODE:
        await socket_mode.hub(slack.store).push(callback)
        return
    body = wire.event_body(callback)
    url = target.request_url()
    last: str = ""
    status: int | None = None
    for attempt in range(RETRIES + 1):
        reason = None if attempt == 0 else ("http_timeout" if status is None else "http_error")
        try:
            answered = await post_signed(url, body, "application/json", secret, retry=attempt or None, reason=reason)
        except httpx.TimeoutException as e:
            status, last = None, repr(e)
            continue
        except httpx.HTTPError as e:
            raise DeliveryRefused(url, None, repr(e)) from e
        if answered.is_success:
            return
        status, last = answered.status_code, answered.text
    raise DeliveryRefused(url, status, last)


def callback(
    slack: SlackWorld, event: wire.Event, *, seq: int, clock: Clock, second: bool = False
) -> wire.EventCallback:
    """The `event_callback` envelope, from the workspace `slack` is. `event_id` is fixed by the world event the push
    reports; a second push for the same event (the `app_mention` beside a `message`) gets an id of its own."""
    return wire.EventCallback(
        team_id=slack.team.id,
        api_app_id=slack.team.app_id,
        event_id=f"Ev{seq:010d}{'M' if second else ''}",
        event_time=int(clock.now().timestamp()),
        authorizations=[wire.Authorization(team_id=slack.team.id, user_id=slack.bot)],
        event=event,
    )


def acting(world: Store, person: str, channel: str | None = None) -> SlackWorld:
    """The workspace a person acts in: of the workspaces they belong to, the first that has `channel` with them in
    it when one is named, else the first."""
    every = SlackWorld(world)
    theirs = [
        every.as_team(w) for w in every.workspaces() if every.as_team(w).user(state.user_id(person, w.id)) is not None
    ]
    if not theirs:
        raise LookupError(f"{person} is not a member of any workspace")
    if channel is not None:
        named = [
            w
            for w in theirs
            if w.is_member(state.named_channel_id(channel, w.team.id), state.user_id(person, w.team.id))
        ]
        if named:
            return named[0]
    return theirs[0]


def where(world: Store, channel: str) -> SlackWorld:
    """The workspace a channel or conversation is in."""
    found = SlackWorld(world).channel_team(channel)
    if found is None:
        raise LookupError(f"no Slack conversation {channel} in any workspace")
    return found


async def verify_url(target: InboundTarget, secret: str, challenge: str) -> None:
    """Slack's `url_verification`, as it is sent when an app's request URL is set: the agent must echo the challenge,
    as JSON or as plain text. Nothing in a run sends it; a check of an agent's endpoint does."""
    refuse_foreign(target)
    url = target.request_url()
    body = wire.event_body(wire.UrlVerification(token=wire.VERIFICATION_TOKEN, challenge=challenge))
    try:
        answered = await post_signed(url, body, "application/json", secret)
    except httpx.HTTPError as e:
        raise DeliveryRefused(url, None, repr(e), what="url_verification") from e
    if not answered.is_success:
        raise DeliveryRefused(url, answered.status_code, answered.text, what="url_verification")
    echoed = answered.text.strip()
    if echoed != challenge:
        try:
            echoed = wire.Challenged.model_validate_json(answered.content).challenge
        except ValueError:
            echoed = ""
    if echoed != challenge:
        raise DeliveryRefused(url, answered.status_code, answered.text, what="url_verification's challenge")


# --------------------------------------------------------------------------- replies and messages


async def deliver(reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str) -> None:
    refuse_foreign(target)
    if reply.press is not None:
        raise ValueError(f"{reply.person}'s reply presses {reply.press.label!r}; a press is not delivered as a message")
    if reply.in_reply_to.provider != MANIFEST.key:
        raise ValueError(f"a {reply.in_reply_to.provider} message is not Slack's to answer")
    if reply.in_reply_to.kind is not EntityKind.MESSAGE:
        raise ValueError(f"a reply answers a message, not a {reply.in_reply_to.kind}")
    found = SlackWorld(world).located(reply.in_reply_to.external_id)
    if found is None:
        raise LookupError(f"no Slack message {reply.in_reply_to.external_id} for {reply.person} to answer")
    channel_id, asked = found
    slack = where(world, channel_id)
    channel = slack.channel(channel_id)
    author = state.user_id(reply.person, slack.team.id)
    if channel is None or not slack.is_member(channel_id, author):
        raise LookupError(f"{reply.person} is not in the conversation {channel_id} they are answering")
    # In an IM a reply is a new message; anywhere else it goes in the thread of what it answers. An ephemeral
    # message has no thread of its own: the answer is posted where it was shown.
    thread_ts = (
        asked.thread_ts if asked.thread_ts is not None or channel.is_im or asked.ephemeral_to is not None else asked.ts
    )
    await _post(slack, channel, author, reply.text, thread_ts, [], False, target, clock, secret)


async def say(message: PersonMessage, target: InboundTarget, world: Store, clock: Clock, *, secret: str) -> None:
    """The person DMs the agent's bot: a new message in their IM with it, never in a thread."""
    refuse_foreign(target)
    slack = acting(world, message.person)
    author = state.user_id(message.person, slack.team.id)
    channel = slack.channel(state.conversation_id([slack.bot, author]))
    if channel is None:
        raise LookupError(f"{message.person} has no DM with the agent's bot; the workspace was not seeded for them")
    await _post(slack, channel, author, message.text, None, [], False, target, clock, secret)


async def _post(
    slack: SlackWorld,
    channel: wire.SlackChannel,
    author: str,
    text: str,
    thread_ts: str | None,
    files: list[wire.SlackFile],
    mentions: bool,
    target: InboundTarget,
    clock: Clock,
    secret: str,
) -> str:
    """Write the person's message and push it: `message`, and `app_mention` too when it names the agent outside
    a DM. The agent hears neither from a channel its bot is not in."""
    ts = seed.write_post(
        slack, channel.id, author, text, thread_ts, files, at=int(clock.now().timestamp()), actor=Actor.PERSON
    )
    if not slack.is_member(channel.id, slack.bot):
        return ts
    event = wire.MessageEvent(
        subtype="file_share" if files else None,
        channel=channel.id,
        user=author,
        text=text,
        ts=ts,
        event_ts=ts,
        channel_type=wire.event_channel_type(channel),
        team=slack.team.id,
        client_msg_id=state.client_msg_id(ts),
        thread_ts=thread_ts,
        files=files or None,
        upload=False if files else None,
    )
    await push_event(slack, target, callback(slack, event, seq=slack.next_seq() - 1, clock=clock), secret)
    if mentions and not channel.is_im:
        mention = wire.AppMentionEvent(
            user=author,
            text=text,
            ts=ts,
            channel=channel.id,
            event_ts=ts,
            team=slack.team.id,
            client_msg_id=state.client_msg_id(ts),
            thread_ts=thread_ts,
            files=files or None,
        )
        await push_event(
            slack, target, callback(slack, mention, seq=slack.next_seq() - 1, clock=clock, second=True), secret
        )
    return ts


# --------------------------------------------------------------------------- happenings


async def happen(
    happening: MessagingHappening, target: InboundTarget, world: Store, clock: Clock, *, secret: str
) -> None:
    refuse_foreign(target)
    if happening.provider != MANIFEST.key:
        raise ValueError(f"a {happening.provider} happening is not Slack's")
    slack = _acts_in(world, happening)
    author = state.user_id(happening.person, slack.team.id)
    if isinstance(happening, PersonPosts):
        await _posts(slack, happening, author, target, clock, secret)
    elif isinstance(happening, PersonEdits):
        await _edits(slack, happening, author, target, clock, secret)
    elif isinstance(happening, PersonDeletes):
        await _deletes(slack, happening, author, target, clock, secret)
    elif isinstance(happening, PersonReacts):
        await _reacts(slack, happening, author, target, clock, secret)
    elif isinstance(happening, PersonJoins):
        await _joins(slack, happening, author, target, clock, secret)
    elif isinstance(happening, PersonAddsAgent):
        await _adds_agent(slack, happening, author, target, clock, secret)
    elif isinstance(happening, PersonOpensAgent):
        await _opens_home(slack, world, author, target, clock, secret)
    else:
        assert isinstance(happening, PersonCommands)
        raise ValueError("a slash command is pushed as an interaction (`interactive.command`), not as an event")


def _acts_in(world: Store, happening: MessagingHappening) -> SlackWorld:
    """The workspace a happening is done in: where the post it changes is, else where the channel it names is."""
    post: str | None = None
    if isinstance(happening, PersonEdits | PersonDeletes | PersonReacts):
        post = happening.post
    if post is not None:
        posted = SlackWorld(world).post(post)
        if posted is None:
            raise LookupError(f"post {post!r} is not in any workspace")
        return where(world, posted.channel)
    channel: str | None = None
    if isinstance(happening, PersonPosts | PersonReacts | PersonCommands | PersonAddsAgent | PersonJoins):
        channel = happening.channel
    return acting(world, happening.person, channel)


def conversation(slack: SlackWorld, name: str | None, author: str) -> wire.SlackChannel:
    """A channel by its name, or with none, the person's DM with the agent's bot; the person must be in it."""
    cid = state.conversation_id([slack.bot, author]) if name is None else state.named_channel_id(name, slack.team.id)
    found = slack.channel(cid)
    where = "their DM with the agent" if name is None else f"#{name}"
    if found is None:
        raise LookupError(f"there is no {where} in the workspace")
    if not slack.is_member(cid, author):
        raise LookupError(f"{author} is not in {where}")
    return found


def _posted(slack: SlackWorld, key: str) -> tuple[wire.SlackChannel, wire.SlackMessage]:
    found = slack.post(key)
    located = slack.located(found.ts) if found is not None else None
    channel = slack.channel(located[0]) if located is not None else None
    if located is None or channel is None:
        raise LookupError(f"post {key!r} is not in the workspace any more")
    return channel, located[1]


async def _posts(
    slack: SlackWorld, posts: PersonPosts, author: str, target: InboundTarget, clock: Clock, secret: str
) -> None:
    channel = conversation(slack, posts.channel, author)
    thread_ts: str | None = None
    if posts.in_thread_of is not None:
        root_channel, root = _posted(slack, posts.in_thread_of)
        if root_channel.id != channel.id:
            raise LookupError(f"post {posts.in_thread_of!r} is not in the channel {posts.person} posts in")
        thread_ts = root.thread_ts or root.ts
    at = int(clock.now().timestamp())
    files = [
        seed.write_file(slack, f, author, at, seed=f"{slack.next_seq()}|{i}", actor=Actor.PERSON)
        for i, f in enumerate(posts.files)
    ]
    text = f"<@{slack.bot}> {posts.text}" if posts.mentions_agent else posts.text
    ts = await _post(slack, channel, author, text, thread_ts, files, posts.mentions_agent, target, clock, secret)
    if posts.key is not None:
        seed.remember_post(slack, posts.key, channel.id, ts, actor=Actor.PERSON)


def _own(message: wire.SlackMessage, author: str, key: str) -> None:
    if message.user != author:
        raise LookupError(f"post {key!r} is not theirs to change")


async def _edits(
    slack: SlackWorld, edits: PersonEdits, author: str, target: InboundTarget, clock: Clock, secret: str
) -> None:
    channel, before = _posted(slack, edits.post)
    _own(before, author, edits.post)
    stamp = slack.next_ts(clock)
    after = before.model_copy(update={"text": edits.text, "edited": wire.SlackEdited(user=author, ts=stamp)})
    slack.write(
        state.message_ref(before.ts),
        after,
        operation=Operation.UPDATE,
        actor=Actor.PERSON,
        parent=channel.id,
        after=MessageSnapshot(
            text=edits.text,
            channel=channel.id,
            recipient_emails=slack.human_emails(channel.id, besides=author),
            thread_of=before.thread_ts,
        ),
    )
    if slack.is_member(channel.id, slack.bot):
        event = wire.MessageChangedEvent(
            channel=channel.id,
            channel_type=wire.event_channel_type(channel),
            ts=stamp,
            event_ts=stamp,
            message=after,
            previous_message=before,
        )
        await push_event(slack, target, callback(slack, event, seq=slack.next_seq() - 1, clock=clock), secret)


async def _deletes(
    slack: SlackWorld, deletes: PersonDeletes, author: str, target: InboundTarget, clock: Clock, secret: str
) -> None:
    channel, before = _posted(slack, deletes.post)
    _own(before, author, deletes.post)
    stamp = slack.next_ts(clock)
    slack.delete(
        state.message_ref(before.ts),
        actor=Actor.PERSON,
        parent=channel.id,
        before=MessageSnapshot(
            text=before.text,
            channel=channel.id,
            recipient_emails=slack.human_emails(channel.id, besides=author),
            thread_of=before.thread_ts,
        ),
    )
    if slack.is_member(channel.id, slack.bot):
        event = wire.MessageDeletedEvent(
            channel=channel.id,
            channel_type=wire.event_channel_type(channel),
            ts=stamp,
            deleted_ts=before.ts,
            event_ts=stamp,
            previous_message=before,
        )
        await push_event(slack, target, callback(slack, event, seq=slack.next_seq() - 1, clock=clock), secret)


async def _reacts(
    slack: SlackWorld, reacts: PersonReacts, author: str, target: InboundTarget, clock: Clock, secret: str
) -> None:
    if reacts.post is not None:
        channel, message = _posted(slack, reacts.post)
    else:
        channel = conversation(slack, reacts.channel, author)
        mine = [m for m in slack.messages(channel.id) if m.user == slack.bot]
        if not mine:
            raise LookupError(f"the agent has written nothing in {channel.id} for {reacts.person} to react to")
        message = mine[-1]
    reactions = list(message.reactions or [])
    same = next((r for r in reactions if r.name == reacts.reaction), None)
    if same is not None and author in same.users:
        raise LookupError(f"{reacts.person} has already reacted :{reacts.reaction}: there")
    if same is None:
        reactions.append(wire.SlackReaction(name=reacts.reaction, users=[author], count=1))
    else:
        reactions[reactions.index(same)] = wire.SlackReaction(
            name=same.name, users=[*same.users, author], count=same.count + 1
        )
    stamp = slack.next_ts(clock)
    slack.write(
        state.message_ref(message.ts),
        message.model_copy(update={"reactions": reactions}),
        operation=Operation.UPDATE,
        actor=Actor.PERSON,
        parent=channel.id,
    )
    if slack.is_member(channel.id, slack.bot):
        event = wire.ReactionAddedEvent(
            user=author,
            reaction=reacts.reaction,
            item_user=message.user,
            item=wire.ReactionItem(channel=channel.id, ts=message.ts),
            event_ts=stamp,
        )
        await push_event(slack, target, callback(slack, event, seq=slack.next_seq() - 1, clock=clock), secret)


async def _joins(
    slack: SlackWorld, joins: PersonJoins, author: str, target: InboundTarget, clock: Clock, secret: str
) -> None:
    cid = state.named_channel_id(joins.channel, slack.team.id)
    channel = slack.channel(cid)
    if channel is None:
        raise LookupError(f"there is no #{joins.channel} in the workspace")
    if slack.is_member(cid, author):
        raise LookupError(f"{joins.person} is already in #{joins.channel}")
    if channel.is_private:
        raise LookupError(f"#{joins.channel} is private; {joins.person} cannot join it without an invitation")
    stamp = slack.next_ts(clock)
    slack.write(
        state.membership_ref(cid, author),
        wire.SlackMembership(channel=cid, user=author),
        operation=Operation.CREATE,
        actor=Actor.PERSON,
        parent=cid,
    )
    if slack.is_member(cid, slack.bot):
        event = wire.MemberJoinedEvent(
            user=author,
            channel=cid,
            channel_type="G" if channel.is_private else "C",
            team=slack.team.id,
            event_ts=stamp,
        )
        await push_event(slack, target, callback(slack, event, seq=slack.next_seq() - 1, clock=clock), secret)


async def _adds_agent(
    slack: SlackWorld, adds: PersonAddsAgent, author: str, target: InboundTarget, clock: Clock, secret: str
) -> None:
    """The person invites the agent's bot to a channel they are in: Slack tells the bot it joined, naming who
    invited it. A bot is never invited to a DM, so a happening with no channel is refused."""
    if adds.channel is None:
        raise ValueError("a Slack bot is invited to a channel; a DM with it exists already, so name a channel")
    cid = state.named_channel_id(adds.channel, slack.team.id)
    channel = conversation(slack, adds.channel, author)
    if slack.is_member(cid, slack.bot):
        raise LookupError(f"the agent is already in #{adds.channel}")
    stamp = slack.next_ts(clock)
    slack.write(
        state.membership_ref(cid, slack.bot),
        wire.SlackMembership(channel=cid, user=slack.bot),
        operation=Operation.CREATE,
        actor=Actor.PERSON,
        parent=cid,
    )
    event = wire.MemberJoinedEvent(
        user=slack.bot,
        channel=cid,
        channel_type="G" if channel.is_private else "C",
        team=slack.team.id,
        inviter=author,
        event_ts=stamp,
    )
    await push_event(slack, target, callback(slack, event, seq=slack.next_seq() - 1, clock=clock), secret)


async def _opens_home(
    slack: SlackWorld, world: Store, author: str, target: InboundTarget, clock: Clock, secret: str
) -> None:
    dm = conversation(slack, None, author)
    stamp = slack.next_ts(clock)
    home = slack.body(state.view_ref(state.home_view_id(author)), wire.OpenView)
    world.apply(Change(entity=state.user_ref(author), operation=Operation.READ, actor=Actor.PERSON))
    event = wire.AppHomeOpenedEvent(
        user=author, channel=dm.id, event_ts=stamp, view=home.view if home is not None else None
    )
    await push_event(slack, target, callback(slack, event, seq=slack.next_seq() - 1, clock=clock), secret)
