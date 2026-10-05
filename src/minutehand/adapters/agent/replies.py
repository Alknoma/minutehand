"""People's answers to the agent's captured sends (`Acknowledge.replies`), delivered as the agent's own inbound
webhook expects them: `ports.agent.TakesReplies` for one declared host.

The answer is first written into the world as the person's message, in the conversation the send opened and
threaded under it, by actor PERSON: the ledger settles the wait with it, the expectations hear it (`Relayed`),
and the timeline shows it. Then it is sent to the agent: the declared body with the reply's fields in its strings,
signed if the declaration says so, straight to the agent (never through the proxy). An answer other than 2xx, or
none, fails the wake as a refused pushed event does.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode

import httpx

from minutehand.adapters.proxy.capture import fill, structured, values_at
from minutehand.application.refusals import AgentFailed
from minutehand.domain.outbound import DEFAULT_REPLY_BODY, Acknowledge, ReplyDelivery
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Model, Person
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, MessageSnapshot, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

TIMEOUT = 30.0


class ReplyRefused(AgentFailed):
    """The agent's inbound endpoint did not take a person's answer to one of its sends."""


class _Sent(Model):
    """A captured send as the proxy wrote it into the world (`ProxyAddon._message`)."""

    to: list[str]
    subject: str | None
    text: str


class CapturedReplies:
    def __init__(self, declaration: Acknowledge, people: list[Person], *, secret: str | None) -> None:
        if declaration.replies is None:
            raise ValueError(f"outbound host {declaration.host} declares no replies")
        if declaration.replies.signing is not None and secret is None:
            raise ValueError(f"outbound host {declaration.host} signs replies and no secret was resolved")
        self._declaration = declaration
        self._delivery: ReplyDelivery = declaration.replies
        self._people = {p.key: p for p in people}
        self._secret = secret

    async def deliver(self, reply: PersonReply, world: Store, clock: Clock) -> None:
        asked = world.get(reply.in_reply_to)
        if asked is None:
            raise LookupError(f"no message {reply.in_reply_to.external_id} for {reply.person} to answer")
        sent = _Sent.model_validate_json(asked.body)
        person = self._people[reply.person]
        reply_id = f"{self._declaration.key}-reply-{world.head() + 1}"
        written = world.apply(
            Change(
                entity=EntityRef(provider=self._declaration.key, kind=EntityKind.MESSAGE, external_id=reply_id),
                operation=Operation.CREATE,
                actor=Actor.PERSON,
                body=json.dumps({"from": person.email, "text": reply.text, "in_reply_to": asked.entity.external_id}),
                parent=asked.parent,
                after=MessageSnapshot(
                    text=reply.text,
                    channel=asked.parent or "",
                    thread_of=asked.entity.external_id,
                    answerable=False,
                ),
            )
        )
        subject = sent.subject or ""
        to = sent.to[0] if sent.to else ""
        values = {
            "{reply_id}": reply_id,
            "{from}": person.email,
            "{from_name}": person.name,
            "{to}": str(to),
            "{subject}": f"Re: {subject}" if subject else "",
            "{text}": reply.text,
            "{in_reply_to}": self._thread(asked.entity.external_id, written.seq, world),
            "{sent_at}": clock.now().isoformat(),
        }
        body = self._delivery.body if self._delivery.body is not None else DEFAULT_REPLY_BODY
        await self._send(fill(body, values), clock)

    def _thread(self, message: str, before: int, world: Store) -> str:
        """The id the email API answered the send with, read at `thread` from its own answer; else the message's
        id in the run."""
        path = self._delivery.thread
        if path is None:
            return message
        call = next((c for c in world.calls() if c.first_seq <= int(message) <= c.last_seq), None)
        if call is None or call.exchange.response_body is None or call.exchange.captured is None:
            return message
        parsed = structured(call.exchange.response_body, call.exchange.captured.response.content_type)
        found = [v for v in values_at(parsed, path) if isinstance(v, str | int)]
        return str(found[0]) if found else message

    async def _send(self, body: object, clock: Clock) -> None:
        delivery = self._delivery
        if delivery.form:
            assert isinstance(body, dict)
            raw = urlencode({str(k): str(v) for k, v in body.items()}).encode()
            kind = "application/x-www-form-urlencoded"
        else:
            raw = json.dumps(body, ensure_ascii=False).encode()
            kind = "application/json"
        headers = {"content-type": kind, **delivery.headers}
        signing = delivery.signing
        if signing is not None:
            assert self._secret is not None
            stamp = str(int(clock.now().timestamp()))
            signed = (stamp + "." if "{timestamp}" in signing.format else "").encode() + raw
            digest = hmac.new(self._secret.encode(), signed, hashlib.sha256).digest()
            headers[signing.header] = (
                signing.format.replace("{hex}", digest.hex())
                .replace("{base64}", base64.b64encode(digest).decode())
                .replace("{timestamp}", stamp)
            )
            if signing.timestamp_header is not None:
                headers[signing.timestamp_header] = stamp
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            try:
                answer = await client.request(delivery.method, delivery.url, content=raw, headers=headers)
            except httpx.HTTPError as e:
                raise ReplyRefused(f"{delivery.method} {delivery.url}: {e!r}") from e
        if answer.is_error:
            raise ReplyRefused(
                f"{delivery.method} {delivery.url} answered {answer.status_code} to a reply through "
                f"{self._declaration.host}: {answer.text[:300]}"
            )
