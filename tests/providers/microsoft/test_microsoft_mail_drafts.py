"""Outlook drafts through Graph: create, change, send; reply and forward drafts; move and copy; attachments."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from tests.providers.microsoft.outlook import AGENT, OUTLOOK, sent_by_agent, signed_in
from tests.providers.microsoft.tenant import GRAPH, Intercepted, Tenant, Webhook, bearer, seeded, token


@pytest.fixture
def tenant(tmp_path: Path) -> Tenant:
    return seeded(tmp_path / "world.db", OUTLOOK)


@dataclass
class Mailbox:
    http: httpx.AsyncClient
    me: dict[str, str]
    app: dict[str, str]
    tenant: Tenant

    async def folder(self, name: str, subject: str | None = None) -> list[dict[str, Any]]:
        params = (
            {"$filter": f"subject eq '{subject}'", "$orderby": "subject"}
            if subject is not None
            else {"$orderby": "createdDateTime"}
        )
        listed = await self.http.get(f"{GRAPH}/me/mailFolders/{name}/messages", params=params, headers=self.me)
        assert listed.status_code == 200, listed.text
        value: list[dict[str, Any]] = listed.json()["value"]
        return value

    async def inbox_message(self, subject: str) -> dict[str, str]:
        listed = await self.http.get(
            f"{GRAPH}/me/mailFolders/inbox/messages",
            params={"$filter": f"subject eq '{subject}'", "$orderby": "subject"},
            headers=self.me,
        )
        [found] = listed.json()["value"]
        return found


@pytest.fixture
async def mailbox(tenant: Tenant, microsoft: Intercepted) -> AsyncIterator[Mailbox]:
    async with microsoft.http() as http:
        yield Mailbox(
            http=http,
            me=bearer(await signed_in(http, tenant, AGENT)),
            app=bearer(await token(http, tenant, "https://graph.microsoft.com/.default")),
            tenant=tenant,
        )


SOFIA = {"emailAddress": {"address": "sofia@example.com", "name": "Sofia Romano"}}


def _at(tenant: Tenant, later: timedelta) -> str:
    return (tenant.clock.now() + later).isoformat().replace("+00:00", "Z")


async def test_a_draft_is_made_changed_and_sent_to_its_recipients(mailbox: Mailbox, webhook: Webhook) -> None:
    http, me, tenant = mailbox.http, mailbox.me, mailbox.tenant
    made = await http.post(
        f"{GRAPH}/me/subscriptions".replace("/me", ""),
        json={
            "changeType": "created,updated",
            "notificationUrl": webhook.url,
            "resource": "/me/mailFolders('Drafts')/messages",
            "expirationDateTime": _at(tenant, timedelta(days=2)),
            "clientState": "drafts",
        },
        headers=me,
    )
    assert made.status_code == 201, made.text
    created = await http.post(
        f"{GRAPH}/me/messages",
        json={"subject": "Room", "importance": "high", "body": {"contentType": "HTML", "content": "<p>Which?</p>"}},
        headers=me,
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["isDraft"] is True and draft["subject"] == "Room" and draft["importance"] == "high"
    assert "from" not in draft and "sender" not in draft and draft["toRecipients"] == []
    assert draft["body"] == {"contentType": "html", "content": "<p>Which?</p>"}
    assert draft["bodyPreview"] == "Which?" and draft["etag" if False else "changeKey"]
    assert [m["id"] for m in await mailbox.folder("drafts")] == [draft["id"]]
    assert sent_by_agent(tenant) == [], "a draft tells nobody"
    changed = await http.patch(
        f"{GRAPH}/me/messages/{draft['id']}",
        json={"subject": "Room?", "toRecipients": [SOFIA], "body": {"contentType": "text", "content": "Which room?"}},
        headers=me,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["subject"] == "Room?" and changed.json()["toRecipients"] == [SOFIA]
    assert (
        changed.json()["changeKey"] != draft["changeKey"] and changed.json()["odata_etag" if False else "@odata.etag"]
    )
    assert changed.json()["bodyPreview"] == "Which room?"
    again = (await http.get(f"{GRAPH}/me/messages/{draft['id']}", headers=me)).json()
    assert again["toRecipients"] == [SOFIA] and again["isDraft"] is True
    sent = await http.post(f"{GRAPH}/me/messages/{draft['id']}/send", headers=me)
    assert sent.status_code == 202 and sent.content == b""
    assert await mailbox.folder("drafts") == [], "sending takes the draft out of Drafts"
    gone = await http.get(f"{GRAPH}/me/messages/{draft['id']}", headers=me)
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "ErrorItemNotFound"
    [kept] = [m for m in await mailbox.folder("sentitems") if m["subject"] == "Room?"]
    assert kept["id"] != draft["id"] and kept["isDraft"] is False and kept["from"]["emailAddress"]["address"] == AGENT
    assert kept["conversationId"] == draft["conversationId"] and kept["importance"] == "high"
    [asked] = sent_by_agent(tenant)
    assert asked.after is not None and asked.after.recipient_emails == ["sofia@example.com"]  # type: ignore[union-attr]
    hers = (
        await http.get(
            f"{GRAPH}/users/sofia@example.com/mailFolders/inbox/messages",
            params={"$orderby": "subject"},
            headers=mailbox.app,
        )
    ).json()
    assert [m["subject"] for m in hers["value"] if m["subject"] == "Room?"] == ["Room?"]
    changes = [(n["value"][0]["changeType"], n["value"][0]["clientState"]) for n in webhook.notifications]
    assert changes == [("created", "drafts"), ("updated", "drafts")]


async def test_a_draft_with_no_recipient_cannot_be_sent_and_a_message_that_is_no_draft_is_refused_by_name(
    mailbox: Mailbox,
) -> None:
    http, me = mailbox.http, mailbox.me
    empty = (await http.post(f"{GRAPH}/me/messages", json={"subject": "Nobody"}, headers=me)).json()
    refused = await http.post(f"{GRAPH}/me/messages/{empty['id']}/send", headers=me)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "ErrorInvalidRecipients"
    assert [m["id"] for m in await mailbox.folder("drafts")] == [empty["id"]], "nothing was sent"
    received = await mailbox.inbox_message("Vendor review")
    edit = await http.patch(f"{GRAPH}/me/messages/{received['id']}", json={"subject": "Changed"}, headers=me)
    assert edit.status_code == 501 and "isDraft" in edit.json()["error"]["message"]
    send = await http.post(f"{GRAPH}/me/messages/{received['id']}/send", headers=me)
    assert send.status_code == 501
    attach = await http.post(
        f"{GRAPH}/me/messages/{received['id']}/attachments",
        json={"@odata.type": "#microsoft.graph.fileAttachment", "name": "a.txt", "contentBytes": "aGk="},
        headers=me,
    )
    assert attach.status_code == 501
    mime = await http.post(f"{GRAPH}/me/messages", content=b"RnJvbTo=", headers={**me, "Content-Type": "text/plain"})
    assert mime.status_code == 501
    unkept = await http.post(f"{GRAPH}/me/messages", json={"subject": "x", "categories": ["Red"]}, headers=me)
    assert unkept.status_code == 501 and "categories" in unkept.json()["error"]["message"]


async def test_a_reply_draft_answers_as_reply_does_and_takes_the_body_written_later(mailbox: Mailbox) -> None:
    http, me, tenant = mailbox.http, mailbox.me, mailbox.tenant
    received = await mailbox.inbox_message("Vendor review")
    both = await http.post(
        f"{GRAPH}/me/messages/{received['id']}/createReply",
        json={"comment": "x", "message": {"body": {"content": "y"}}},
        headers=me,
    )
    assert both.status_code == 400 and "code" not in both.json()["error"]
    made = await http.post(f"{GRAPH}/me/messages/{received['id']}/createReply", json={"comment": "On it."}, headers=me)
    assert made.status_code == 201, made.text
    draft = made.json()
    assert draft["isDraft"] is True and draft["subject"] == "RE: Vendor review"
    assert draft["toRecipients"][0]["emailAddress"]["address"] == "owen@example.com"
    assert draft["conversationId"] == received["conversationId"] and "body" not in draft
    patched = await http.patch(
        f"{GRAPH}/me/messages/{draft['id']}", json={"body": {"contentType": "text", "content": "Booked."}}, headers=me
    )
    assert patched.status_code == 200
    assert (await http.post(f"{GRAPH}/me/messages/{draft['id']}/send", headers=me)).status_code == 202
    [asked] = sent_by_agent(tenant)
    assert asked.after is not None and asked.after.channel == received["conversationId"]  # type: ignore[union-attr]
    assert asked.after.text.endswith("Booked.")  # type: ignore[union-attr]
    all_ = await http.post(f"{GRAPH}/me/messages/{received['id']}/createReplyAll", json={}, headers=me)
    assert all_.status_code == 201
    assert [r["emailAddress"]["address"] for r in all_.json()["toRecipients"]] == ["owen@example.com", AGENT]


async def test_a_forward_names_its_recipients_once_and_a_forward_draft_is_sent_later(mailbox: Mailbox) -> None:
    http, me, tenant = mailbox.http, mailbox.me, mailbox.tenant
    received = await mailbox.inbox_message("Vendor review")
    url = f"{GRAPH}/me/messages/{received['id']}"
    neither = await http.post(f"{url}/forward", json={"comment": "FYI"}, headers=me)
    both = await http.post(
        f"{url}/createForward", json={"toRecipients": [SOFIA], "message": {"toRecipients": [SOFIA]}}, headers=me
    )
    clash = await http.post(
        f"{url}/forward",
        json={"toRecipients": [SOFIA], "comment": "a", "message": {"body": {"content": "b"}}},
        headers=me,
    )
    assert [r.status_code for r in (neither, both, clash)] == [400, 400, 400]
    assert sent_by_agent(tenant) == []
    draft = await http.post(f"{url}/createForward", json={"toRecipients": [SOFIA], "comment": "FYI"}, headers=me)
    assert draft.status_code == 201, draft.text
    assert draft.json()["isDraft"] is True and draft.json()["toRecipients"] == [SOFIA]
    assert "subject" not in draft.json(), "no page says how Outlook composes a forward's subject"
    assert draft.json()["conversationId"] != received["conversationId"]
    sent = await http.post(f"{GRAPH}/me/messages/{draft.json()['id']}/send", headers=me)
    assert sent.status_code == 202
    direct = await http.post(
        f"{url}/forward", json={"toRecipients": [SOFIA], "message": {"subject": "Fwd"}}, headers=me
    )
    assert direct.status_code == 202 and direct.content == b""
    assert [e.after.recipient_emails for e in sent_by_agent(tenant) if e.after is not None] == [  # type: ignore[union-attr]
        ["sofia@example.com"],
        ["sofia@example.com"],
    ]
    assert "Fwd" in [m.get("subject") for m in await mailbox.folder("sentitems")]


async def test_move_makes_a_new_copy_in_the_folder_and_removes_the_original_and_copy_keeps_it(
    mailbox: Mailbox, webhook: Webhook
) -> None:
    http, me, tenant = mailbox.http, mailbox.me, mailbox.tenant
    made = await http.post(
        f"{GRAPH}/subscriptions",
        json={
            "changeType": "created",
            "notificationUrl": webhook.url,
            "resource": "/me/mailFolders('DeletedItems')/messages",
            "expirationDateTime": _at(tenant, timedelta(days=2)),
            "clientState": "bin",
        },
        headers=me,
    )
    assert made.status_code == 201, made.text
    received = await mailbox.inbox_message("Lunch")
    copied = await http.post(f"{GRAPH}/me/messages/{received['id']}/copy", json={"destinationId": "drafts"}, headers=me)
    assert copied.status_code == 201, copied.text
    assert copied.json()["id"] != received["id"] and copied.json()["subject"] == "Lunch"
    assert copied.json()["parentFolderId"] != received["parentFolderId"]
    assert (await http.get(f"{GRAPH}/me/messages/{received['id']}", headers=me)).status_code == 200
    moved = await http.post(
        f"{GRAPH}/me/messages/{received['id']}/move", json={"destinationId": "deleteditems"}, headers=me
    )
    assert moved.status_code == 201, moved.text
    after = moved.json()
    assert after["id"] != received["id"] and after["createdDateTime"] == received["createdDateTime"]
    assert after["parentFolderId"] == (await http.get(f"{GRAPH}/me/mailFolders/deleteditems", headers=me)).json()["id"]
    assert (await http.get(f"{GRAPH}/me/messages/{received['id']}", headers=me)).status_code == 404
    assert [m["id"] for m in await mailbox.folder("deleteditems")] == [after["id"]]
    assert [m["subject"] for m in await mailbox.folder("inbox") if m["subject"] == "Lunch"] == []
    [note] = webhook.notifications
    assert note["value"][0]["clientState"] == "bin" and note["value"][0]["resourceData"]["id"] == after["id"]
    elsewhere = await http.post(
        f"{GRAPH}/me/messages/{after['id']}/move", json={"destinationId": "junkemail"}, headers=me
    )
    assert elsewhere.status_code == 501, "a folder the mailbox does not hold"


async def test_attachments_are_kept_as_sent_listed_read_and_carried_to_each_recipient(mailbox: Mailbox) -> None:
    http, me = mailbox.http, mailbox.me
    raw = bytes(range(256)) * 4
    content = base64.b64encode(raw).decode()
    draft = (
        await http.post(
            f"{GRAPH}/me/messages",
            json={"subject": "Files", "toRecipients": [SOFIA]},
            headers=me,
        )
    ).json()
    assert draft["hasAttachments"] is False
    url = f"{GRAPH}/me/messages/{draft['id']}/attachments"
    one = await http.post(
        url,
        json={"@odata.type": "#microsoft.graph.fileAttachment", "name": "b.bin", "contentType": "application/octet-stream",
              "contentBytes": content},
        headers=me,
    )  # fmt: skip
    assert one.status_code == 201, one.text
    assert one.json()["contentBytes"] == content and one.json()["size"] == len(raw)
    assert one.json()["@odata.type"] == "#microsoft.graph.fileAttachment" and one.json()["isInline"] is False
    two = await http.post(
        url,
        json={"@odata.type": "#microsoft.graph.fileAttachment", "name": "a.txt", "contentBytes": "aGk="},
        headers=me,
    )
    assert two.json()["size"] == 2 and two.json()["name"] == "a.txt"
    assert (await http.get(f"{GRAPH}/me/messages/{draft['id']}", headers=me)).json()["hasAttachments"] is True
    unordered = await http.get(url, headers=me)
    assert unordered.status_code == 501, "message-list-attachments documents no order"
    listed = (await http.get(url, params={"$orderby": "name"}, headers=me)).json()["value"]
    assert [a["name"] for a in listed] == ["a.txt", "b.bin"]
    read = await http.get(f"{url}/{one.json()['id']}", headers=me)
    assert read.status_code == 200 and read.json()["contentBytes"] == content and "@odata.context" in read.json()
    missing = await http.get(f"{url}/nope", headers=me)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "ErrorItemNotFound"
    selected = (await http.get(url, params={"$orderby": "size", "$select": "name,size"}, headers=me)).json()["value"]
    assert [(a["id"], a["name"], a["size"]) for a in selected] == [
        (two.json()["id"], "a.txt", 2),
        (one.json()["id"], "b.bin", len(raw)),
    ]
    assert all(set(a) == {"@odata.type", "id", "name", "size"} for a in selected)
    assert (await http.post(f"{GRAPH}/me/messages/{draft['id']}/send", headers=me)).status_code == 202
    [theirs] = [
        m
        for m in (await http.get(f"{GRAPH}/users/sofia@example.com/mailFolders/inbox/messages", params={"$orderby": "subject"}, headers=mailbox.app)).json()["value"]
        if m["subject"] == "Files"
    ]  # fmt: skip
    assert theirs["hasAttachments"] is True
    kept = (
        await http.get(
            f"{GRAPH}/users/sofia@example.com/messages/{theirs['id']}/attachments",
            params={"$orderby": "name"},
            headers=mailbox.app,
        )
    ).json()
    assert [a["contentBytes"] for a in kept["value"]] == ["aGk=", content]
    assert {a["id"] for a in kept["value"]}.isdisjoint({one.json()["id"], two.json()["id"]})
    sent_copy = await mailbox.folder("sentitems", "Files")
    assert sent_copy[0]["hasAttachments"] is True


async def test_send_mail_carries_its_attachments_and_what_is_not_a_small_file_is_refused_by_name(
    mailbox: Mailbox,
) -> None:
    http, me = mailbox.http, mailbox.me
    sent = await http.post(
        f"{GRAPH}/me/sendMail",
        json={"message": {"subject": "Logo", "toRecipients": [SOFIA], "attachments": [
            {"@odata.type": "#microsoft.graph.fileAttachment", "name": "logo.png", "isInline": True,
             "contentId": "logo", "contentBytes": "aGk="}]}},
        headers=me,
    )  # fmt: skip
    assert sent.status_code == 202, sent.text
    [mine] = await mailbox.folder("sentitems", "Logo")
    assert mine["hasAttachments"] is False, "an inline attachment does not count (resources/message)"
    listed = await http.get(f"{GRAPH}/me/messages/{mine['id']}/attachments", headers=me)
    [inline] = listed.json()["value"]
    assert inline["isInline"] is True and inline["contentId"] == "logo"
    draft = (await http.post(f"{GRAPH}/me/messages", json={"subject": "Big"}, headers=me)).json()
    url = f"{GRAPH}/me/messages/{draft['id']}/attachments"
    big = base64.b64encode(bytes(3 * 1024 * 1024)).decode()
    refused = [
        {"@odata.type": "#microsoft.graph.fileAttachment", "name": "big", "contentBytes": big},
        {"@odata.type": "#microsoft.graph.itemAttachment", "name": "item", "item": {}},
        {"@odata.type": "#microsoft.graph.referenceAttachment", "name": "link", "sourceUrl": "https://x.example"},
        {"@odata.type": "#microsoft.graph.fileAttachment", "name": "bad", "contentBytes": "***"},
    ]
    for body in refused:
        answered = await http.post(url, json=body, headers=me)
        assert answered.status_code == 501, body["name"]
    upload = await http.post(f"{url}/createUploadSession", json={"AttachmentItem": {}}, headers=me)
    assert upload.status_code == 501 and "createUploadSession" in upload.json()["error"]["message"]
    assert (await http.get(url, headers=me)).json()["value"] == [], "nothing refused was kept"
