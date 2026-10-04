"""A person's reply, written into the workspace and pushed to the agent as Slack's Events API pushes it."""

from __future__ import annotations

import hashlib
import hmac

import httpx

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.domain.people import InboundTarget, PersonReply
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store


class DeliveryRefused(Exception):
    """The agent answered a pushed event with something other than 2xx."""

    def __init__(self, url: str, status: int, body: str) -> None:
        super().__init__(f"{url} answered {status} to a Slack event: {body[:200]}")
        self.status = status


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """`X-Slack-Signature`: v0= and the HMAC-SHA256 of `v0:<timestamp>:<body>` under the signing secret."""
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


async def deliver(reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str) -> None:
    if target.provider != MANIFEST.key or reply.in_reply_to.provider != MANIFEST.key:
        raise ValueError(f"a {target.provider} target or a {reply.in_reply_to.provider} message is not Slack's to deliver")
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
    message = wire.SlackMessage(
        ts=slack.next_ts(clock), user=author, text=reply.text, team=state.TEAM_ID, thread_ts=thread_ts
    )
    event = slack.write(
        state.message_ref(message.ts), message, operation=Operation.CREATE, actor=Actor.PERSON, parent=channel_id,
        after=MessageSnapshot(
            text=reply.text, channel=channel_id, recipient_emails=slack.human_emails(channel_id, besides=author),
            thread_of=thread_ts,
        ),
    )

    now = int(clock.now().timestamp())
    body = wire.event_body(wire.EventCallback(
        team_id=state.TEAM_ID, api_app_id=state.APP_ID, event_id=f"Ev{event.seq:010d}", event_time=now,
        authorizations=[wire.Authorization(team_id=state.TEAM_ID, user_id=state.BOT_USER_ID)],
        event=wire.MessageEvent(
            channel=channel_id, user=author, text=reply.text, ts=message.ts, event_ts=message.ts,
            channel_type=wire.event_channel_type(channel), team=state.TEAM_ID, thread_ts=thread_ts,
        ),
    ))
    stamp = str(now)
    async with httpx.AsyncClient(timeout=30) as client:
        answered = await client.post(target.url, content=body, headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": sign(secret, stamp, body),
            "User-Agent": "Slackbot 1.0 (+https://api.slack.com/robots)",
        })
    if not answered.is_success:
        raise DeliveryRefused(target.url, answered.status_code, answered.text)
