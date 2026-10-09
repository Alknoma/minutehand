"""Asana's webhooks: the handshake that makes one, and the signed deliveries that follow.

Written from https://developers.asana.com/docs/webhooks-guide and `createWebhook` in the OpenAPI subset
(`tests/data/asana_rest_1_0/openapi-subset-2026-10-08.json`):

- **Handshake.** Creating a webhook sends the target a POST with an `X-Hook-Secret` header; "The target must respond
  with a `200 OK` or `204 No Content` and a matching `X-Hook-Secret` header", or the webhook "will fail to setup".
  The creating request is answered while that POST is in flight.
- **Deliveries.** A POST whose JSON body is `{"events": [...]}`, signed: `X-Hook-Signature` is "a SHA256 HMAC
  signature computed on the request body using the shared secret transmitted during the handshake".
- **Heartbeat.** "Heartbeats deliver empty payloads initially after handshake": the first delivery to a webhook is
  `{"events": []}`, and the webhook is `active` and has a `last_success_at` once it is answered. The heartbeat
  every eight hours after is not sent: it is a timer, and none is booked.
- **Filters.** "If a webhook event passes any of the filters the event will be delivered".
- **Failure.** A delivery the target does not answer 2xx is recorded (`last_failure_at`, `last_failure_content`,
  `delivery_retry_count`) and its events stay owed, sent again with the next delivery. Asana's exponential back-off
  and its deletion of a webhook that fails for 24 hours are not modelled.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from minutehand.adapters.providers.asana import state, wire
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock

TIMEOUT = 30.0
HANDSHAKE = "X-Hook-Secret"
SIGNATURE = "X-Hook-Signature"


def secret_for(subscription: str, head: int) -> str:
    """The secret Asana assigns a webhook (named by what it subscribes and where it delivers): 64 hex digits, as its
    example, the same in every run of the same world."""
    return hashlib.sha256(f"asana-webhook/{subscription}/{head}".encode()).hexdigest()


def signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def passes(hook: wire.AsanaWebhook, event: wire.AsanaEvent) -> bool:
    """Whether the event is delivered: always when the webhook has no filters, else when it passes any one."""
    if not hook.filters:
        return True
    return any(_matches(f, event) for f in hook.filters)


def _matches(filter_: wire.AsanaFilter, event: wire.AsanaEvent) -> bool:
    if filter_.resource_type is not None and filter_.resource_type != event.resource.resource_type:
        return False
    if filter_.resource_subtype is not None and filter_.resource_subtype != event.resource.resource_subtype:
        return False
    if filter_.action is not None and filter_.action is not event.action:
        return False
    if filter_.fields:
        return event.change is not None and event.change.field in filter_.fields
    return True


async def handshake(target: str, secret: str) -> bool:
    """Send the target its secret and whether it answered as Asana requires."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            answered = await client.post(target, headers={HANDSHAKE: secret})
    except httpx.HTTPError:
        return False
    return answered.status_code in (200, 204) and answered.headers.get(HANDSHAKE) == secret


def watching(world: state.AsanaWorld, task_scope: list[str] | None = None) -> bool:
    """Whether any webhook exists (any that hears of a change to something in `task_scope`, when given)."""
    hooks = world.webhooks()
    return any(h.resource in task_scope for h in hooks) if task_scope is not None else bool(hooks)


async def deliver(world: state.AsanaWorld, clock: Clock) -> None:
    """Send every webhook what it is owed: the heartbeat first to one never answered, then its events, in order."""
    for hook in world.webhooks():
        heard = list(world.events_after(hook.cursor, hook.resource))
        owed = [e for e in heard if passes(hook, e)]
        beating = hook.last_success_at is None
        if not owed and not beating:
            continue
        names: dict[str, str] = {}
        body = json.dumps({"events": [wire.event_json(e, names, named=False) for e in owed]}).encode()
        headers = {"Content-Type": "application/json", SIGNATURE: signature(hook.secret, body)}
        now = wire.stamp(clock.now())
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
                answered = await client.post(hook.target, content=body, headers=headers)
            failure = (
                None if answered.is_success else f"{answered.status_code} {answered.reason_phrase}\n\n{answered.text}"
            )
        except httpx.HTTPError as error:
            failure = f"{type(error).__name__}: {error}"
        latest = world.webhook(hook.gid)
        if latest is None:  # deleted while the delivery was in flight
            continue
        if failure is None:
            cursor = max([hook.cursor, *(e.gid for e in heard)], key=int)
            changed = latest.model_copy(
                update={"active": True, "last_success_at": now, "delivery_retry_count": 0, "cursor": cursor}
            )
        else:
            changed = latest.model_copy(
                update={
                    "last_failure_at": now,
                    "last_failure_content": failure,
                    "delivery_retry_count": latest.delivery_retry_count + 1,
                }
            )
        world.put_record(changed, parent=state.WEBHOOKS, actor=Actor.SYSTEM, operation=Operation.UPDATE)
