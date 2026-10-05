"""The Bot Framework connector, called as a bot calls it: every route, and each refusal in the connector's shape."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest

from minutehand.adapters.providers.microsoft.state import ConversationRecord, UserRecord
from minutehand.domain.world import Actor, Operation
from tests.providers.microsoft.tenant import CONNECTOR, Intercepted, Tenant, bearer, token


@dataclass
class Bot:
    http: httpx.AsyncClient
    auth: dict[str, str]
    tenant: Tenant

    def chat(self, person: str) -> tuple[UserRecord, ConversationRecord]:
        user = self.tenant.world.person(person)
        assert user is not None
        chat = self.tenant.world.personal_with(user.user.id, self.tenant.directory.tenant_id)
        assert chat is not None
        return user, chat

    @property
    def general(self) -> str:
        return self.tenant.directory.general_channel_id


@pytest.fixture
async def connector(tenant: Tenant, microsoft: Intercepted) -> AsyncIterator[Bot]:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://api.botframework.com/.default"))
        yield Bot(http=http, auth=auth, tenant=tenant)


def error_code(answered: httpx.Response, status: int) -> str:
    assert answered.status_code == status, answered.text
    return str(answered.json()["error"]["code"])


async def test_a_reply_in_a_channel_is_threaded_under_its_root(connector: Bot) -> None:
    root = await connector.http.post(
        f"{CONNECTOR}v3/conversations/{connector.general}/activities",
        json={"type": "message", "text": "Weekly status"},
        headers=connector.auth,
    )
    root_id = root.json()["id"]
    by_suffix = await connector.http.post(
        f"{CONNECTOR}v3/conversations/{connector.general};messageid={root_id}/activities",
        json={"type": "message", "text": "first reply"},
        headers=connector.auth,
    )
    by_route = await connector.http.post(
        f"{CONNECTOR}v3/conversations/{connector.general}/activities/{root_id}",
        json={"type": "message", "text": "second reply"},
        headers=connector.auth,
    )
    for answered in (by_suffix, by_route):
        assert answered.status_code == 201
        found = connector.tenant.world.message(answered.json()["id"])
        assert found is not None and found[1].replyToId == root_id


async def test_update_and_delete_change_only_the_bots_own_activity(connector: Bot) -> None:
    _, chat = connector.chat("sofia")
    url = f"{CONNECTOR}v3/conversations/{chat.id}/activities"
    sent = (await connector.http.post(url, json={"type": "message", "text": "draft"}, headers=connector.auth)).json()
    updated = await connector.http.put(
        f"{url}/{sent['id']}", json={"type": "message", "id": sent["id"], "text": "final"}, headers=connector.auth
    )
    assert updated.status_code == 200
    found = connector.tenant.world.message(sent["id"])
    assert found is not None and found[1].text == "final"
    deleted = await connector.http.delete(f"{url}/{sent['id']}", headers=connector.auth)
    assert deleted.status_code == 200
    assert connector.tenant.world.message(sent["id"]) is None
    last = connector.tenant.store.events()[-1]
    assert (last.actor, last.operation) == (Actor.AGENT, Operation.DELETE)


async def test_an_update_with_text_and_a_card_together_is_refused(connector: Bot) -> None:
    _, chat = connector.chat("sofia")
    url = f"{CONNECTOR}v3/conversations/{chat.id}/activities"
    sent = (await connector.http.post(url, json={"type": "message", "text": "x"}, headers=connector.auth)).json()
    both = {
        "type": "message",
        "text": "x",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": {"type": "AdaptiveCard"}}
        ],
    }
    assert (
        error_code(await connector.http.put(f"{url}/{sent['id']}", json=both, headers=connector.auth), 400)
        == "BadSyntax"
    )


async def test_a_proactive_conversation_answers_the_persons_chat(connector: Bot) -> None:
    user, chat = connector.chat("dania")
    created = await connector.http.post(
        f"{CONNECTOR}v3/conversations",
        json={
            "bot": {"id": connector.tenant.directory.bot_app_id},
            "members": [{"id": user.mri}],
            "channelData": {"tenant": {"id": connector.tenant.directory.tenant_id}},
            "isGroup": False,
        },
        headers=connector.auth,
    )
    assert created.status_code == 201 and created.json()["id"] == chat.id


async def test_a_proactive_conversation_with_a_person_who_never_installed_the_bot_is_refused(connector: Bot) -> None:
    user, chat = connector.chat("dania")
    world = connector.tenant.world
    world.write_conversation(
        chat.model_copy(update={"bot_installed": False}), operation=Operation.UPDATE, actor=Actor.SCENARIO
    )
    refused = await connector.http.post(
        f"{CONNECTOR}v3/conversations",
        json={"members": [{"id": user.mri}], "channelData": {"tenant": {"id": connector.tenant.directory.tenant_id}}},
        headers=connector.auth,
    )
    assert error_code(refused, 403) == "Forbidden"
    sending = await connector.http.post(
        f"{CONNECTOR}v3/conversations/{chat.id}/activities",
        json={"type": "message", "text": "hi"},
        headers=connector.auth,
    )
    assert error_code(sending, 403) == "BotNotInConversationRoster"


async def test_paged_members_follow_the_continuation_token(connector: Bot) -> None:
    seen: list[str] = []
    continuation: str | None = None
    while True:
        params = {"pageSize": "1", **({"continuationToken": continuation} if continuation else {})}
        page = (
            await connector.http.get(
                f"{CONNECTOR}v3/conversations/{connector.general}/pagedmembers", params=params, headers=connector.auth
            )
        ).json()
        seen += [m["email"] for m in page["members"]]
        continuation = page["continuationToken"] if "continuationToken" in page else None
        if continuation is None:
            break
    assert sorted(seen) == ["dania@example.com", "owen@example.com", "sofia@example.com"]
    user, _ = connector.chat("sofia")
    one = await connector.http.get(
        f"{CONNECTOR}v3/conversations/{connector.general}/members/{user.mri}", headers=connector.auth
    )
    assert one.json()["aadObjectId"] == user.user.id


async def test_a_team_and_its_channels_are_answered_even_with_a_doubled_slash(connector: Bot) -> None:
    team = await connector.http.get(f"{CONNECTOR}/v3/teams/{connector.general}", headers=connector.auth)
    assert team.status_code == 200 and team.json()["aadGroupId"] == connector.tenant.directory.team_id
    channels = await connector.http.get(
        f"{CONNECTOR}/v3/teams/{connector.general}/conversations", headers=connector.auth
    )
    assert [c["id"] for c in channels.json()["conversations"]] == [connector.general]


async def test_calls_without_a_connector_token_are_refused(tenant: Tenant, microsoft: Intercepted) -> None:
    url = f"{CONNECTOR}v3/conversations/{tenant.directory.general_channel_id}/activities"
    async with microsoft.http() as http:
        none = await http.post(url, json={"type": "message", "text": "x"})
        graph = await token(http, tenant, "https://graph.microsoft.com/.default")
        wrong_audience = await http.post(url, json={"type": "message", "text": "x"}, headers=bearer(graph))
        forged = await http.post(url, json={"type": "message", "text": "x"}, headers=bearer(graph[:-4] + "AAAA"))
    for answered in (none, wrong_audience, forged):
        assert answered.status_code == 401
        assert answered.json() == {"message": "Authorization has been denied for this request."}


async def test_an_unknown_conversation_and_an_oversized_activity_are_refused(connector: Bot) -> None:
    missing = await connector.http.post(
        f"{CONNECTOR}v3/conversations/a:nobody/activities",
        json={"type": "message", "text": "x"},
        headers=connector.auth,
    )
    assert error_code(missing, 404) == "ConversationNotFound"
    huge = await connector.http.post(
        f"{CONNECTOR}v3/conversations/{connector.general}/activities",
        json={"type": "message", "text": "x" * (29 * 1024)},
        headers=connector.auth,
    )
    assert error_code(huge, 413) == "MessageSizeTooBig"
