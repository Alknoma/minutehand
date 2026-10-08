"""What the fake answers is what Notion answers: content as it was sent, links and messages as Notion's reference gives
them, and what Notion takes but this fake does not build refused by name. Each case pins one behaviour fixed against
the reference or a recording of the real service (`CLAIMS.md` beside the provider)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from tests.providers.notion.notion_world import (
    API,
    NOTION,
    World,
    answer,
    direct,
    ids,
    refusal,
    scenario,
    seeded,
    served,
    unserved,
)


def _children(api: httpx.AsyncClient, page: str) -> Any:
    return api.get(f"/v1/blocks/{page}/children")


async def _page(api: httpx.AsyncClient, title: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    body = {
        "parent": {"page_id": ids("handbook")},
        "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        "children": children,
    }
    return answer(await api.post("/v1/pages", json=body))


async def test_rich_text_and_block_content_come_back_as_they_were_sent(api: httpx.AsyncClient) -> None:
    """https://developers.notion.com/reference/rich-text: what the caller sent, with only what Notion fills in
    (`plain_text`, `href`, the annotations and colour left at their defaults)."""
    run = {
        "type": "text",
        "text": {"content": "Read the guide", "link": {"url": "https://example.com/guide?a=1&b=2"}},
        "annotations": {
            "bold": True,
            "italic": False,
            "strikethrough": True,
            "underline": False,
            "code": True,
            "color": "red_background",
        },
    }
    sent = [
        {"type": "paragraph", "paragraph": {"rich_text": [run], "color": "blue"}},
        {
            "type": "to_do",
            "to_do": {"rich_text": [{"type": "text", "text": {"content": "  spaced  "}}], "checked": True},
        },
        {"type": "code", "code": {"rich_text": [{"text": {"content": "FROM x\n"}}], "language": "docker"}},
    ]
    made = await _page(api, "Round trip", sent)
    blocks = answer(await _children(api, made["id"]))["results"]

    assert blocks[0]["paragraph"] == {
        "rich_text": [{**run, "plain_text": "Read the guide", "href": "https://example.com/guide?a=1&b=2"}],
        "color": "blue",
    }
    assert blocks[1]["to_do"]["rich_text"][0]["text"] == {"content": "  spaced  ", "link": None}
    assert blocks[1]["to_do"]["checked"] is True
    assert (blocks[2]["code"]["language"], blocks[2]["code"]["rich_text"][0]["plain_text"]) == ("docker", "FROM x\n")


async def test_a_page_a_database_and_a_mention_link_to_app_notion_com(api: httpx.AsyncClient) -> None:
    """https://developers.notion.com/reference/versioning: Notion's own links moved to `https://app.notion.com/p/...`
    in June 2026, on every version; /reference/page, /database and /rich-text give the shapes."""
    mention = {"type": "mention", "mention": {"type": "page", "page": {"id": ids("handbook")}}}
    made = await _page(api, "Launch site", [{"type": "paragraph", "paragraph": {"rich_text": [mention]}}])
    database = answer(await api.get(f"/v1/databases/{ids('projects')}"))
    linked = answer(await _children(api, made["id"]))["results"][0]["paragraph"]["rich_text"][0]

    assert made["url"] == "https://app.notion.com/p/Launch-site-" + made["id"].replace("-", "")
    assert database["url"] == "https://app.notion.com/p/" + ids("projects").replace("-", "")
    assert linked["href"] == "https://app.notion.com/p/" + ids("handbook").replace("-", "")


async def test_a_date_mention_reads_as_its_date(api: httpx.AsyncClient) -> None:
    """https://developers.notion.com/reference/rich-text: a date mention of 2022-12-16 reads "2022-12-16"."""
    mention = {"type": "mention", "mention": {"type": "date", "date": {"start": "2022-12-16"}}}
    made = await _page(api, "Dated", [{"type": "paragraph", "paragraph": {"rich_text": [mention]}}])
    found = answer(await _children(api, made["id"]))["results"][0]["paragraph"]["rich_text"][0]
    assert (found["plain_text"], found["mention"]["date"]) == (
        "2022-12-16",
        {"start": "2022-12-16", "end": None, "time_zone": None},
    )


async def test_a_date_mention_with_an_end_or_a_time_is_refused_501_naming_it(api: httpx.AsyncClient) -> None:
    """Notion documents the `plain_text` only of a date alone, so a range or a time is not answered with a guess."""
    for date in ({"start": "2022-12-16", "end": "2022-12-18"}, {"start": "2022-12-16T10:00:00.000Z"}):
        mention = {"type": "mention", "mention": {"type": "date", "date": date}}
        body = {"children": [{"type": "paragraph", "paragraph": {"rich_text": [mention]}}]}
        assert unserved(await api.patch(f"/v1/blocks/{ids('handbook')}/children", json=body)).startswith(
            "a date mention with a time or an end"
        )


async def test_a_mention_notion_takes_and_this_fake_does_not_build_is_refused_501_naming_it(
    api: httpx.AsyncClient,
) -> None:
    mention = {"type": "mention", "mention": {"type": "template_mention", "template_mention": {}}}
    body = {"children": [{"type": "paragraph", "paragraph": {"rich_text": [mention]}}]}
    assert unserved(await api.patch(f"/v1/blocks/{ids('handbook')}/children", json=body)).startswith(
        "a `template_mention` mention"
    )


async def test_an_image_that_is_not_external_is_refused_501_naming_it(api: httpx.AsyncClient) -> None:
    image = {"type": "image", "image": {"type": "file_upload", "file_upload": {"id": ids("handbook")}}}
    answered = await api.patch(f"/v1/blocks/{ids('handbook')}/children", json={"children": [image]})
    assert unserved(answered).startswith("an image that is not `external`")


async def test_a_property_type_notion_takes_and_this_fake_does_not_build_is_refused_501_naming_it(
    api: httpx.AsyncClient,
) -> None:
    body = {
        "parent": {"page_id": ids("handbook")},
        "title": [{"type": "text", "text": {"content": "Sums"}}],
        "properties": {"Name": {"title": {}}, "Total": {"formula": {"expression": "1"}}},
    }
    assert unserved(await api.post("/v1/databases", json=body)).startswith("`formula` properties")


async def test_a_missing_version_carries_notions_documented_message(world: World) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=served(world)), base_url=API) as unversioned:
        message = refusal(await unversioned.get("/v1/users/me"), 400, "missing_version")
    assert message == (
        "Notion-Version header failed validation: Notion-Version header should be defined, instead was undefined."
    )


async def test_an_object_out_of_reach_is_refused_with_notions_documented_message(api: httpx.AsyncClient) -> None:
    """https://developers.notion.com/reference/status-codes: "Could not find database with ID: ... Make sure the
    relevant pages and databases are shared with your connection "My Connection".\""""
    message = refusal(await api.get(f"/v1/pages/{ids('private')}"), 404, "object_not_found")
    assert message == (
        f"Could not find page with ID: {ids('private')}. Make sure the relevant pages and databases are shared with "
        'your connection "Planning bot".'
    )


async def test_a_rate_limit_and_a_conflict_carry_notions_documented_messages(tmp_path: Path) -> None:
    world = seeded(
        tmp_path,
        scenario({**NOTION, "faults": [{"kind": "rate_limited", "times": 1}, {"kind": "conflict", "times": 1}]}),
    )
    async with direct(world) as api:
        limited = refusal(await api.post("/v1/search", json={}), 429, "rate_limited")
        heading = answer(await _children(api, ids("handbook")))["results"][0]
        body = {"heading_1": {"rich_text": [{"text": {"content": "Hi"}}]}}
        conflicted = refusal(await api.patch(f"/v1/blocks/{heading['id']}", json=body), 409, "conflict_error")
    assert limited == "You have been rate limited. Please try again later."
    assert conflicted == "Conflict occurred while saving. Please try again."
