"""Graph change notifications: subscriptions created with the validation handshake, renewed, deleted, expiring on
the run's clock, and notified when anyone, the agent or a person, changes what they watch.

What can be watched: a drive's root (`/drives/{id}/root`, `/me/drive/root`, `/sites/{id}/drive/root`), which every
change to an item in the drive notifies; a chat's or a channel's messages (`/chats/{id}/messages`,
`/teams/{id}/channels/{id}/messages`); a mailbox's messages, all of them or one folder's (`/me/messages`,
`/users/{id}/mailFolders('Inbox')/messages`), each message written there notifying; and a mailbox's events
(`/me/events`, `/users/{id}/events`). Anything else is refused as Graph refuses a resource it cannot watch.

**Validation.** Creating one, Graph POSTs to `notificationUrl` (and `lifecycleNotificationUrl`) with a
`validationToken` query parameter and requires it echoed as `text/plain` with 200; anything else refuses the
subscription. **Expiry** is read on the run's clock: a subscription whose `expirationDateTime` has passed is gone,
notified of nothing and answered 404. **Delivery** of a notification is fire-and-record: a notification URL that
refuses or cannot be reached does not fail the change that caused it, as at Graph.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import httpx
from starlette.requests import Request
from starlette.responses import Response

from minutehand.adapters.providers.microsoft import tokens, wire
from minutehand.adapters.providers.microsoft.common import GRAPH_JSON, GraphRefusal, graph_caller
from minutehand.adapters.providers.microsoft.state import (
    GRAPH,
    SUBSCRIPTIONS,
    MicrosoftWorld,
    SubscriptionRecord,
    graph_time,
    subscription_ref,
)
from minutehand.domain.world import Actor, Operation, RecordSnapshot
from minutehand.ports.clock import Clock

DRIVE_LIMIT = timedelta(minutes=42300)
MESSAGES_LIMIT = timedelta(minutes=60)
OUTLOOK_LIMIT = timedelta(minutes=10080)
"""The longest an Outlook message or event subscription may last."""
VALIDATION_TIMEOUT_SECONDS = 10


def drive_watch(drive: str) -> str:
    return f"drive {drive}"


def conversation_watch(conversation: str) -> str:
    return f"conversation {conversation}"


def mail_watch(user: str, folder: str | None) -> str:
    """A mailbox's messages: one folder's (a well-known folder's name), or with None every folder's."""
    return f"mail {user}" if folder is None else f"mail {user} {folder}"


def calendar_watch(user: str) -> str:
    return f"calendar {user}"


def parse_time(text: str) -> datetime:
    try:
        found = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise NotImplementedError(
            f"the expirationDateTime {text!r}: Graph's answer is not documented or recorded"
        ) from e
    if found.tzinfo is None:
        raise NotImplementedError(
            "an expirationDateTime without a time zone: Graph's answer is not documented or recorded"
        )
    return found


def live(record: SubscriptionRecord, now: datetime) -> bool:
    return parse_time(record.subscription.expirationDateTime) > now


async def _validate(url: str) -> bool:
    token = secrets.token_urlsafe(24)
    try:
        async with httpx.AsyncClient(timeout=VALIDATION_TIMEOUT_SECONDS, trust_env=False) as client:
            answered = await client.post(url, params={"validationToken": token}, headers={"Content-Type": "text/plain"})
    except httpx.HTTPError:
        return False
    return answered.status_code == 200 and answered.text == token


async def notify(
    world: MicrosoftWorld, clock: Clock, watched: str, *, change: str, odata_type: str, resource: str, item: str
) -> None:
    """Tell every live subscription on `watched` that `resource` changed."""
    now = clock.now()
    for record in world.subscriptions():
        if record.watches != watched or not live(record, now):
            continue
        sub = record.subscription
        if change not in sub.changeType.split(","):
            continue
        body = wire.NotificationCollection(
            value=[
                wire.ChangeNotification(
                    subscriptionId=sub.id,
                    clientState=sub.clientState,
                    changeType=change,
                    resource=resource,
                    subscriptionExpirationDateTime=sub.expirationDateTime,
                    tenantId=record.tenant_id,
                    resourceData=wire.ResourceData(odata_type=odata_type, odata_id=resource, id=item),
                )
            ]
        )
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                await client.post(
                    sub.notificationUrl, content=wire.dump(body), headers={"Content-Type": "application/json"}
                )
        except httpx.HTTPError:
            continue


class Subscriptions:
    def __init__(self, world: MicrosoftWorld, clock: Clock, resolve) -> None:
        """`resolve(resource, claims)` answers what a resource watches, and the longest a subscription to it may
        last; it refuses a resource that cannot be watched."""
        self._world = world
        self._clock = clock
        self._resolve = resolve

    def _answer(self, record: SubscriptionRecord, status: int = 200) -> Response:
        sub = record.subscription.model_copy(update={"odata_context": f"{GRAPH}/$metadata#subscriptions/$entity"})
        return Response(wire.dump(sub), status_code=status, media_type=GRAPH_JSON)

    def _found(self, sub: str, *, renewing: bool = False) -> SubscriptionRecord:
        """A live subscription. Renewing one that expired or was deleted is 404 (subscription-update documents the
        status, not the code, so none is answered); reading or deleting one is not documented."""
        record = self._world.subscription(sub)
        if record is None or not live(record, self._clock.now()):
            if renewing:
                raise GraphRefusal(404, None, f"The subscription '{sub}' no longer exists.")
            raise NotImplementedError(
                "a subscription that does not exist or has expired: Graph's answer is not documented or recorded"
            )
        return record

    async def create(self, request: Request) -> Response:
        claims = graph_caller(request, self._world)
        try:
            asked = wire.read(wire.SubscriptionRequest, await request.body())
        except wire.Unreadable as e:
            raise NotImplementedError(
                f"a request body that cannot be read ({e.message}): Graph's answer is not documented or recorded"
            ) from e
        for name, value in (
            ("changeType", asked.changeType),
            ("notificationUrl", asked.notificationUrl),
            ("resource", asked.resource),
            ("expirationDateTime", asked.expirationDateTime),
        ):
            if not value:
                raise NotImplementedError(
                    f"a subscription without {name}: Graph's answer is not documented or recorded"
                )
        kinds = asked.changeType.split(",")
        if not set(kinds) <= {"created", "updated", "deleted"}:
            raise NotImplementedError(
                f"the changeType {asked.changeType!r}: Graph's answer is not documented or recorded"
            )
        if not asked.notificationUrl.lower().startswith("https://") and not asked.notificationUrl.lower().startswith(
            "http://"
        ):
            raise NotImplementedError(
                f"the notificationUrl {asked.notificationUrl!r}: Graph's answer is not documented or recorded"
            )
        watches, limit = self._resolve(asked.resource, claims)
        expires = parse_time(asked.expirationDateTime)
        now = self._clock.now()
        if expires <= now or expires > now + limit:
            raise NotImplementedError(
                f"an expirationDateTime outside now to {int(limit.total_seconds() // 60)} minutes ahead, the most the "
                f"resource allows: Graph's answer is not documented or recorded"
            )
        taken = next(
            (
                r
                for r in self._world.subscriptions()
                if live(r, now)
                and r.subscription.changeType == asked.changeType
                and r.subscription.resource == asked.resource
            ),
            None,
        )
        if taken is not None:
            raise GraphRefusal(
                409, None, f"Subscription Id {taken.subscription.id} already exists for the requested combination"
            )
        for url in [
            asked.notificationUrl,
            *([asked.lifecycleNotificationUrl] if asked.lifecycleNotificationUrl else []),
        ]:
            if not await _validate(url):
                raise GraphRefusal(
                    400,
                    None,
                    "Subscription validation request failed. Notification endpoint must respond with 200 OK to "
                    "validation request.",
                )
        record = SubscriptionRecord(
            subscription=wire.Subscription(
                id=tokens.derived_trace(f"subscription {self._world.next_seq()}"),
                resource=asked.resource,
                applicationId=claims.appid,
                changeType=asked.changeType,
                clientState=asked.clientState,
                notificationUrl=asked.notificationUrl,
                lifecycleNotificationUrl=asked.lifecycleNotificationUrl,
                expirationDateTime=graph_time(expires),
                creatorId=claims.oid or claims.appid,
            ),
            tenant_id=claims.tid or "",
            watches=watches,
        )
        self._write(record, Operation.CREATE)
        return self._answer(record, 201)

    def _write(self, record: SubscriptionRecord, operation: Operation) -> None:
        self._world.write(
            subscription_ref(record.subscription.id),
            record,
            operation=operation,
            actor=Actor.AGENT,
            parent=SUBSCRIPTIONS,
            after=RecordSnapshot(
                resource="subscription", text=f"{record.subscription.changeType} {record.subscription.resource}"
            ),
        )

    async def get(self, request: Request) -> Response:
        record = self._found(request.path_params["sub"])
        self._world.saw(subscription_ref(record.subscription.id), Operation.READ)
        return self._answer(record)

    async def list(self, request: Request) -> Response:
        claims = graph_caller(request, self._world)
        now = self._clock.now()
        found = [
            r.subscription
            for r in self._world.subscriptions()
            if live(r, now) and r.subscription.applicationId == claims.appid
        ]
        page = wire.Page[wire.Subscription](context=f"{GRAPH}/$metadata#subscriptions", value=found)
        return Response(wire.dump(page), media_type=GRAPH_JSON)

    async def renew(self, request: Request) -> Response:
        record = self._found(request.path_params["sub"], renewing=True)
        try:
            asked = wire.read(wire.SubscriptionPatch, await request.body())
        except wire.Unreadable as e:
            raise NotImplementedError(
                f"a request body that cannot be read ({e.message}): Graph's answer is not documented or recorded"
            ) from e
        expires = parse_time(asked.expirationDateTime)
        _, limit = self._resolve(record.subscription.resource, None)
        now = self._clock.now()
        if expires <= now or expires > now + limit:
            raise NotImplementedError(
                "renewing to an expirationDateTime outside what the resource allows: Graph's answer is not documented or recorded"
            )
        renewed = record.model_copy(
            update={"subscription": record.subscription.model_copy(update={"expirationDateTime": graph_time(expires)})}
        )
        self._write(renewed, Operation.UPDATE)
        return self._answer(renewed)

    async def delete(self, request: Request) -> Response:
        record = self._found(request.path_params["sub"])
        self._world.remove(subscription_ref(record.subscription.id), actor=Actor.AGENT, parent=SUBSCRIPTIONS)
        return Response(status_code=204)
