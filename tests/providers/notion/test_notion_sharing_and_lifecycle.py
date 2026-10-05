"""Sharing is the permission model, and a whole agent conversation run with ids discovered through the API.

Both go through the real proxy with the official SDK."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from notion_client import APIResponseError

from minutehand.domain.world import Actor, Operation, RecordSnapshot
from tests.providers.notion.notion_world import OTHER_TOKEN, START, Sdk, World, ids


async def _refused_not_found(sdk: Sdk, call: Any, token: str | None = None) -> None:
    with pytest.raises(APIResponseError) as raised:
        await (sdk(call) if token is None else sdk.as_token(token, call))
    assert raised.value.code == "object_not_found" and raised.value.status == 404


async def test_what_is_not_shared_with_an_integration_is_invisible_to_it(sdk: Sdk) -> None:
    everything = await sdk(lambda c: c.search(page_size=100))
    titles = {r["id"] for r in everything["results"]}
    assert ids("private") not in titles and ids("private_child") not in titles
    assert {ids("handbook"), ids("onboarding"), ids("projects"), ids("launch")} <= titles
    named = await sdk(lambda c: c.search(query="Salary"))
    assert named["results"] == []
    await _refused_not_found(sdk, lambda c: c.pages.retrieve(ids("private")))
    await _refused_not_found(sdk, lambda c: c.pages.retrieve(ids("private_child")))
    await _refused_not_found(sdk, lambda c: c.blocks.children.list(ids("private")))
    await _refused_not_found(sdk, lambda c: c.blocks.retrieve(ids("private")))

    theirs = await sdk.as_token(OTHER_TOKEN, lambda c: c.blocks.children.list(ids("private")))
    block = theirs["results"][0]
    assert block["paragraph"]["rich_text"][0]["plain_text"] == "Confidential"
    await _refused_not_found(sdk, lambda c: c.blocks.retrieve(block["id"]))
    await _refused_not_found(sdk, lambda c: c.blocks.update(block["id"], paragraph={"rich_text": []}))
    child = await sdk.as_token(OTHER_TOKEN, lambda c: c.pages.retrieve(ids("private_child")))
    assert child["parent"] == {"type": "page_id", "page_id": ids("private")}

    seen_by_other = await sdk.as_token(OTHER_TOKEN, lambda c: c.search(page_size=100))
    assert {r["id"] for r in seen_by_other["results"]} == {ids("private"), ids("private_child")}
    await _refused_not_found(sdk, lambda c: c.pages.retrieve(ids("handbook")), token=OTHER_TOKEN)
    await _refused_not_found(sdk, lambda c: c.databases.query(ids("projects")), token=OTHER_TOKEN)
    await _refused_not_found(
        sdk,
        lambda c: c.pages.create(parent={"page_id": ids("handbook")}, properties={"title": []}),
        token=OTHER_TOKEN,
    )


def _title(page: dict[str, Any]) -> str:
    prop = next(p for p in page["properties"].values() if p["type"] == "title")
    return "".join(t["plain_text"] for t in prop["title"])


async def _every_child(sdk: Sdk, block_id: str) -> list[dict[str, Any]]:
    """The caller's walk: page_size 100 and `next_cursor` until `has_more` is false."""
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = await sdk(lambda c, at=cursor: c.blocks.children.list(block_id, page_size=100, start_cursor=at))
        found += page["results"]
        if not page["has_more"]:
            return found
        cursor = page["next_cursor"]


async def test_an_agent_conversation_from_a_token_to_an_archived_row(async_sdk: Sdk, world: World) -> None:
    sdk = async_sdk
    me = await sdk(lambda c: c.users.me())
    assert me["type"] == "bot"

    hits = await sdk(lambda c: c.search(query="handbook", filter={"property": "object", "value": "page"}))
    [handbook] = hits["results"]
    tree = await _every_child(sdk, handbook["id"])
    subpages = {b["child_page"]["title"]: b["id"] for b in tree if b["type"] == "child_page"}
    database_id = next(b["id"] for b in tree if b["type"] == "child_database")
    steps = await _every_child(sdk, subpages["Onboarding"])
    assert len(steps) == 150
    nested = [b for b in tree if b["has_children"] and b["type"] not in ("child_page", "child_database")]
    assert {b["type"] for b in nested} == {"bulleted_list_item", "toggle", "table"}

    appended = await sdk(
        lambda c: c.blocks.children.append(
            handbook["id"],
            children=[
                {"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "Changelog"}}]}},
                {"type": "to_do", "to_do": {"rich_text": [{"text": {"content": "Review"}}], "checked": False}},
            ],
        )
    )
    todo = appended["results"][1]
    checked = await sdk(lambda c: c.blocks.update(todo["id"], to_do={"checked": True}))
    assert checked["to_do"]["checked"] is True

    child = await sdk(
        lambda c: c.pages.create(
            parent={"page_id": handbook["id"]},
            properties={"title": [{"text": {"content": "Q4 plan"}}]},
        )
    )
    assert child["parent"]["page_id"] == handbook["id"]

    users = (await sdk(lambda c: c.users.list()))["results"]
    dov = next(u for u in users if u["type"] == "person" and u["person"]["email"] == "dov@example.com")
    row = await sdk(
        lambda c: c.pages.create(
            parent={"database_id": database_id},
            properties={
                "Name": {"title": [{"text": {"content": "Write Q4 plan"}}]},
                "Status": {"status": {"name": "Not started"}},
                "Priority": {"select": {"name": "High"}},
                "Tags": {"multi_select": [{"name": "ops"}, {"name": "web"}]},
                "Due": {"date": {"start": "2026-09-18"}},
                "Owner": {"people": [{"object": "user", "id": dov["id"]}]},
                "Signed off": {"checkbox": False},
                "Estimate": {"number": 8},
                "Link": {"url": "https://example.com/q4"},
                "Notes": {"rich_text": [{"text": {"content": "Draft first"}}]},
                "Depends on": {"relation": [{"id": child["id"]}]},
            },
        )
    )
    shapes: list[dict[str, Any]] = [
        {"property": "Name", "title": {"contains": "q4"}},
        {"property": "Notes", "rich_text": {"starts_with": "Draft"}},
        {"property": "Status", "status": {"equals": "Not started"}},
        {"property": "Priority", "select": {"equals": "High"}},
        {"property": "Tags", "multi_select": {"contains": "ops"}},
        {"property": "Due", "date": {"on_or_before": "2026-09-18"}},
        {"property": "Owner", "people": {"contains": dov["id"]}},
        {"property": "Signed off", "checkbox": {"equals": False}},
        {"property": "Estimate", "number": {"greater_than_or_equal_to": 8}},
        {"property": "Link", "url": {"contains": "q4"}},
        {"property": "Depends on", "relation": {"contains": child["id"]}},
        {"timestamp": "created_time", "created_time": {"on_or_after": "2026-09-14"}},
        {
            "and": [
                {"property": "Tags", "multi_select": {"contains": "web"}},
                {"property": "Estimate", "number": {"equals": 8}},
            ]
        },
    ]
    for shape in shapes:
        found = await sdk(lambda c, shape=shape: c.databases.query(database_id, filter=shape))
        assert row["id"] in [r["id"] for r in found["results"]], shape

    world.clock.jump(START + timedelta(days=1, hours=3))
    world.provider.person_sets_property(
        row["id"], "Status", "In progress", by="dov@example.com", world=world.store, clock=world.clock
    )
    changed = world.store.events()[-1]
    assert changed.actor is Actor.PERSON and isinstance(changed.after, RecordSnapshot)

    moving = await sdk(
        lambda c: c.databases.query(database_id, filter={"property": "Status", "status": {"equals": "In progress"}})
    )
    seen = next(r for r in moving["results"] if r["id"] == row["id"])
    assert seen["last_edited_by"]["id"] == dov["id"] and seen["last_edited_time"] == "2026-09-15T11:30:00.000Z"

    await sdk(lambda c: c.pages.update(row["id"], archived=True))
    after = await sdk(
        lambda c: c.databases.query(database_id, filter={"property": "Name", "title": {"contains": "q4"}})
    )
    assert after["results"] == []
    searched = await sdk(lambda c: c.search(query="Write Q4"))
    assert searched["results"] == []
    kept = await sdk(lambda c: c.pages.retrieve(row["id"]))
    assert kept["archived"] is True and _title(kept) == "Write Q4 plan"
    with pytest.raises(APIResponseError) as raised:
        await sdk(lambda c: c.pages.update(row["id"], properties={"Estimate": {"number": 1}}))
    assert raised.value.code == "validation_error"

    agent_writes = [e for e in world.store.events() if e.actor is Actor.AGENT and e.operation is not Operation.READ]
    assert agent_writes and all(e.entity.provider == "notion" for e in agent_writes)
