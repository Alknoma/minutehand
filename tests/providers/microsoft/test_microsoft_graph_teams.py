"""Graph for people and Teams, with the query shapes the services build, and each unknown shape refused."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest

from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import PersonPosts
from minutehand.domain.world import EntityKind, EntityRef
from tests.providers.microsoft.tenant import CONNECTOR, GRAPH, Bot, Intercepted, Tenant, bearer, token


@dataclass
class Graph:
    http: httpx.AsyncClient
    auth: dict[str, str]
    connector: dict[str, str]
    tenant: Tenant


@pytest.fixture
async def graph(tenant: Tenant, microsoft: Intercepted) -> AsyncIterator[Graph]:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        connector = bearer(await token(http, tenant, "https://api.botframework.com/.default"))
        yield Graph(http=http, auth=auth, connector=connector, tenant=tenant)


async def test_users_by_id_principal_name_and_filter_with_select(graph: Graph) -> None:
    sofia = graph.tenant.world.person("sofia")
    assert sofia is not None
    one = (
        await graph.http.get(
            f"{GRAPH}/users/{sofia.user.id}", params={"$select": "mail,userPrincipalName"}, headers=graph.auth
        )
    ).json()
    assert set(one) == {"@odata.context", "id", "mail", "userPrincipalName"}
    by_upn = (await graph.http.get(f"{GRAPH}/users/{sofia.user.userPrincipalName}", headers=graph.auth)).json()
    assert by_upn["id"] == sofia.user.id
    filtered = await graph.http.get(
        f"{GRAPH}/users", params={"$filter": "mail eq 'sofia@example.com'"}, headers=graph.auth
    )
    assert [u["id"] for u in filtered.json()["value"]] == [sofia.user.id]
    unknown = await graph.http.get(f"{GRAPH}/users", params={"$filter": "jobTitle ne 'x'"}, headers=graph.auth)
    assert unknown.status_code == 400 and unknown.json()["error"]["code"] == "invalidRequest"
    me = await graph.http.get(f"{GRAPH}/me", headers=graph.auth)
    assert me.status_code == 400


async def test_channels_by_name_and_their_messages_paged_with_replies(graph: Graph, bot: Bot) -> None:
    team = graph.tenant.directory.team_id
    general = graph.tenant.directory.general_channel_id
    named = await graph.http.get(
        f"{GRAPH}/teams/{team}/channels", params={"$filter": "displayName eq 'General'"}, headers=graph.auth
    )
    assert [c["id"] for c in named.json()["value"]] == [general]
    roots = [
        await graph.http.post(
            f"{CONNECTOR}v3/conversations/{general}/activities",
            json={"type": "message", "text": f"update {n}"},
            headers=graph.connector,
        )
        for n in range(3)
    ]
    root_id = roots[-1].json()["id"]
    await graph.tenant.provider.deliver(
        PersonReply(
            person="sofia",
            in_reply_to=EntityRef(provider="microsoft", kind=EntityKind.MESSAGE, external_id=root_id),
            text="Seen it.",
            at=graph.tenant.clock.now(),
        ),
        bot.target(),
        graph.tenant.store,
        graph.tenant.clock,
        secret="unused",
    )
    pushed = bot.accepted()[-1]
    assert pushed["conversation"]["id"] == f"{general};messageid={root_id}"
    assert pushed["entities"][0]["mentioned"]["id"] == f"28:{graph.tenant.directory.bot_app_id}"
    url = f"{GRAPH}/teams/{team}/channels/{general}/messages"
    first = (await graph.http.get(url, params={"$top": "2", "$expand": "replies"}, headers=graph.auth)).json()
    assert [m["body"]["content"] for m in first["value"]] == ["update 2", "update 1"]
    assert first["value"][0]["replies"][0]["from"]["user"]["displayName"] == "Sofia Romano"
    assert first["value"][0]["from"]["application"]["id"] == graph.tenant.directory.bot_app_id
    second = (await graph.http.get(first["@odata.nextLink"], headers=graph.auth)).json()
    assert [m["body"]["content"] for m in second["value"]] == ["update 0"] and "@odata.nextLink" not in second
    replies = (await graph.http.get(f"{url}/{root_id}/replies", params={"$top": "50"}, headers=graph.auth)).json()
    assert [r["replyToId"] for r in replies["value"]] == [root_id]
    too_many = await graph.http.get(url, params={"$top": "51"}, headers=graph.auth)
    assert too_many.status_code == 400


async def test_a_post_in_a_channel_reaches_the_bot_only_when_it_is_mentioned(graph: Graph, bot: Bot) -> None:
    for mentions in (False, True):
        await graph.tenant.provider.happen(
            PersonPosts(
                provider="microsoft", person="dania", channel="general", text="Anyone free?", mentions_agent=mentions
            ),
            bot.target(),
            graph.tenant.store,
            graph.tenant.clock,
            secret="unused",
        )
    [pushed] = bot.accepted()
    assert pushed["text"] == "<at>Agent</at> Anyone free?"
    listed = await graph.http.get(
        f"{GRAPH}/teams/{graph.tenant.directory.team_id}/channels/{graph.tenant.directory.general_channel_id}/messages",
        headers=graph.auth,
    )
    assert len(listed.json()["value"]) == 2


async def test_chats_their_messages_and_members(graph: Graph) -> None:
    sofia = graph.tenant.world.person("sofia")
    assert sofia is not None
    chat = graph.tenant.world.personal_with(sofia.user.id, graph.tenant.directory.tenant_id)
    assert chat is not None
    await graph.http.post(
        f"{CONNECTOR}v3/conversations/{chat.id}/activities",
        json={"type": "message", "text": "Hello"},
        headers=graph.connector,
    )
    messages = (
        await graph.http.get(f"{GRAPH}/chats/{chat.graph_id}/messages", params={"$top": "10"}, headers=graph.auth)
    ).json()
    assert [m["body"]["content"] for m in messages["value"]] == ["Hello"]
    assert messages["value"][0]["chatId"] == chat.graph_id
    members = (await graph.http.get(f"{GRAPH}/chats/{chat.graph_id}/members", headers=graph.auth)).json()
    assert [m["userId"] for m in members["value"]] == [sofia.user.id]
    every = await graph.http.get(f"{GRAPH}/chats", headers=graph.auth)
    assert every.status_code == 501 and "no signed-in user" in every.json()["error"]["message"]
    sent = await graph.http.post(f"{GRAPH}/chats/{chat.graph_id}/messages", json={}, headers=graph.auth)
    assert sent.status_code == 501 and sent.json()["error"]["code"] == "not_implemented"


async def test_a_team_unknown_is_refused_in_graphs_shape(graph: Graph) -> None:
    answered = await graph.http.get(f"{GRAPH}/teams/not-a-team/channels", headers=graph.auth)
    assert answered.status_code == 404
    assert set(answered.json()["error"]) == {"code", "message", "innerError"}
    no_token = await graph.http.get(f"{GRAPH}/users")
    assert no_token.status_code == 200 and no_token.json()["value"]
