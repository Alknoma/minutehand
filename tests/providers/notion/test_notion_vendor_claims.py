"""What earlier Notion stand-ins were found to need, each a fact about the real API, held by this fake.

Every case goes through the real proxy over TLS at api.notion.com: the official async SDK where the claim is about a
call, a bare httpx client where the claim is about the headers the SDK always sends. Provenance, class and source
for each are in `src/minutehand/adapters/providers/notion/CLAIMS.md`.
"""

from __future__ import annotations

import ssl
from typing import Any

import httpx
import pytest
from notion_client import APIResponseError

from tests.providers.notion.notion_world import AGENT_TOKEN, API, VERSION, Sdk, ids


def _para(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def _title(text: str) -> dict[str, Any]:
    return {"title": {"title": [{"type": "text", "text": {"content": text}}]}}


def _plain(block: dict[str, Any]) -> str:
    kind = block["type"]
    return "".join(run["plain_text"] for run in block[kind]["rich_text"]) if "rich_text" in block[kind] else kind


async def _refused(sdk: Sdk, call: Any) -> APIResponseError:
    with pytest.raises(APIResponseError) as caught:
        await sdk(call)
    return caught.value


def _bare(sdk: Sdk, headers: dict[str, str]) -> httpx.AsyncClient:
    trust = ssl.create_default_context(cafile=str(sdk.proxy.ca_cert))
    return httpx.AsyncClient(proxy=sdk.proxy.url, verify=trust, trust_env=False, base_url=API, headers=headers)


async def _page(sdk: Sdk, title: str, children: list[dict[str, Any]]) -> str:
    made = await sdk(
        lambda c: c.pages.create(parent={"page_id": ids("handbook")}, properties=_title(title), children=children)
    )
    return str(made["id"])


async def test_a_call_with_no_bearer_token_is_answered_as_the_agents_integration(async_sdk: Sdk) -> None:
    """Minutehand does not enforce credentials (CLAIMS.md): Notion answers a call with no bearer token 401
    `unauthorized` (`tests/data/notion_api/real-service-without-a-token-2026-10-08.txt`); this fake answers it as the
    first integration the seed declares."""
    async with _bare(async_sdk, VERSION) as anonymous:
        response = await anonymous.post("/v1/search", json={})

    assert response.status_code == 200, response.text
    assert response.json()["object"] == "list"


async def test_a_call_with_no_notion_version_header_is_refused_missing_version(async_sdk: Sdk) -> None:
    """Documented: https://developers.notion.com/reference/status-codes lists 400 `missing_version` for a request
    that names no API version."""
    async with _bare(async_sdk, {"Authorization": f"Bearer {AGENT_TOKEN}"}) as unversioned:
        response = await unversioned.post("/v1/search", json={})

    assert response.status_code == 400
    assert response.json()["code"] == "missing_version"


async def test_a_page_created_with_a_hundred_and_one_children_is_refused_validation_error(async_sdk: Sdk) -> None:
    """Documented: https://developers.notion.com/reference/post-page caps `children` at 100 items, and
    https://developers.notion.com/reference/request-limits answers a breach with 400 `validation_error`."""
    head = async_sdk.store.head()

    refusal = await _refused(
        async_sdk,
        lambda c: c.pages.create(
            parent={"page_id": ids("handbook")},
            properties=_title("Packing list"),
            children=[_para(f"crate {n}") for n in range(101)],
        ),
    )

    assert (refusal.status, refusal.code) == (400, "validation_error")
    assert "children" in str(refusal)
    assert async_sdk.store.head() == head


async def test_a_page_created_with_exactly_a_hundred_children_keeps_them_all(async_sdk: Sdk) -> None:
    """Documented: https://developers.notion.com/reference/post-page allows up to 100 children, so 100 is the
    boundary the refusal above must not cross."""
    page = await _page(async_sdk, "Hundred crates", [_para(f"crate {n}") for n in range(100)])

    listed = await async_sdk(lambda c: c.blocks.children.list(page))

    assert len(listed["results"]) == 100 and listed["has_more"] is False


async def test_a_text_run_over_two_thousand_characters_is_refused_validation_error(async_sdk: Sdk) -> None:
    """Documented: https://developers.notion.com/reference/request-limits caps `text.content` at 2000 characters
    and answers a breach with 400 `validation_error`."""
    refusal = await _refused(
        async_sdk,
        lambda c: c.pages.create(
            parent={"page_id": ids("handbook")}, properties=_title("Long note"), children=[_para("w" * 2001)]
        ),
    )

    assert (refusal.status, refusal.code) == (400, "validation_error")
    assert "text.content" in str(refusal)


async def test_a_text_run_of_exactly_two_thousand_characters_is_kept_whole(async_sdk: Sdk) -> None:
    """Documented: https://developers.notion.com/reference/request-limits sets the cap at 2000, inclusive."""
    page = await _page(async_sdk, "Exact note", [_para("w" * 2000)])

    (block,) = (await async_sdk(lambda c: c.blocks.children.list(page)))["results"]

    assert _plain(block) == "w" * 2000


async def test_a_toggle_created_with_children_reports_and_lists_them(async_sdk: Sdk) -> None:
    """Documented: https://developers.notion.com/reference/block says `has_children` tells whether a block has
    blocks nested under it, which are read by listing that block's own children."""
    page = await _page(
        async_sdk,
        "Folded",
        [{"type": "toggle", "toggle": {"rich_text": [{"type": "text", "text": {"content": "fold"}}], "children": [_para("tucked")]}}],
    )  # fmt: skip

    (toggle,) = (await async_sdk(lambda c: c.blocks.children.list(page)))["results"]
    inside = (await async_sdk(lambda c: c.blocks.children.list(toggle["id"])))["results"]

    assert toggle["has_children"] is True
    assert [_plain(b) for b in inside] == ["tucked"]


async def test_an_append_after_a_grandchild_is_refused_validation_error(async_sdk: Sdk) -> None:
    """Observed: the `after` block must be a direct child of the block being appended to. The endpoint's page
    (https://developers.notion.com/reference/patch-block-children) says only that new blocks go after the named
    one; a nested block as the anchor was seen refused, not silently appended at the end."""
    page = await _page(
        async_sdk,
        "Anchors",
        [_para("opening"), {"type": "toggle", "toggle": {"rich_text": [], "children": [_para("buried")]}}],
    )
    toggle = (await async_sdk(lambda c: c.blocks.children.list(page)))["results"][1]
    buried = (await async_sdk(lambda c: c.blocks.children.list(toggle["id"])))["results"][0]
    head = async_sdk.store.head()

    refusal = await _refused(
        async_sdk, lambda c: c.blocks.children.append(page, children=[_para("stray")], after=buried["id"])
    )

    assert refusal.status == 400
    assert "after" in str(refusal)
    assert async_sdk.store.head() == head


async def test_an_append_after_a_direct_child_lands_right_behind_it(async_sdk: Sdk) -> None:
    """Documented: https://developers.notion.com/reference/patch-block-children inserts the new blocks after the
    block `after` names, not at the end of the list."""
    page = await _page(async_sdk, "Running order", [_para("one"), _para("three"), _para("four")])
    one = (await async_sdk(lambda c: c.blocks.children.list(page)))["results"][0]

    await async_sdk(lambda c: c.blocks.children.append(page, children=[_para("two")], after=one["id"]))
    listed = (await async_sdk(lambda c: c.blocks.children.list(page)))["results"]

    assert [_plain(b) for b in listed] == ["one", "two", "three", "four"]


async def test_an_update_that_names_another_block_type_is_refused(async_sdk: Sdk) -> None:
    """Observed: an update to a paragraph that carries a `heading_2` body is refused, not turned into a heading.
    https://developers.notion.com/reference/update-a-block answers 400 when the type in the body is wrong for the
    block, without saying in so many words that the type is fixed."""
    page = await _page(async_sdk, "Typed", [_para("plain words")])
    (block,) = (await async_sdk(lambda c: c.blocks.children.list(page)))["results"]

    refusal = await _refused(
        async_sdk,
        lambda c: c.blocks.update(block["id"], heading_2={"rich_text": [{"type": "text", "text": {"content": "Big"}}]}),
    )
    (after,) = (await async_sdk(lambda c: c.blocks.children.list(page)))["results"]

    assert (refusal.status, refusal.code) == (400, "validation_error")
    assert (after["type"], _plain(after)) == ("paragraph", "plain words")


async def test_a_children_listing_stops_at_a_hundred_and_hands_a_cursor_for_the_rest(async_sdk: Sdk) -> None:
    """Documented: https://developers.notion.com/reference/intro pages every list at 100 by default, setting
    `has_more` and a `next_cursor` that the next request passes as `start_cursor`."""
    page = await _page(async_sdk, "Long ledger", [_para(f"entry {n}") for n in range(100)])
    await async_sdk(lambda c: c.blocks.children.append(page, children=[_para(f"entry {n}") for n in range(100, 140)]))

    first = await async_sdk(lambda c: c.blocks.children.list(page))
    rest = await async_sdk(lambda c: c.blocks.children.list(page, start_cursor=first["next_cursor"]))

    assert (len(first["results"]), first["has_more"]) == (100, True)
    assert (len(rest["results"]), rest["has_more"], rest["next_cursor"]) == (40, False, None)
    assert _plain(rest["results"][-1]) == "entry 139"
