"""What a person writes, put into the workspace and pushed to the agent as Slack's Events API pushes it.

Two ways a person writes: a reply to something the agent sent (`deliver`), and a message of their own,
which in Slack is a DM to the app's bot (`say`). Both are recorded as actor PERSON and pushed the same way.

The world stamps the message from the run's clock: its `ts` and the callback's `event_time` are simulated.
`X-Slack-Request-Timestamp` is not world time. It is the moment the request is sent, and the agent's
`SignatureVerifier` refuses any timestamp more than five minutes from its own clock, which is the machine's.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import httpx

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.state import BOT_USER_ID, SlackWorld
from minutehand.application.refusals import AgentFailed
from minutehand.domain.people import InboundTarget, PersonMessage, PersonReply
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store


class DeliveryRefused(AgentFailed):
    """The agent answered a pushed event with something other than 2xx, or could not be reached for it."""

    def __init__(self, url: str, status: int | None, body: str) -> None:
        answered = f"answered {status}" if status is not None else "could not be reached"
        super().__init__(f"{url} {answered} to a Slack event: {body[:200]}")
        self.status = status


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """`X-Slack-Signature`: v0= and the HMAC-SHA256 of `v0:<timestamp>:<body>` under the signing secret."""
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _refuse_foreign(target: InboundTarget) -> None:
    if target.provider != MANIFEST.key:
        raise ValueError(f"a {target.provider} target is not Slack's to deliver to")


async def deliver(reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str) -> None:
    _refuse_foreign(target)
    if reply.in_reply_to.provider != MANIFEST.key:
        raise ValueError(f"a {reply.in_reply_to.provider} message is not Slack's to answer")
    if reply.in_reply_to.kind is not EntityKind.MESSAGE:
        raise ValueError(f"a reply answers a message, not a {reply.in_reply_to.kind}")
    slack = SlackWorld(world)
    found = slack.located(reply.in_reply_to.external_id)
    if found is None:
        raise LookupError(f"no Slack message {reply.in_reply_to.external_id} for {reply.person} to answer")
    channel_id, asked = found
    channel = slack.channel(channel_id)
    author = state.user_id(reply.person)
    if channel is None or not slack.is_member(channel_id, author):
        raise LookupError(f"{reply.person} is not in the conversation {channel_id} they are answering")
    # In an IM a reply is a new message; anywhere else it goes in the thread of what it answers.
    thread_ts = asked.thread_ts if asked.thread_ts is not None or channel.is_im else asked.ts
    await _write_and_push(slack, channel, author, reply.text, thread_ts, target, clock, secret)


async def say(message: PersonMessage, target: InboundTarget, world: Store, clock: Clock, *, secret: str) -> None:
    """The person DMs the agent's bot: a new message in their IM with it, never in a thread."""
    _refuse_foreign(target)
    slack = SlackWorld(world)
    author = state.user_id(message.person)
    if slack.user(author) is None:
        raise LookupError(f"{message.person} is not a member of the workspace")
    channel = slack.channel(state.conversation_id([BOT_USER_ID, author]))
    if channel is None:
        raise LookupError(f"{message.person} has no DM with the agent's bot; the workspace was not seeded for them")
    await _write_and_push(slack, channel, author, message.text, None, target, clock, secret)


async def _write_and_push(
    slack: SlackWorld,
    channel: wire.SlackChannel,
    author: str,
    text: str,
    thread_ts: str | None,
    target: InboundTarget,
    clock: Clock,
    secret: str,
) -> None:
    message = wire.SlackMessage(
        ts=slack.next_ts(clock), user=author, text=text, team=state.TEAM_ID, thread_ts=thread_ts
    )
    event = slack.write(
        state.message_ref(message.ts),
        message,
        operation=Operation.CREATE,
        actor=Actor.PERSON,
        parent=channel.id,
        after=MessageSnapshot(
            text=text,
            channel=channel.id,
            recipient_emails=slack.human_emails(channel.id, besides=author),
            thread_of=thread_ts,
        ),
    )
    body = wire.event_body(
        wire.EventCallback(
            team_id=state.TEAM_ID,
            api_app_id=state.APP_ID,
            event_id=f"Ev{event.seq:010d}",
            event_time=int(clock.now().timestamp()),
            authorizations=[wire.Authorization(team_id=state.TEAM_ID, user_id=state.BOT_USER_ID)],
            event=wire.MessageEvent(
                channel=channel.id,
                user=author,
                text=text,
                ts=message.ts,
                event_ts=message.ts,
                channel_type=wire.event_channel_type(channel),
                team=state.TEAM_ID,
                thread_ts=thread_ts,
            ),
        )
    )
    stamp = str(int(time.time()))  # clock-lint: exempt the request's send time, checked against the agent's own clock
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            answered = await client.post(
                target.url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Slack-Request-Timestamp": stamp,
                    "X-Slack-Signature": sign(secret, stamp, body),
                    "User-Agent": "Slackbot 1.0 (+https://api.slack.com/robots)",
                },
            )
    except httpx.HTTPError as e:
        raise DeliveryRefused(target.url, None, repr(e)) from e
    if not answered.is_success:
        raise DeliveryRefused(target.url, answered.status_code, answered.text)
