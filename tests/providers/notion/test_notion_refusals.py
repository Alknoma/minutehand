"""What Notion refuses, with its status, its code and its error object, driven over the app directly."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest
from notion_client import APIResponseError

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
    served,
    through_proxy,
    unserved,
)


def paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


async def first_block(api: httpx.AsyncClient, index: int = 0) -> dict[str, Any]:
    return answer(await api.get(f"/v1/blocks/{ids('handbook')}/children"))["results"][index]


# --------------------------------------------------------------------------- sign-in and version


async def test_a_call_with_no_token_or_an_unknown_one_is_made_as_the_agents_integration(world: World) -> None:
    """Minutehand does not enforce credentials: Notion answers both 401 `unauthorized`
    (`tests/data/notion_api/real-service-without-a-token-2026-10-08.txt`); this fake answers as the first integration
    the seed declares."""
    async with direct(world) as agent:
        mine = answer(await agent.get("/v1/users/me"))["id"]
    for token in (None, "ntn_nobody_seeded_this", ""):
        async with direct(world, token=token) as stranger:
            assert answer(await stranger.get("/v1/users/me"))["id"] == mine
            assert answer(await stranger.post("/v1/search", json={}))["object"] == "list"


async def test_a_seeded_token_still_names_its_own_integration(world: World) -> None:
    async with direct(world) as agent, direct(world, token=READER_TOKEN) as reader:
        assert answer(await reader.get("/v1/users/me"))["id"] != answer(await agent.get("/v1/users/me"))["id"]


async def test_a_call_without_a_version_is_refused_missing_version(world: World) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=served(world)),
        base_url=API,
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
    ) as unversioned:
        refusal(await unversioned.get("/v1/users/me"), 400, "missing_version")


async def test_a_version_this_fake_does_not_serve_is_refused_501_naming_it(api: httpx.AsyncClient) -> None:
    for version in ("2025-09-03", "2026-03-11", "2021-08-16"):
        answered = await api.get("/v1/users/me", headers={"Notion-Version": version})
        assert unserved(answered) == f"Notion-Version {version} (this simulation answers 2022-06-28)"


async def test_an_unknown_path_and_a_wrong_method_are_refused_invalid_request_url(api: httpx.AsyncClient) -> None:
    """OBSERVED: Notion answers both 400 `invalid_request_url` "Invalid request URL."
    (`tests/data/notion_api/real-service-without-a-token-2026-10-08.txt`)."""
    assert refusal(await api.get("/v1/nothing-here"), 400, "invalid_request_url") == "Invalid request URL."
    assert refusal(await api.put(f"/v1/pages/{ids('handbook')}"), 400, "invalid_request_url") == "Invalid request URL."


async def test_a_body_that_is_not_json_is_refused_invalid_json(api: httpx.AsyncClient) -> None:
    """OBSERVED and DOCUMENTED (https://developers.notion.com/reference/status-codes): "Error parsing JSON body."."""
    assert (
        refusal(await api.post("/v1/search", content=b"{not json"), 400, "invalid_json") == "Error parsing JSON body."
    )


async def test_an_id_without_dashes_is_the_same_object(api: httpx.AsyncClient) -> None:
    found = answer(await api.get(f"/v1/pages/{ids('handbook').replace('-', '')}"))
    assert found["id"] == ids("handbook")


# --------------------------------------------------------------------------- capabilities


async def test_an_integration_missing_a_capability_is_not_refused(world: World) -> None:
    """Capabilities are the integration's scope, and Minutehand does not enforce credentials or scopes: the reader,
    seeded with `read_content` alone, appends, lists users and reads comments."""
    async with direct(world, token=READER_TOKEN) as reader:
        appended = await reader.patch(f"/v1/blocks/{ids('handbook')}/children", json={"children": [paragraph("x")]})
        assert answer(appended)["object"] == "list"
        assert answer(await reader.get("/v1/users"))["object"] == "list"
        assert answer(await reader.get("/v1/comments", params={"block_id": ids("handbook")}))["object"] == "list"


# --------------------------------------------------------------------------- validation
#
# A refusal Notion's words are reported for is answered in them, each test citing where; one no page, recording or
# report gives words for is the shared not-served refusal, naming the case.


def append(api: httpx.AsyncClient, body: dict[str, Any]) -> Any:
    return api.patch(f"/v1/blocks/{ids('handbook')}/children", json=body)


async def test_rich_text_over_two_thousand_characters_is_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    """https://github.com/trustmaster/gkeep2notion/issues/15"""
    message = refusal(await append(api, {"children": [paragraph("x" * 2001)]}), 400, "validation_error")
    assert message == (
        "body failed validation: body.children[0].paragraph.rich_text[0].text.content.length should be ≤ `2000`, "
        "instead was `2001`."
    )


async def test_more_than_a_hundred_children_is_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    """https://github.com/Suntory-N-Water/kindle-highlight-syncer/issues/2"""
    children = [paragraph(str(n)) for n in range(101)]
    message = refusal(await append(api, {"children": children}), 400, "validation_error")
    assert message == "body failed validation: body.children.length should be ≤ `100`, instead was `101`."


async def test_children_nested_three_deep_in_one_request_are_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    """https://github.com/tryfabric/martian/issues/15: the third level's children "should be not present"."""
    deepest = paragraph("3")
    level2 = {"type": "toggle", "toggle": {"rich_text": [], "children": [deepest]}}
    level1 = {"type": "toggle", "toggle": {"rich_text": [], "children": [level2]}}
    top = {"type": "toggle", "toggle": {"rich_text": [], "children": [level1]}}
    message = refusal(await append(api, {"children": [top]}), 400, "validation_error")
    assert message.startswith(
        "body failed validation: body.children[0].toggle.children[0].toggle.children[0].toggle.children should be not "
        'present, instead was `[{"type":"paragraph"'
    )


async def test_a_missing_field_and_a_stray_one_are_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    """https://github.com/selfboot/html2notion/issues/17 and https://github.com/eval-sys/mcpmark/issues/269"""
    missing = refusal(await append(api, {}), 400, "validation_error")
    stray = refusal(await append(api, {"children": [paragraph("x")], "colour": "red"}), 400, "validation_error")
    wrong = refusal(await append(api, {"children": "x"}), 400, "validation_error")
    assert missing == "body failed validation: body.children should be defined, instead was `undefined`."
    assert stray == 'body failed validation: body.colour should be not present, instead was `"red"`.'
    assert wrong == 'body failed validation: body.children should be an array, instead was `"x"`.'


async def test_an_id_that_is_not_a_uuid_is_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    """https://github.com/zant/notion-cards-action/issues/21"""
    message = refusal(await api.get("/v1/pages/not-an-id"), 400, "validation_error")
    assert message == 'path failed validation: path.page_id should be a valid uuid, instead was `"not-an-id"`.'


async def test_appending_after_a_block_that_is_not_a_child_is_refused_by_name(
    world: World, api: httpx.AsyncClient
) -> None:
    toggle = await first_block(api, 5)
    inside = answer(await api.get(f"/v1/blocks/{toggle['id']}/children"))["results"][0]
    head = world.store.head()
    answered = await append(api, {"children": [paragraph("x")], "after": inside["id"]})
    assert unserved(answered).startswith("an `after` that names no child")
    assert world.store.head() == head


async def test_changing_a_blocks_type_is_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    """https://community.zapier.com/troubleshooting-99/error-expected-block-type-divider-in-request-body-17502"""
    heading = await first_block(api)
    message = refusal(await api.patch(f"/v1/blocks/{heading['id']}", json=paragraph("now")), 400, "validation_error")
    assert message == "Expected block type heading_1 in request body"


async def test_a_block_type_this_fake_does_not_build_is_refused_501_naming_it(api: httpx.AsyncClient) -> None:
    """Notion takes a column list; this fake does not build one, and says so with the shared not-served refusal rather
    than a 400 that would read as Notion's own."""
    answered = await append(api, {"children": [{"type": "column_list", "column_list": {"children": []}}]})
    assert unserved(answered) == "`column_list` blocks (body.children[0])"


async def test_a_property_the_database_does_not_have_is_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    """https://github.com/bil0u/remarkable2-to-notion/issues/2"""
    message = refusal(
        await api.patch(f"/v1/pages/{ids('launch')}", json={"properties": {"Colour": {"rich_text": []}}}),
        400,
        "validation_error",
    )
    assert message == "Colour is not a property that exists."


async def test_a_value_of_the_wrong_type_for_its_property_is_refused_by_name(api: httpx.AsyncClient) -> None:
    other = await api.patch(f"/v1/pages/{ids('launch')}", json={"properties": {"Estimate": {"rich_text": []}}})
    text = await api.patch(f"/v1/pages/{ids('launch')}", json={"properties": {"Estimate": {"number": "five"}}})
    assert "given another type's value" in unserved(other)
    assert "that is not a number" in unserved(text)


async def test_a_status_option_the_property_does_not_have_is_refused_by_name(api: httpx.AsyncClient) -> None:
    body = {"properties": {"Status": {"status": {"name": "Blocked"}}}}
    assert "no option of the status property" in unserved(await api.patch(f"/v1/pages/{ids('launch')}", json=body))


async def test_a_select_option_with_a_comma_is_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    """https://community.make.com/t/how-to-add-notion-select-value-containing-commas/18286"""
    body = {"properties": {"Priority": {"select": {"name": "High, really"}}}}
    message = refusal(await api.patch(f"/v1/pages/{ids('launch')}", json=body), 400, "validation_error")
    assert message == "Invalid select option, commas not allowed: High, really"


async def test_a_write_to_a_read_only_property_is_refused_by_name(api: httpx.AsyncClient) -> None:
    body = {"properties": {"Created": {"created_time": "2026-01-01T00:00:00Z"}}}
    assert "read-only" in unserved(await api.patch(f"/v1/pages/{ids('launch')}", json=body))


QUERY_REFUSALS = [
    ("an unknown condition", {"property": "Name", "title": {"resembles": "x"}}),
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
async def test_a_database_query_filter_notion_gives_no_words_for_is_refused_by_name(
    api: httpx.AsyncClient, why: str, found: dict[str, Any]
) -> None:
    del why
    assert unserved(await api.post(f"/v1/databases/{ids('projects')}/query", json={"filter": found}))


async def test_a_filter_on_an_unknown_property_or_of_another_type_is_refused_in_notions_words(
    api: httpx.AsyncClient,
) -> None:
    """https://github.com/ikisuke/wagumi-sbt/issues/13 and
    https://community.n8n.io/t/need-help-troubleshooting-an-issue-with-notion-node/10730"""
    query = f"/v1/databases/{ids('projects')}/query"
    unknown = await api.post(query, json={"filter": {"property": "Colour", "select": {"equals": "red"}}})
    typed = await api.post(query, json={"filter": {"property": "Estimate", "rich_text": {"equals": "5"}}})
    assert refusal(unknown, 400, "validation_error") == "Could not find property with name or id: Colour"
    assert refusal(typed, 400, "validation_error") == (
        "The property type in the database does not match the property type of the filter provided: "
        "database property number does not match filter rich_text"
    )


async def test_a_sort_on_an_unknown_property_is_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    """https://github.com/azu/bluenotiondb/issues/14"""
    sorts = [{"property": "Colour", "direction": "ascending"}]
    answered = await api.post(f"/v1/databases/{ids('projects')}/query", json={"sorts": sorts})
    assert refusal(answered, 400, "validation_error") == "Could not find sort property with name or id: Colour"


async def test_a_search_filter_or_sort_notion_refuses_is_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    """https://github.com/brekkylab/backlot/issues/375, which recorded these of the real service."""
    value = await api.post("/v1/search", json={"filter": {"property": "object", "value": "data_source"}})
    timestamp = await api.post("/v1/search", json={"sort": {"timestamp": "created_time", "direction": "ascending"}})
    direction = await api.post("/v1/search", json={"sort": {"timestamp": "last_edited_time", "direction": "up"}})
    assert refusal(value, 400, "validation_error") == (
        'body failed validation: body.filter.value should be `"page"` or `"database"`, instead was `"data_source"`.'
    )
    assert refusal(timestamp, 400, "validation_error") == (
        'body.sort.timestamp should be "last_edited_time" when sorting by timestamp.'
    )
    assert refusal(direction, 400, "validation_error") == (
        'body failed validation: body.sort.direction should be `"ascending"`, `"descending"`, or `undefined`, '
        'instead was `"up"`.'
    )


async def test_a_start_cursor_notion_did_not_issue_is_refused_as_reported(api: httpx.AsyncClient) -> None:
    """https://github.com/brekkylab/backlot/issues/375: a list of children refuses a cursor that is not a uuid as a
    validation failure, a list of users "The start_cursor provided is invalid"; what children answer to a uuid they
    did not issue is not documented."""
    bogus = await api.get(f"/v1/blocks/{ids('onboarding')}/children", params={"start_cursor": "bogus"})
    users = await api.get("/v1/users", params={"start_cursor": "bogus"})
    stranger = await api.get(f"/v1/blocks/{ids('onboarding')}/children", params={"start_cursor": ids("audit")})
    assert refusal(bogus, 400, "validation_error") == (
        'query failed validation: query.start_cursor should be a valid uuid or `undefined`, instead was `"bogus"`.'
    )
    assert refusal(users, 400, "validation_error") == "The start_cursor provided is invalid: bogus"
    assert "did not issue" in unserved(stranger)


async def test_a_page_size_is_read_as_notion_is_reported_to_read_it(api: httpx.AsyncClient) -> None:
    """https://github.com/brekkylab/backlot/issues/375: 0 is the default page; comments refuse 0 and 101; a size
    that is not a number is a validation failure; what other lists answer to 101 is not documented."""
    zero = answer(await api.post("/v1/search", json={"page_size": 0}))
    comments = await api.get("/v1/comments", params={"block_id": ids("handbook"), "page_size": "101"})
    text = await api.post("/v1/search", json={"page_size": "ten"})
    over = await api.post("/v1/search", json={"page_size": 101})
    assert zero["object"] == "list"
    assert refusal(comments, 400, "validation_error") == (
        "query failed validation: query.page_size should be ≤ `100` or `undefined`, instead was `101`."
    )
    assert refusal(text, 400, "validation_error") == (
        'body failed validation: body.page_size should be a number or `undefined`, instead was `"ten"`.'
    )
    assert unserved(over) == "body.page_size of 101 (Notion's answer to it is not documented)"


# --------------------------------------------------------------------------- archived

ARCHIVED = "Can't edit block that is archived. You must unarchive the block before editing."
"""https://github.com/parkminhyun0/bible-mindmap/issues/322"""


async def test_an_edit_or_append_to_an_archived_page_is_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    answer(await api.patch(f"/v1/pages/{ids('audit')}", json={"archived": True}))
    edit = await api.patch(f"/v1/pages/{ids('audit')}", json={"properties": {"Estimate": {"number": 1}}})
    appended = await api.patch(f"/v1/blocks/{ids('audit')}/children", json={"children": [paragraph("x")]})
    assert refusal(edit, 400, "validation_error") == ARCHIVED
    assert refusal(appended, 400, "validation_error") == ARCHIVED


async def test_an_edit_to_an_archived_block_is_refused_in_notions_words(api: httpx.AsyncClient) -> None:
    quote = await first_block(api, 7)
    answer(await api.delete(f"/v1/blocks/{quote['id']}"))
    edited = await api.patch(f"/v1/blocks/{quote['id']}", json={"quote": {"rich_text": []}})
    assert refusal(edited, 400, "validation_error") == ARCHIVED


async def test_a_workspace_top_page_from_an_internal_integration_is_refused_by_name(api: httpx.AsyncClient) -> None:
    answered = await api.post("/v1/pages", json={"parent": {"workspace": True}, "properties": {"title": []}})
    assert "made by an internal integration" in unserved(answered)


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


async def test_a_code_is_exchanged_and_its_token_signs_in_as_the_public_bot(world: World) -> None:
    async with direct(world, token=None) as bare:
        body = {"grant_type": "authorization_code", "code": CODE, "redirect_uri": REDIRECT}
        granted = answer(await bare.post("/v1/oauth/token", json=body, headers=basic()))
        assert granted["token_type"] == "bearer" and granted["workspace_name"] == "Acme"
        assert granted["owner"]["type"] == "user" and granted["owner"]["user"]["person"]["email"] == "dov@example.com"
    async with direct(world, token=granted["access_token"]) as connector:
        me = answer(await connector.get("/v1/users/me"))
        assert me["id"] == granted["bot_id"] and me["name"] == "Connector"
        assert me["bot"]["owner"]["user"]["name"] == "Dov Aranha"


async def test_no_code_client_secret_redirect_or_refresh_token_is_refused(world: World) -> None:
    """Minutehand does not enforce credentials: a code used twice, a wrong client secret, another redirect URI, a
    refresh token used twice or nobody's each buys tokens for the public integration the client id names."""
    async with direct(world, token=None) as bare:
        code = {"grant_type": "authorization_code", "code": CODE, "redirect_uri": REDIRECT}
        first = answer(await bare.post("/v1/oauth/token", json=code, headers=basic()))
        tries = [
            (code, basic()),
            (code, basic(secret="guess")),
            ({**code, "redirect_uri": "https://elsewhere.example.com/cb"}, basic()),
            ({"grant_type": "refresh_token", "refresh_token": first["refresh_token"]}, basic()),
            ({"grant_type": "refresh_token", "refresh_token": first["refresh_token"]}, basic()),
            ({"grant_type": "refresh_token", "refresh_token": "nrt_nobody_minted_this"}, basic()),
        ]
        for body, headers in tries:
            granted = answer(await bare.post("/v1/oauth/token", json=body, headers=headers))
            assert granted["bot_id"] == first["bot_id"], body


async def test_a_grant_type_notion_does_not_take_is_refused_unsupported_grant_type(world: World) -> None:
    async with direct(world, token=None) as bare:
        refused = await bare.post("/v1/oauth/token", json={"grant_type": "password"}, headers=basic())
        assert answer(refused, 400)["error"] == "unsupported_grant_type"


async def test_a_rate_limit_is_refused_to_the_sdk_without_a_retry_and_keeps_retry_after(tmp_path: Path) -> None:
    """notion-client 2.2.1 has no retry: a 429 reaches the caller as `APIResponseError`, Retry-After on it."""
    world = faulted(tmp_path, [{"kind": "rate_limited", "times": 1, "retry_after": 30}])
    async for sdk in through_proxy(tmp_path, world, "async"):
        with pytest.raises(APIResponseError) as raised:
            await sdk(lambda c: c.users.me())
        assert raised.value.code == "rate_limited" and raised.value.headers["retry-after"] == "30"
        assert (await sdk(lambda c: c.users.me()))["type"] == "bot"
    world.store.close()
