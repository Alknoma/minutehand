"""Teams messaging through Graph: a signed-in user posts to a chat and a channel and replies, the messages round-trip
as sent, subscriptions are notified, a one-on-one or group chat is created, and `joinedTeams` and `primaryChannel`
answer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

import httpx
import pytest

from tests.providers.microsoft.outlook import signed_in
from tests.providers.microsoft.tenant import GRAPH, Intercepted, Tenant, Webhook, bearer, token


@dataclass
class Posting:
    http: httpx.AsyncClient
    app: dict[str, str]
    owen: dict[str, str]
    sofia: dict[str, str]
    tenant: Tenant

    @property
    def team(self) -> str:
        return self.tenant.directory.team_id

    @property
    def general(self) -> str:
        return self.tenant.directory.general_channel_id

    def user(self, key: str) -> str:
        found = self.tenant.world.person(key)
        assert found is not None
        return found.user.id

    def chat_of(self, key: str) -> str:
        chat = self.tenant.world.personal_with(self.user(key), self.tenant.directory.tenant_id)
        assert chat is not None
        return chat.graph_id


@pytest.fixture
async def posting(tenant: Tenant, microsoft: Intercepted) -> AsyncIterator[Posting]:
    async with microsoft.http() as http:
        yield Posting(
            http=http,
            app=bearer(await token(http, tenant, "https://graph.microsoft.com/.default")),
            owen=bearer(await signed_in(http, tenant, "owen@example.com")),
            sofia=bearer(await signed_in(http, tenant, "sofia@example.com")),
            tenant=tenant,
        )


def _later(posting: Posting) -> None:
    posting.tenant.clock.jump(posting.tenant.clock.now() + timedelta(minutes=1))


async def _watch(posting: Posting, webhook: Webhook, resource: str) -> None:
    expires = (posting.tenant.clock.now() + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    made = await posting.http.post(
        f"{GRAPH}/subscriptions",
        json={
            "changeType": "created",
            "notificationUrl": webhook.url,
            "resource": resource,
            "expirationDateTime": expires,
            "clientState": "kept",
        },
        headers=posting.app,
    )
    assert made.status_code == 201, made.text


async def test_a_message_posted_to_a_chat_is_kept_as_sent_read_back_and_notified(
    posting: Posting, webhook: Webhook
) -> None:
    chat = posting.chat_of("owen")
    await _watch(posting, webhook, f"/chats/{chat}/messages")
    html = '<p>See <at id="0">Sofia</at></p><attachment id="74d20c7f"></attachment>'
    card = '{"title": "Card", "buttons": []}'
    sent = {
        "subject": "Plan",
        "importance": "high",
        "body": {"contentType": "html", "content": html},
        "mentions": [
            {"id": 0, "mentionText": "Sofia", "mentioned": {"user": {"id": posting.user("sofia"), "displayName": "Sofia"}}}
        ],
        "attachments": [{"id": "74d20c7f", "contentType": "application/vnd.microsoft.card.thumbnail", "content": card}],
    }
    posted = await posting.http.post(f"{GRAPH}/chats/{chat}/messages", json=sent, headers=posting.owen)
    assert posted.status_code == 201, posted.text
    made = posted.json()
    assert made["@odata.context"] == f"{GRAPH}/$metadata#chats('{chat}')/messages/$entity"
    assert made["body"] == {"contentType": "html", "content": html}
    assert made["subject"] == "Plan" and made["importance"] == "high" and made["chatId"] == chat
    assert made["from"]["user"]["id"] == posting.user("owen") and made["from"]["user"]["displayName"] == "Owen Okafor"
    assert made["attachments"] == [
        {"id": "74d20c7f", "contentType": "application/vnd.microsoft.card.thumbnail", "content": card}
    ], "a card's content stays the string it was sent as"
    assert made["mentions"][0]["mentionText"] == "Sofia" and made["mentions"][0]["mentioned"]["user"]["id"] == posting.user("sofia")
    assert made["createdDateTime"] == "2026-09-14T08:30:00.000Z" and made["etag"] == made["id"]
    read = await posting.http.get(f"{GRAPH}/chats/{chat}/messages/{made['id']}", headers=posting.owen)
    assert {k: v for k, v in read.json().items() if k != "@odata.context"} == {
        k: v for k, v in made.items() if k != "@odata.context"
    }
    listed = (await posting.http.get(f"{GRAPH}/chats/{chat}/messages", headers=posting.owen)).json()["value"]
    assert [m["id"] for m in listed] == [made["id"]]
    [notified] = webhook.notifications
    change = notified["value"][0]
    assert change["changeType"] == "created" and change["clientState"] == "kept"
    assert change["resource"] == f"chats('{chat}')/messages('{made['id']}')"
    assert change["resourceData"]["id"] == made["id"]


async def test_a_text_body_defaults_to_text_and_pages_with_others(posting: Posting) -> None:
    chat = posting.chat_of("owen")
    ids: list[str] = []
    for n in range(3):
        _later(posting)
        posted = await posting.http.post(
            f"{GRAPH}/chats/{chat}/messages", json={"body": {"content": f"note {n}"}}, headers=posting.owen
        )
        assert posted.json()["body"] == {"contentType": "text", "content": f"note {n}"}
        assert posted.json()["importance"] == "normal"
        ids.append(posted.json()["id"])
    first = (await posting.http.get(f"{GRAPH}/chats/{chat}/messages", params={"$top": "2"}, headers=posting.owen)).json()
    assert [m["id"] for m in first["value"]] == [ids[2], ids[1]]
    second = (await posting.http.get(first["@odata.nextLink"], headers=posting.owen)).json()
    assert [m["id"] for m in second["value"]] == [ids[0]] and "@odata.nextLink" not in second


async def test_a_channel_post_and_a_reply_round_trip_and_notify(posting: Posting, webhook: Webhook) -> None:
    url = f"{GRAPH}/teams/{posting.team}/channels/{posting.general}/messages"
    await _watch(posting, webhook, f"/teams/{posting.team}/channels/{posting.general}/messages")
    root = await posting.http.post(url, json={"body": {"content": "Standup?"}}, headers=posting.owen)
    assert root.status_code == 201, root.text
    assert "replyToId" not in root.json() and "chatId" not in root.json()
    assert root.json()["channelIdentity"] == {"teamId": posting.team, "channelId": posting.general}
    _later(posting)
    reply = await posting.http.post(
        f"{url}/{root.json()['id']}/replies", json={"body": {"content": "Yes", "contentType": "html"}}, headers=posting.sofia
    )
    assert reply.status_code == 201, reply.text
    assert reply.json()["replyToId"] == root.json()["id"] and reply.json()["from"]["user"]["displayName"] == "Sofia Romano"
    assert "replies/$entity" in reply.json()["@odata.context"]
    listed = (await posting.http.get(url, params={"$expand": "replies"}, headers=posting.owen)).json()["value"]
    assert [m["id"] for m in listed] == [root.json()["id"]]
    assert [r["body"]["content"] for r in listed[0]["replies"]] == ["Yes"]
    replies = (await posting.http.get(f"{url}/{root.json()['id']}/replies", headers=posting.owen)).json()["value"]
    assert [r["id"] for r in replies] == [reply.json()["id"]]
    resources = [n["value"][0]["resource"] for n in webhook.notifications]
    assert resources == [
        f"teams('{posting.team}')/channels('{posting.general}')/messages('{root.json()['id']}')",
        f"teams('{posting.team}')/channels('{posting.general}')/messages('{reply.json()['id']}')",
    ]


async def test_what_graph_documents_no_answer_to_is_refused_by_name(posting: Posting) -> None:
    chat = posting.chat_of("owen")
    url = f"{GRAPH}/chats/{chat}/messages"
    application = await posting.http.post(url, json={"body": {"content": "x"}}, headers=posting.app)
    assert application.status_code == 501 and "migration" in application.json()["error"]["message"]
    hosted = await posting.http.post(
        url, json={"body": {"content": "x"}, "hostedContents": [{"temporaryId": "1"}]}, headers=posting.owen
    )
    assert hosted.status_code == 501 and "hostedContents" in hosted.json()["error"]["message"]
    kind = await posting.http.post(url, json={"body": {"contentType": "markdown", "content": "x"}}, headers=posting.owen)
    assert kind.status_code == 501
    stranger = await posting.http.post(
        f"{GRAPH}/chats/{posting.chat_of('sofia')}/messages", json={"body": {"content": "x"}}, headers=posting.owen
    )
    assert stranger.status_code == 501 and "no member" in stranger.json()["error"]["message"]
    nested = await posting.http.post(
        f"{GRAPH}/teams/{posting.team}/channels/{posting.general}/messages", json={"body": {"content": "r"}}, headers=posting.owen
    )
    again = await posting.http.post(
        f"{GRAPH}/teams/{posting.team}/channels/{posting.general}/messages/{nested.json()['id']}/replies",
        json={"body": {"content": "one"}},
        headers=posting.owen,
    )
    deeper = await posting.http.post(
        f"{GRAPH}/teams/{posting.team}/channels/{posting.general}/messages/{again.json()['id']}/replies",
        json={"body": {"content": "two"}},
        headers=posting.owen,
    )
    assert deeper.status_code == 501
    unmade = await posting.http.get(url, headers=posting.owen)
    assert unmade.json()["value"] == [], "nothing refused was kept"


async def test_a_one_on_one_chat_between_two_people_is_made_once_and_a_group_with_its_topic(posting: Posting) -> None:
    owen, sofia, dania = posting.user("owen"), posting.user("sofia"), posting.user("dania")

    def members(*ids: str) -> list[dict[str, object]]:
        return [{"@odata.type": "#microsoft.graph.aadUserConversationMember", "roles": ["owner"],
                 "user@odata.bind": f"{GRAPH}/users('{i}')"} for i in ids]  # fmt: skip

    made = await posting.http.post(
        f"{GRAPH}/chats", json={"chatType": "oneOnOne", "members": members(owen, sofia)}, headers=posting.owen
    )
    assert made.status_code == 201, made.text
    chat = made.json()
    assert chat["chatType"] == "oneOnOne" and chat["id"].startswith("19:") and chat["id"].endswith("@unq.gbl.spaces")
    assert chat["createdDateTime"] == "2026-09-14T08:30:00.000Z" and "topic" not in chat
    again = await posting.http.post(
        f"{GRAPH}/chats", json={"chatType": "oneOnOne", "members": members(sofia, owen)}, headers=posting.sofia
    )
    assert again.status_code == 201 and again.json()["id"] == chat["id"], "chat-post: an existing one is returned"
    group = await posting.http.post(
        f"{GRAPH}/chats",
        json={"chatType": "group", "topic": "Vendor review", "members": members(owen, sofia, dania)},
        headers=posting.owen,
    )
    assert group.status_code == 201, group.text
    assert group.json()["chatType"] == "group" and group.json()["topic"] == "Vendor review"
    assert group.json()["id"].endswith("@thread.v2")
    theirs = (await posting.http.get(f"{GRAPH}/chats", headers=posting.sofia)).json()["value"]
    assert {c["id"] for c in theirs} >= {chat["id"], group.json()["id"]}
    listed = (await posting.http.get(f"{GRAPH}/chats/{group.json()['id']}/members", headers=posting.owen)).json()["value"]
    assert sorted(m["userId"] for m in listed) == sorted([owen, sofia, dania])
    spoken = await posting.http.post(
        f"{GRAPH}/chats/{chat['id']}/messages", json={"body": {"content": "Hi Sofia"}}, headers=posting.owen
    )
    assert spoken.status_code == 201 and spoken.json()["chatId"] == chat["id"]
    read = await posting.http.get(f"{GRAPH}/chats/{chat['id']}/messages", headers=posting.sofia)
    assert [m["body"]["content"] for m in read.json()["value"]] == ["Hi Sofia"]
    without_self = await posting.http.post(
        f"{GRAPH}/chats", json={"chatType": "oneOnOne", "members": members(sofia, dania)}, headers=posting.owen
    )
    assert without_self.status_code == 501
    guest = await posting.http.post(
        f"{GRAPH}/chats",
        json={"chatType": "oneOnOne", "members": [{**members(owen)[0]}, {**members(dania)[0], "roles": ["guest"]}]},
        headers=posting.owen,
    )
    assert guest.status_code == 501


async def test_joined_teams_and_the_primary_channel(posting: Posting) -> None:
    mine = await posting.http.get(f"{GRAPH}/me/joinedTeams", headers=posting.owen)
    assert mine.status_code == 200
    [team] = mine.json()["value"]
    assert team["id"] == posting.team and set(team) == {"id", "displayName", "isArchived", "tenantId"}
    assert team["isArchived"] is False and mine.json()["@odata.context"] == f"{GRAPH}/$metadata#teams"
    other = await posting.http.get(f"{GRAPH}/users/{posting.user('sofia')}/joinedTeams", headers=posting.app)
    assert [t["id"] for t in other.json()["value"]] == [posting.team]
    optioned = await posting.http.get(f"{GRAPH}/me/joinedTeams", params={"$top": "1"}, headers=posting.owen)
    assert optioned.status_code == 501, "user-list-joinedteams: no OData query parameter is supported"
    primary = await posting.http.get(f"{GRAPH}/teams/{posting.team}/primaryChannel", headers=posting.app)
    assert primary.status_code == 200
    assert primary.json()["id"] == posting.general and primary.json()["displayName"] == "General"
    assert "primaryChannel/$entity" in primary.json()["@odata.context"]
