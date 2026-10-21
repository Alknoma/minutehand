"""What the inbox tests share: the product's inbox declared as an agent file declares it, and its people."""

from __future__ import annotations

from minutehand.domain.inboxes import HttpInbox
from tests.inboxes.product import Product

OWEN = "owen@example.com"
NADIA = "nadia@example.com"
TOKENS = {OWEN: "token-of-owen-7c1d", NADIA: "token-of-nadia-93ab"}


def inbox(product: Product, *, everyone: bool = False, **more: object) -> HttpInbox:
    """The product's approvals, declared as an agent file would: as each approver, with their bearer token."""
    url = f"{product.base}/everyone" if everyone else f"{product.base}/approvals?approver={{person.email}}"
    pending: dict[str, object] = {
        "request": {"kind": "template", "url": url},
        "items": "$.items[*]",
        "id": "$.id",
        "summary": "$.summary",
        "decisions": "$.actions",
        "paging": {"next": "$.next", "param": "cursor"},
    }
    if everyone:
        pending["waits_on"] = "$.approver"
    decide = f"{product.base}/approvals/{{item.id}}/decision"
    return HttpInbox.model_validate(
        {
            "name": "approvals",
            "as_person": {"headers": {"Authorization": "Bearer {person.credential}"}},
            "pending": pending,
            "decisions": [
                {
                    "name": "approve",
                    "reads": "approved",
                    "request": {"kind": "template", "method": "POST", "url": decide, "body": {"decision": "approve"}},
                },
                {
                    "name": "reject",
                    "reads": "rejected",
                    "description": "Turn it down, saying why",
                    "request": {
                        "kind": "template",
                        "method": "POST",
                        "url": decide,
                        "body": {"decision": "reject", "reason": "{input.reason}"},
                    },
                    "inputs": [{"name": "reason", "description": "Why it is turned down"}],
                },
            ],
            **more,
        }
    )
