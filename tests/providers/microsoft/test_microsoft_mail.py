"""Outlook mail through Graph, through the real proxy with plain `httpx`: sending, one copy per mailbox, the sent
copy read by the run as an ask, a person's reply by email landing in the conversation and notified to an inbox
subscription, the query options clients send, `delta`, and the refusals."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from minutehand.adapters.providers.microsoft.seed import microsoft_seed
from minutehand.domain.scenario import ProviderSeed
from minutehand.domain.world import Actor, MessageSnapshot
from tests.providers.microsoft.outlook import AGENT, OUTLOOK, reply, sent_by_agent, signed_in
from tests.providers.microsoft.tenant import GRAPH, Intercepted, Tenant, Webhook, bearer, seeded, token

"""Where Teams would push; nothing is pushed for an email, and a push here would fail."""


@pytest.fixture
def tenant(tmp_path: Path) -> Tenant:
    return seeded(tmp_path / "world.db", OUTLOOK)


def _at(tenant: Tenant, later: timedelta) -> str:
    return (tenant.clock.now() + later).isoformat().replace("+00:00", "Z")


async def _send(http: Any, auth: dict[str, str], to: list[str], subject: str, text: str) -> None:
    sent = await http.post(
        f"{GRAPH}/users/{AGENT}/sendMail",
        json={
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": text},
                "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            },
            "saveToSentItems": True,
        },
        headers=auth,
    )
    assert sent.status_code == 202, sent.text


async def test_a_sent_email_is_one_copy_per_mailbox_and_the_senders_copy_asks_each_recipient(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        await _send(http, auth, ["Sofia@Example.com", "outsider@elsewhere.example"], "Room", "Which room is free?")
        [asked] = sent_by_agent(tenant)
        assert isinstance(asked.after, MessageSnapshot)
        assert asked.after.recipient_emails == ["sofia@example.com", "outsider@elsewhere.example"]
        assert asked.after.text == "Room\n\nWhich room is free?"
        sent = (await http.get(f"{GRAPH}/users/{AGENT}/mailFolders/sentitems/messages", headers=auth)).json()
        [mine] = sent["value"]
        assert mine["id"] == asked.entity.external_id and mine["isRead"] is True
        assert mine["conversationId"] == asked.after.channel
        assert mine["body"] == {"contentType": "html", "content": "<html><body>Which room is free?</body></html>"}
        inbox = (await http.get(f"{GRAPH}/users/sofia@example.com/mailFolders('Inbox')/messages", headers=auth)).json()
        [hers] = inbox["value"]
        assert hers["id"] != mine["id"] and hers["conversationId"] == mine["conversationId"]
        assert (hers["isRead"], hers["from"]["emailAddress"]["address"]) == (False, AGENT)


async def test_a_persons_reply_lands_in_the_senders_inbox_in_the_conversation_and_is_notified(
    tenant: Tenant, microsoft: Intercepted, webhook: Webhook
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await signed_in(http, tenant, AGENT))
        made = await http.post(
            f"{GRAPH}/subscriptions",
            json={
                "changeType": "created",
                "notificationUrl": webhook.url,
                "resource": "/me/mailFolders('Inbox')/messages",
                "expirationDateTime": _at(tenant, timedelta(days=2)),
                "clientState": "inbox",
            },
            headers=auth,
        )
        assert made.status_code == 201, made.text
        sent = await http.post(
            f"{GRAPH}/me/sendMail",
            json={"message": {"subject": "Room", "body": {"contentType": "HTML", "content": "<p>Which room?</p>"},
                              "toRecipients": [{"emailAddress": {"address": "sofia@example.com"}}]}},
            headers=auth,
        )  # fmt: skip
        assert sent.status_code == 202
        [asked] = sent_by_agent(tenant)
        assert webhook.notifications == [], "the agent's own send lands in Sent Items, not its Inbox"
        await tenant.provider.land(
            reply("sofia", asked, "Room Ferris is free.", after=timedelta(hours=4)),
            tenant.store,
            tenant.clock,
        )
        [note] = webhook.notifications
        assert note["value"][0]["clientState"] == "inbox" and note["value"][0]["changeType"] == "created"
        unread = await http.get(
            f"{GRAPH}/me/mailFolders/inbox/messages",
            params={"$filter": "isRead eq false and from/emailAddress/address eq 'sofia@example.com'"},
            headers=auth,
        )
        [answer] = unread.json()["value"]
        assert answer["id"] == note["value"][0]["resourceData"]["id"]
        assert answer["subject"] == "RE: Room" and answer["conversationId"] == asked.after.channel  # type: ignore[union-attr]
        written = [e for e in tenant.store.events() if e.actor is Actor.PERSON and isinstance(e.after, MessageSnapshot)]
        assert [(e.after.text, e.after.recipient_emails) for e in written if isinstance(e.after, MessageSnapshot)] == [
            ("RE: Room\n\nRoom Ferris is free.", [AGENT])
        ]
        read = await http.patch(f"{GRAPH}/me/messages/{answer['id']}", json={"isRead": True}, headers=auth)
        assert read.status_code == 200 and read.json()["isRead"] is True
        answered = await http.post(
            f"{GRAPH}/me/messages/{answer['id']}/reply", json={"comment": "Thanks, booking it."}, headers=auth
        )
        assert answered.status_code == 202
        follow = sent_by_agent(tenant)[-1]
        assert isinstance(follow.after, MessageSnapshot)
        assert follow.after.channel == asked.after.channel, "a reply stays in the conversation, so it is one ask"  # type: ignore[union-attr]
        assert follow.after.recipient_emails == ["sofia@example.com"]


async def test_filter_orderby_top_and_paging_read_the_mailbox_as_clients_ask(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        everything = (await http.get(f"{GRAPH}/users/{AGENT}/messages", headers=auth)).json()["value"]
        assert [m["subject"] for m in everything] == ["Re: Vendor review", "Lunch", "Vendor review"], "newest first"
        kickoff = everything[2]
        assert everything[0]["conversationId"] == kickoff["conversationId"] != everything[1]["conversationId"]
        same = await http.get(
            f"{GRAPH}/users/{AGENT}/messages",
            params={"$filter": f"conversationId eq '{kickoff['conversationId']}'", "$select": "subject"},
            headers=auth,
        )
        assert [set(m) - {"@odata.etag"} for m in same.json()["value"]] == [{"id", "subject"}] * 2
        since = (tenant.clock.now() - timedelta(minutes=150)).isoformat().replace("+00:00", "Z")
        recent = await http.get(
            f"{GRAPH}/users/{AGENT}/mailFolders/inbox/messages",
            params={"$filter": f"receivedDateTime ge {since}", "$orderby": "receivedDateTime asc", "$top": "1"},
            headers=auth,
        )
        page = recent.json()
        assert [m["subject"] for m in page["value"]] == ["Lunch"]
        following = (await http.get(page["@odata.nextLink"], headers=auth)).json()
        assert [m["subject"] for m in following["value"]] == ["Re: Vendor review"]
        assert "@odata.nextLink" not in following
        unread = await http.get(f"{GRAPH}/users/{AGENT}/messages", params={"$filter": "isRead eq false"}, headers=auth)
        assert {m["subject"] for m in unread.json()["value"]} == {"Vendor review", "Re: Vendor review"}
        text = await http.get(
            f"{GRAPH}/users/{AGENT}/messages/{kickoff['id']}",
            headers={**auth, "Prefer": 'outlook.body-content-type="text"'},
        )
        assert text.json()["body"] == {"contentType": "text", "content": "Please book the vendor review with Sofia."}


async def test_an_orderby_that_does_not_lead_the_filter_is_refused_inefficient_filter(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        refused = await http.get(
            f"{GRAPH}/users/{AGENT}/messages",
            params={"$filter": "isRead eq false", "$orderby": "receivedDateTime desc"},
            headers=auth,
        )
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "InefficientFilter"
        led = await http.get(
            f"{GRAPH}/users/{AGENT}/messages",
            params={"$filter": "receivedDateTime ge 2020-01-01T00:00:00Z and isRead eq false",
                    "$orderby": "receivedDateTime desc"},
            headers=auth,
        )  # fmt: skip
        assert led.status_code == 200 and len(led.json()["value"]) == 2


async def test_delta_lists_the_folder_then_only_what_arrived_and_what_left(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        link = f"{GRAPH}/users/{AGENT}/mailFolders('inbox')/messages/delta"
        first = (await http.get(link, headers={**auth, "Prefer": "odata.maxpagesize=2"})).json()
        rest = (await http.get(first["@odata.nextLink"], headers=auth)).json()
        assert len(first["value"]) + len(rest["value"]) == 3 and "@odata.deltaLink" in rest
        nothing = (await http.get(rest["@odata.deltaLink"], headers=auth)).json()
        assert nothing["value"] == []
        await _send(http, auth, [AGENT], "Note to self", "Remember the projector.")
        gone = next(m for m in first["value"] + rest["value"] if m["subject"] == "Lunch")
        assert (await http.delete(f"{GRAPH}/users/{AGENT}/messages/{gone['id']}", headers=auth)).status_code == 204
        changed = (await http.get(nothing["@odata.deltaLink"], headers=auth)).json()["value"]
        assert [m["subject"] for m in changed if "subject" in m] == ["Note to self"]
        assert [m for m in changed if "@removed" in m] == [{"id": gone["id"], "@removed": {"reason": "deleted"}}]


async def test_a_users_token_reaching_another_mailbox_is_refused_access_denied(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await signed_in(http, tenant, "sofia@example.com"))
        assert (await http.get(f"{GRAPH}/me/messages", headers=auth)).status_code == 200
        refused = await http.get(f"{GRAPH}/users/{AGENT}/messages", headers=auth)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "ErrorAccessDenied"


async def test_a_send_with_no_recipient_or_a_bad_address_is_refused_invalid_recipients(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        for recipients in ([], [{"emailAddress": {"address": "not an address"}}]):
            refused = await http.post(
                f"{GRAPH}/users/{AGENT}/sendMail",
                json={"message": {"subject": "x", "toRecipients": recipients}},
                headers=auth,
            )
            assert refused.status_code == 400 and refused.json()["error"]["code"] == "ErrorInvalidRecipients"
        assert sent_by_agent(tenant) == []


def test_a_seeded_email_naming_nobody_is_refused(tmp_path: Path) -> None:
    body = microsoft_seed(OUTLOOK).model_copy(update={"emails": []}).model_dump(mode="json")
    body["emails"] = [{"by": "owen", "to": ["nobody"], "subject": "x", "ago": "PT1H"}]
    scenario = OUTLOOK.model_copy(
        update={"provider_seeds": [ProviderSeed.model_validate({"provider": "microsoft", "body": body})]}
    )
    with pytest.raises(ValueError, match="names no user of the tenant: nobody"):
        seeded(tmp_path / "world.db", scenario)


async def test_a_mail_subscription_past_seven_days_or_on_a_folder_that_is_none_is_refused(
    tenant: Tenant, microsoft: Intercepted, webhook: Webhook
) -> None:
    base = {"changeType": "created", "notificationUrl": webhook.url, "resource": f"/users/{AGENT}/messages"}
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        week = await http.post(
            f"{GRAPH}/subscriptions", json={**base, "expirationDateTime": _at(tenant, timedelta(days=8))}, headers=auth
        )
        assert week.status_code == 400 and "10080 minutes" in week.json()["error"]["message"]
        nowhere = await http.post(
            f"{GRAPH}/subscriptions",
            json={**base, "resource": f"/users/{AGENT}/mailFolders('Archive2')/messages",
                  "expirationDateTime": _at(tenant, timedelta(days=1))},
            headers=auth,
        )  # fmt: skip
        assert nowhere.status_code == 404
        held = await http.post(
            f"{GRAPH}/subscriptions", json={**base, "expirationDateTime": _at(tenant, timedelta(days=6))}, headers=auth
        )
        assert held.status_code == 201, held.text
