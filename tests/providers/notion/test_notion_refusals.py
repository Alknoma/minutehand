"""What Notion refuses, with its status, its code and its error object, driven over the app directly."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.providers.notion.notion_world import (
    AGENT_TOKEN,
    API,
    CLIENT_ID,
    CLIENT_SECRET,
    CODE,
    NOTION,
    READER_TOKEN,
    REDIRECT,
    World,
    answer,
    direct,
    ids,
    refusal,
    scenario,
    seeded,
)


def paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


async def first_block(api: httpx.AsyncClient, index: int = 0) -> dict[str, Any]:
    return answer(await api.get(f"/v1/blocks/{ids('handbook')}/children"))["results"][index]


# --------------------------------------------------------------------------- sign-in and version


async def test_a_call_without_a_token_is_refused_unauthorized(world: World) -> None:
    async with direct(world, token=None) as bare:
        refusal(await bare.get("/v1/users/me"), 401, "unauthorized")


async def test_an_unknown_token_is_refused_unauthorized(world: World) -> None:
    async with direct(world, token="ntn_nobody_seeded_this") as stranger:
        refusal(await stranger.post("/v1/search", json={}), 401, "unauthorized")


async def test_a_call_without_a_version_is_refused_missing_version(world: World) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.provider.app(world.store, world.clock)),
        base_url=API,
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
    ) as unversioned:
        refusal(await unversioned.get("/v1/users/me"), 400, "missing_version")


async def test_a_version_this_fake_does_not_serve_is_refused_saying_so(api: httpx.AsyncClient) -> None:
    message = refusal(await api.get("/v1/users/me", headers={"Notion-Version": "2025-09-03"}), 400, "validation_error")
    assert "2022-06-28" in message


async def test_an_unknown_path_and_a_wrong_method_are_refused(api: httpx.AsyncClient) -> None:
    refusal(await api.get("/v1/nothing-here"), 400, "invalid_request_url")
    refusal(await api.put(f"/v1/pages/{ids('handbook')}"), 400, "invalid_request")


async def test_a_body_that_is_not_json_is_refused_invalid_json(api: httpx.AsyncClient) -> None:
    refusal(await api.post("/v1/search", content=b"{not json"), 400, "invalid_json")


async def test_an_id_that_is_not_a_uuid_is_refused(api: httpx.AsyncClient) -> None:
    refusal(await api.get("/v1/pages/not-an-id"), 400, "validation_error")


async def test_an_id_without_dashes_is_the_same_object(api: httpx.AsyncClient) -> None:
    found = answer(await api.get(f"/v1/pages/{ids('handbook').replace('-', '')}"))
    assert found["id"] == ids("handbook")


# --------------------------------------------------------------------------- capabilities


async def test_an_integration_without_a_capability_is_refused_restricted_resource(world: World) -> None:
    async with direct(world, token=READER_TOKEN) as reader:
        assert answer(await reader.get(f"/v1/pages/{ids('handbook')}"))["id"] == ids("handbook")
        refusal(
            await reader.patch(f"/v1/blocks/{ids('handbook')}/children", json={"children": [paragraph("x")]}),
            403,
            "restricted_resource",
        )
        refusal(await reader.get("/v1/users"), 403, "restricted_resource")
        refusal(await reader.get("/v1/comments", params={"block_id": ids("handbook")}), 403, "restricted_resource")


# --------------------------------------------------------------------------- validation


async def test_rich_text_over_two_thousand_characters_is_refused(api: httpx.AsyncClient) -> None:
    message = refusal(
        await api.patch(f"/v1/blocks/{ids('handbook')}/children", json={"children": [paragraph("x" * 2001)]}),
        400,
        "validation_error",
    )
    assert "text.content" in message


async def test_more_than_a_hundred_children_is_refused(api: httpx.AsyncClient) -> None:
    children = [paragraph(str(n)) for n in range(101)]
    refusal(
        await api.patch(f"/v1/blocks/{ids('handbook')}/children", json={"children": children}), 400, "validation_error"
    )


async def test_children_nested_three_deep_in_one_request_are_refused(api: httpx.AsyncClient) -> None:
    deepest = paragraph("3")
    level2 = {"type": "toggle", "toggle": {"rich_text": [], "children": [deepest]}}
    level1 = {"type": "toggle", "toggle": {"rich_text": [], "children": [level2]}}
    top = {"type": "toggle", "toggle": {"rich_text": [], "children": [level1]}}
    refusal(
        await api.patch(f"/v1/blocks/{ids('handbook')}/children", json={"children": [top]}), 400, "validation_error"
    )


async def test_appending_after_a_block_that_is_not_a_child_is_refused(api: httpx.AsyncClient) -> None:
    toggle = await first_block(api, 5)
    inside = answer(await api.get(f"/v1/blocks/{toggle['id']}/children"))["results"][0]
    refusal(
        await api.patch(
            f"/v1/blocks/{ids('handbook')}/children", json={"children": [paragraph("x")], "after": inside["id"]}
        ),
        400,
        "validation_error",
    )


async def test_changing_a_blocks_type_is_refused(api: httpx.AsyncClient) -> None:
    heading = await first_block(api)
    refusal(await api.patch(f"/v1/blocks/{heading['id']}", json=paragraph("now a paragraph")), 400, "validation_error")


async def test_a_block_type_this_fake_does_not_build_is_refused_saying_so(api: httpx.AsyncClient) -> None:
    message = refusal(
        await api.patch(
            f"/v1/blocks/{ids('handbook')}/children",
            json={"children": [{"type": "column_list", "column_list": {"children": []}}]},
        ),
        400,
        "validation_error",
    )
    assert "does not build" in message


async def test_a_property_the_database_does_not_have_is_refused(api: httpx.AsyncClient) -> None:
    message = refusal(
        await api.patch(f"/v1/pages/{ids('launch')}", json={"properties": {"Colour": {"rich_text": []}}}),
        400,
        "validation_error",
    )
    assert "Colour" in message


async def test_a_value_of_the_wrong_type_for_its_property_is_refused(api: httpx.AsyncClient) -> None:
    refusal(
        await api.patch(f"/v1/pages/{ids('launch')}", json={"properties": {"Estimate": {"rich_text": []}}}),
        400,
        "validation_error",
    )
    refusal(
        await api.patch(f"/v1/pages/{ids('launch')}", json={"properties": {"Estimate": {"number": "five"}}}),
        400,
        "validation_error",
    )


async def test_a_status_option_the_property_does_not_have_is_refused(api: httpx.AsyncClient) -> None:
    refusal(
        await api.patch(f"/v1/pages/{ids('launch')}", json={"properties": {"Status": {"status": {"name": "Blocked"}}}}),
        400,
        "validation_error",
    )


async def test_a_write_to_a_read_only_property_is_refused(api: httpx.AsyncClient) -> None:
    refusal(
        await api.patch(
            f"/v1/pages/{ids('launch')}", json={"properties": {"Created": {"created_time": "2026-01-01T00:00:00Z"}}}
        ),
        400,
        "validation_error",
    )


QUERY_REFUSALS = [
    ("an unknown condition", {"property": "Name", "title": {"resembles": "x"}}),
    ("a type key not the property's", {"property": "Estimate", "rich_text": {"equals": "5"}}),
    ("an unknown property", {"property": "Colour", "select": {"equals": "red"}}),
    ("is_empty not true", {"property": "Notes", "rich_text": {"is_empty": False}}),
    ("a number given as text", {"property": "Estimate", "number": {"equals": "5"}}),
    ("a date that is not one", {"property": "Due", "date": {"before": "soon"}}),
    ("two conditions", {"property": "Name", "title": {"equals": "a", "contains": "b"}}),
    ("an unknown timestamp", {"timestamp": "deleted_time", "deleted_time": {"past_week": {}}}),
    (
        "compounds three deep",
        {"and": [{"or": [{"and": [{"property": "Name", "title": {"equals": "a"}}]}]}]},
    ),
]


@pytest.mark.parametrize(("why", "found"), QUERY_REFUSALS, ids=[q[0] for q in QUERY_REFUSALS])
async def test_a_database_query_filter_notion_refuses_is_refused(
    api: httpx.AsyncClient, why: str, found: dict[str, Any]
) -> None:
    refusal(await api.post(f"/v1/databases/{ids('projects')}/query", json={"filter": found}), 400, "validation_error")


async def test_a_sort_on_an_unknown_property_is_refused(api: httpx.AsyncClient) -> None:
    refusal(
        await api.post(
            f"/v1/databases/{ids('projects')}/query", json={"sorts": [{"property": "Colour", "direction": "ascending"}]}
        ),
        400,
        "validation_error",
    )


async def test_a_search_filter_value_other_than_page_or_database_is_refused(api: httpx.AsyncClient) -> None:
    refusal(
        await api.post("/v1/search", json={"filter": {"property": "object", "value": "data_source"}}),
        400,
        "validation_error",
    )


async def test_a_start_cursor_not_in_the_list_is_refused(api: httpx.AsyncClient) -> None:
    refusal(
        await api.get(f"/v1/blocks/{ids('onboarding')}/children", params={"start_cursor": ids("audit")}),
        400,
        "validation_error",
    )


async def test_a_page_size_over_a_hundred_is_refused(api: httpx.AsyncClient) -> None:
    refusal(await api.post("/v1/search", json={"page_size": 101}), 400, "validation_error")


# --------------------------------------------------------------------------- archived


async def test_an_edit_or_append_to_an_archived_page_is_refused(api: httpx.AsyncClient) -> None:
    answer(await api.patch(f"/v1/pages/{ids('audit')}", json={"archived": True}))
    refusal(
        await api.patch(f"/v1/pages/{ids('audit')}", json={"properties": {"Estimate": {"number": 1}}}),
        400,
        "validation_error",
    )
    refusal(
        await api.patch(f"/v1/blocks/{ids('audit')}/children", json={"children": [paragraph("x")]}),
        400,
        "validation_error",
    )


async def test_an_edit_to_an_archived_block_is_refused(api: httpx.AsyncClient) -> None:
    quote = await first_block(api, 7)
    answer(await api.delete(f"/v1/blocks/{quote['id']}"))
    refusal(
        await api.patch(f"/v1/blocks/{quote['id']}", json={"quote": {"rich_text": []}}),
        400,
        "validation_error",
    )


# --------------------------------------------------------------------------- faults


def faulted(tmp_path: Path, faults: list[dict[str, Any]]) -> World:
    return seeded(tmp_path, scenario({**NOTION, "faults": faults}))


async def test_an_armed_rate_limit_is_refused_with_retry_after_then_lifts(tmp_path: Path) -> None:
    world = faulted(tmp_path, [{"kind": "rate_limited", "times": 2, "retry_after": 7, "path": "/v1/search"}])
    async with direct(world) as api:
        for _ in range(2):
            limited = await api.post("/v1/search", json={})
            refusal(limited, 429, "rate_limited")
            assert limited.headers["retry-after"] == "7"
        assert answer(await api.get("/v1/users/me"))["type"] == "bot"
        assert answer(await api.post("/v1/search", json={}))["object"] == "list"


async def test_an_armed_conflict_is_refused_on_a_block_edit_and_then_lifts(tmp_path: Path) -> None:
    world = faulted(tmp_path, [{"kind": "conflict", "times": 1}])
    async with direct(world) as api:
        heading = await first_block(api)
        body = {"heading_1": {"rich_text": [{"text": {"content": "Hi"}}]}}
        refusal(await api.patch(f"/v1/blocks/{heading['id']}", json=body), 409, "conflict_error")
        assert answer(await api.patch(f"/v1/blocks/{heading['id']}", json=body))["id"] == heading["id"]


async def test_a_fault_for_one_integration_is_refused_to_it_alone(tmp_path: Path) -> None:
    world = faulted(tmp_path, [{"kind": "rate_limited", "integration": "reader"}])
    async with direct(world) as agent, direct(world, token=READER_TOKEN) as reader:
        assert answer(await agent.post("/v1/search", json={}))["object"] == "list"
        refusal(await reader.post("/v1/search", json={}), 429, "rate_limited")


# --------------------------------------------------------------------------- OAuth


def basic(client_id: str = CLIENT_ID, secret: str = CLIENT_SECRET) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()}


async def test_a_code_is_exchanged_once_a_second_exchange_is_refused_and_its_token_signs_in_as_the_public_bot(
    world: World,
) -> None:
    async with direct(world, token=None) as bare:
        body = {"grant_type": "authorization_code", "code": CODE, "redirect_uri": REDIRECT}
        granted = answer(await bare.post("/v1/oauth/token", json=body, headers=basic()))
        assert granted["token_type"] == "bearer" and granted["workspace_name"] == "Acme"
        assert granted["owner"]["type"] == "user" and granted["owner"]["user"]["person"]["email"] == "dov@example.com"
        again = await bare.post("/v1/oauth/token", json=body, headers=basic())
        assert answer(again, 400)["error"] == "invalid_grant"
    async with direct(world, token=granted["access_token"]) as connector:
        me = answer(await connector.get("/v1/users/me"))
        assert me["id"] == granted["bot_id"] and me["name"] == "Connector"
        assert me["bot"]["owner"]["user"]["name"] == "Dov Aranha"


async def test_a_refresh_token_mints_a_new_access_token_once_and_a_reuse_is_refused(world: World) -> None:
    async with direct(world, token=None) as bare:
        code = {"grant_type": "authorization_code", "code": CODE, "redirect_uri": REDIRECT}
        granted = answer(await bare.post("/v1/oauth/token", json=code, headers=basic()))
        refresh = {"grant_type": "refresh_token", "refresh_token": granted["refresh_token"]}
        renewed = answer(await bare.post("/v1/oauth/token", json=refresh, headers=basic()))
        assert renewed["access_token"] != granted["access_token"]
        assert (
            answer(await bare.post("/v1/oauth/token", json=refresh, headers=basic()), 400)["error"] == "invalid_grant"
        )
    async with direct(world, token=granted["refresh_token"]) as wrong:
        refusal(await wrong.get("/v1/users/me"), 401, "unauthorized")


async def test_a_token_request_with_the_wrong_client_secret_is_refused(world: World) -> None:
    async with direct(world, token=None) as bare:
        body = {"grant_type": "authorization_code", "code": CODE, "redirect_uri": REDIRECT}
        refused = await bare.post("/v1/oauth/token", json=body, headers=basic(secret="guess"))
        assert answer(refused, 401)["error"] == "invalid_client"


async def test_a_code_with_another_redirect_uri_is_refused(world: World) -> None:
    async with direct(world, token=None) as bare:
        body = {"grant_type": "authorization_code", "code": CODE, "redirect_uri": "https://elsewhere.example.com/cb"}
        assert answer(await bare.post("/v1/oauth/token", json=body, headers=basic()), 400)["error"] == "invalid_grant"


async def test_a_workspace_top_page_from_an_internal_integration_is_refused(api: httpx.AsyncClient) -> None:
    refusal(
        await api.post("/v1/pages", json={"parent": {"workspace": True}, "properties": {"title": []}}),
        400,
        "validation_error",
    )
