"""Jira's admin webhooks: a webhook set up on the site (`JiraSeed.webhooks`, as set in Jira administration) is sent
the events it names as they happen, a POST of JSON.

Written from Atlassian's webhook reference (https://developer.atlassian.com/cloud/jira/platform/webhooks/):

- **Events.** `jira:issue_created`, `jira:issue_updated` and `comment_created` are sent; the other events the
  reference lists (`jira:issue_deleted`, `comment_updated`, `worklog_created`, ...) are refused by name at seeding.
- **Payload.** `timestamp` (milliseconds), `webhookEvent`, `user` (the condensed user: no `locale`,
  `emailAddress`, `groups` or `applicationRoles`), `issue` (the issue as the REST API answers it with no `expand`),
  and, for an update, `changelog` (`id`, `items`) and `issue_event_type_name` (`issue_generic`); for a comment
  event, `comment`.
- **Headers.** `X-Atlassian-Webhook-Identifier`, unique to the delivery; `X-Hub-Signature`, `sha256=` and the
  HMAC-SHA256 of the body keyed with the webhook's secret, when it has one.
- **JQL filter.** A webhook's `jql` takes the clauses `issueKey`, `project`, `issuetype`, `status`, `priority`,
  `assignee` and `reporter` with `=`, `!=`, `IN` and `NOT IN`; anything else is refused at seeding.
- **Not done.** The reference's retries (up to five, on 408, 409, 425, 429, 5xx, a refused connection or a timeout)
  are not made: each event is sent once. `X-Atlassian-Webhook-Retry` and `X-Atlassian-Webhook-Flow` are not sent: the
  reference does not give their values for a first delivery.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

import httpx

from minutehand.adapters.providers.jira import jql, wire
from minutehand.adapters.providers.jira.moves import Event

TIMEOUT = 30.0
_CLAUSES = frozenset({"issuekey", "key", "project", "issuetype", "status", "priority", "assignee", "reporter"})
_OPERATORS = frozenset({jql.Op.EQ, jql.Op.NE, jql.Op.IN, jql.Op.NOT_IN})


@dataclass(frozen=True)
class Outgoing:
    """One delivery, built when the event happened."""

    hook: wire.StoredHook
    identifier: str
    body: bytes


def check_events(events: list[str]) -> None:
    """Refuse, by name, an event this fake does not send."""
    sent = {e.value for e in Event}
    for name in events:
        if name in sent:
            continue
        raise ValueError(f"this fake does not send the webhook event {name!r}; it sends {', '.join(sorted(sent))}")


def check_filter(text: str) -> jql.Query:
    """A webhook's JQL filter as the reference allows it, else `ValueError` naming what it does not."""
    query = jql.parse(text)
    if query.order:
        raise ValueError("a webhook's JQL filter takes no ORDER BY")
    if query.where is not None:
        _check(query.where)
    return query


def _check(node: jql.Node) -> None:
    if isinstance(node, jql.Clause):
        if node.field.lower() not in _CLAUSES:
            raise ValueError(f"a webhook's JQL filter does not take the clause {node.field!r}")
        if node.op not in _OPERATORS:
            raise ValueError(f"a webhook's JQL filter does not take the operator {node.op.value!r}")
    elif isinstance(node, jql.Not):
        _check(node.operand)
    else:
        for part in node.parts:
            _check(part)


def signature(secret: str, body: bytes) -> str:
    """`X-Hub-Signature`: the method, `=`, and the HMAC of the body keyed with the secret."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def send(outgoing: Outgoing) -> None:
    """POST the delivery. Jira counts only a 200 a success and retries the rest; here a failure is the end of it."""
    headers = {"Content-Type": "application/json", "X-Atlassian-Webhook-Identifier": outgoing.identifier}
    if outgoing.hook.secret is not None:
        headers["X-Hub-Signature"] = signature(outgoing.hook.secret, outgoing.body)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            await client.post(outgoing.hook.url, content=outgoing.body, headers=headers)
    except httpx.HTTPError:
        pass
