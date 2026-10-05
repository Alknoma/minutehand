"""Notion's integration webhooks: a subscription seeded as if set up in the integration's settings, its one-time
verification request, and signed event deliveries.

Written from Notion's public webhook reference (https://developers.notion.com/reference/webhooks and
`webhooks-events-delivery`) by reading it, and not verified against the live service:

- **Verification.** A subscription that is not yet verified is first sent a POST whose body is
  `{"verification_token": ...}`, unsigned. At Notion a person then pastes that token into the integration's
  settings; here the subscription counts as verified once the endpoint answers it 2xx. Until then it is sent no
  events, and what it was owed is dropped, as Notion sends nothing to an unverified subscription.
- **Signature.** Every event carries `X-Notion-Signature: sha256=<hex>`, the HMAC-SHA256 of the exact body bytes
  keyed with the verification token.
- **Shape.** `id`, `timestamp`, `workspace_id`, `workspace_name`, `subscription_id`, `integration_id`, `type`,
  `authors` (`[{id, type}]`, `person` or `bot`), `attempt_number`, `entity` (`{id, type}`), and `data`: `parent`
  for every page event, `updated_blocks` for content, `updated_properties` (property ids) for properties, and
  `page_id` with `parent` for a comment.
- **Whose changes.** Every change the integration can see, by a person or by any integration, itself included:
  the reference names no exception, and `authors` can be a bot. A subscriber that must ignore its own writes
  compares `authors` with its bot's id.
- **When.** A person's change is sent when the run tells the provider's watchers (`NotifiesChanges.notify`); the
  integration's own change through the API as soon as the call that made it is answered. Notion aggregates
  frequent events over a short window and may deliver late or out of order; here each change is one event, sent
  once, in order, never retried.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
from pydantic import JsonValue

from minutehand.adapters.providers.notion import wire
from minutehand.adapters.providers.notion.state import NotionWorld
from minutehand.domain.world import Operation
from minutehand.ports.clock import Clock

TIMEOUT = 30.0

_PARENT_KIND = {
    wire.ParentType.PAGE_ID: "page",
    wire.ParentType.DATABASE_ID: "database",
    wire.ParentType.BLOCK_ID: "block",
    wire.ParentType.WORKSPACE: "space",
}


def signature(token: str, body: bytes) -> str:
    """`X-Notion-Signature`: `sha256=` and the HMAC-SHA256 of the body under the verification token."""
    return "sha256=" + hmac.new(token.encode(), body, hashlib.sha256).hexdigest()


def watching(world: NotionWorld) -> bool:
    return bool(world.webhooks())


async def deliver(world: NotionWorld, clock: Clock) -> None:
    """Verify every unverified subscription, then send each verified one what it is owed, oldest first."""
    for hook in world.webhooks():
        if not hook.verified:
            if not await _verify(hook):
                for owed in world.owed(hook.id):
                    world.sent(owed)
                continue
            hook = hook.model_copy(update={"verified": True})
            world.write_webhook(hook, operation=Operation.UPDATE)
        workspace = world.workspace(hook.workspace)
        for owed in world.owed(hook.id):
            body = json.dumps(_event(hook, owed, workspace.name if workspace else "")).encode()
            headers = {
                "Content-Type": "application/json",
                "X-Notion-Signature": signature(hook.verification_token, body),
            }
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
                    await client.post(hook.url, content=body, headers=headers)
            except httpx.HTTPError:
                pass
            world.sent(owed)


async def _verify(hook: wire.StoredWebhook) -> bool:
    body = json.dumps({"verification_token": hook.verification_token}).encode()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            answered = await client.post(hook.url, content=body, headers={"Content-Type": "application/json"})
    except httpx.HTTPError:
        return False
    return answered.is_success


def _event(hook: wire.StoredWebhook, owed: wire.StoredOwedEvent, workspace_name: str) -> JsonValue:
    parent: JsonValue = {
        "id": owed.parent.id if owed.parent.id is not None else hook.workspace,
        "type": _PARENT_KIND[owed.parent.type],
    }
    data: dict[str, JsonValue] = {"parent": parent}
    if owed.type is wire.WebhookEvent.PAGE_CONTENT_UPDATED:
        data["updated_blocks"] = [{"id": b, "type": "block"} for b in owed.updated_blocks]
    if owed.type is wire.WebhookEvent.PAGE_PROPERTIES_UPDATED:
        data["updated_properties"] = list(owed.updated_properties)
    if owed.page_id is not None:
        data["page_id"] = owed.page_id
    return {
        "id": owed.id,
        "timestamp": owed.timestamp,
        "workspace_id": hook.workspace,
        "workspace_name": workspace_name,
        "subscription_id": hook.id,
        "integration_id": hook.integration,
        "type": owed.type.value,
        "authors": [{"id": owed.author, "type": owed.author_type.value}],
        "attempt_number": 1,
        "entity": {"id": owed.entity_id, "type": owed.entity_type},
        "data": data,
    }
