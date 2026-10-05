"""Every call, through the real proxy over TLS at api.notion.com, with the official `notion-client` 2.2.1 SDK as a
service builds it (`Client` and `AsyncClient` over an httpx client given the proxy and its CA). Each test runs once
with the sync client and once with the async one."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from notion_client import APIResponseError

from minutehand.domain.world import Actor, EntityKind, Operation
from tests.providers.notion.notion_world import START, Sdk, ids


def _titles(results: list[Any]) -> list[str]:
    found: list[str] = []
    for item in results:
        if item["object"] == "database":
            found.append("".join(t["plain_text"] for t in item["title"]))
        else:
            title = next(p for p in item["properties"].values() if p["type"] == "title")
            found.append("".join(t["plain_text"] for t in title["title"]))
    return found


# --------------------------------------------------------------------------- users


async def test_users_me_is_the_bot_with_its_owner_and_workspace(sdk: Sdk) -> None:
    me = await sdk(lambda c: c.users.me())
    assert (me["object"], me["type"], me["name"]) == ("user", "bot", "Planning bot")
    assert me["bot"] == {"owner": {"type": "workspace", "workspace": True}, "workspace_name": "Acme"}


async def test_users_list_holds_the_people_and_the_bots_and_retrieve_reads_one(sdk: Sdk) -> None:
    listed = await sdk(lambda c: c.users.list(page_size=3))
    assert listed["has_more"] is True and len(listed["results"]) == 3
    rest = await sdk(lambda c: c.users.list(start_cursor=listed["next_cursor"]))
    everyone = listed["results"] + rest["results"]
    people = [u for u in everyone if u["type"] == "person"]
    assert sorted(u["person"]["email"] for u in people) == ["dov@example.com", "mara@example.com"]
    assert sorted(u["name"] for u in everyone if u["type"] == "bot") == [
        "Connector",
        "Other bot",
        "Planning bot",
        "Reader",
    ]
    one = await sdk(lambda c: c.users.retrieve(people[0]["id"]))
    assert one == people[0]


# --------------------------------------------------------------------------- search


async def test_search_by_query_finds_titles_it_can_reach(sdk: Sdk) -> None:
    found = await sdk(lambda c: c.search(query="board", page_size=100))
    assert _titles(found["results"]) == ["Onboarding"]
    assert found["object"] == "list" and found["type"] == "page_or_database"


async def test_search_filtered_to_pages_or_to_databases(sdk: Sdk) -> None:
    pages = await sdk(lambda c: c.search(filter={"property": "object", "value": "page"}))
    assert sorted(_titles(pages["results"])) == ["Launch site", "Onboarding", "Security audit", "Team Handbook"]
    databases = await sdk(lambda c: c.search(filter={"property": "object", "value": "database"}))
    assert _titles(databases["results"]) == ["Projects"]


async def test_search_sorted_and_paginated(sdk: Sdk) -> None:
    sdk.clock.jump(START + timedelta(hours=2))
    await sdk(lambda c: c.pages.update(ids("audit"), properties={"Estimate": {"number": 3}}))
    newest = await sdk(lambda c: c.search(sort={"direction": "descending", "timestamp": "last_edited_time"}))
    oldest = await sdk(lambda c: c.search(sort={"direction": "ascending", "timestamp": "last_edited_time"}))
    assert newest["results"][0]["id"] == ids("audit")
    assert oldest["results"][-1]["id"] == ids("audit")
    first = await sdk(lambda c: c.search(page_size=2))
    second = await sdk(lambda c: c.search(page_size=2, start_cursor=first["next_cursor"]))
    third = await sdk(lambda c: c.search(page_size=2, start_cursor=second["next_cursor"]))
    walked = [r["id"] for r in first["results"] + second["results"] + third["results"]]
    assert len(walked) == len(set(walked)) == 5 and third["has_more"] is False and third["next_cursor"] is None


# --------------------------------------------------------------------------- pages


async def test_pages_retrieve_reads_a_row_with_every_property_type(sdk: Sdk) -> None:
    row = await sdk(lambda c: c.pages.retrieve(ids("launch")))
    props = row["properties"]
    assert row["parent"] == {"type": "database_id", "database_id": ids("projects")}
    assert props["Status"]["status"]["name"] == "In progress"
    assert props["Priority"]["select"]["name"] == "High"
    assert [o["name"] for o in props["Tags"]["multi_select"]] == ["web"]
    assert props["Due"]["date"] == {"start": "2026-09-20", "end": None, "time_zone": None}
    assert [p["person"]["email"] for p in props["Owner"]["people"]] == ["mara@example.com"]
    assert props["Signed off"]["checkbox"] is False and props["Estimate"]["number"] == 5
    assert props["Link"]["url"] == "https://example.com/launch"
    assert props["Notes"]["rich_text"][0]["plain_text"] == "Ship it"
    assert props["Depends on"]["relation"] == [{"id": ids("audit")}]
    assert props["Created"]["created_time"] == "2026-09-14T08:30:00.000Z"
    assert row["url"].startswith("https://www.notion.so/Launch-site-")


async def test_pages_create_under_a_page_with_children(sdk: Sdk) -> None:
    made = await sdk(
        lambda c: c.pages.create(
            parent={"page_id": ids("handbook")},
            properties={"title": [{"type": "text", "text": {"content": "Retro notes"}}]},
            children=[
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"text": {"content": "Went well"}}]},
                }
            ],
        )
    )
    assert made["parent"] == {"type": "page_id", "page_id": ids("handbook")}
    children = await sdk(lambda c: c.blocks.children.list(made["id"]))
    assert [b["paragraph"]["rich_text"][0]["plain_text"] for b in children["results"]] == ["Went well"]
    under = await sdk(lambda c: c.blocks.children.list(ids("handbook")))
    assert under["results"][-1]["type"] == "child_page"
    assert under["results"][-1]["child_page"] == {"title": "Retro notes"}


async def test_pages_create_a_row_with_properties(sdk: Sdk) -> None:
    made = await sdk(
        lambda c: c.pages.create(
            parent={"database_id": ids("projects")},
            properties={
                "Name": {"title": [{"text": {"content": "Hire designer"}}]},
                "Status": {"status": {"name": "Not started"}},
                "Priority": {"select": {"name": "Urgent"}},
                "Due": {"date": {"start": "2026-09-30T09:00:00Z"}},
            },
        )
    )
    assert made["properties"]["Priority"]["select"]["name"] == "Urgent"
    schema = await sdk(lambda c: c.databases.retrieve(ids("projects")))
    assert [o["name"] for o in schema["properties"]["Priority"]["select"]["options"]] == ["Low", "High", "Urgent"]


async def test_pages_update_properties_and_archive(sdk: Sdk) -> None:
    changed = await sdk(lambda c: c.pages.update(ids("audit"), properties={"Status": {"status": {"name": "Done"}}}))
    assert changed["properties"]["Status"]["status"]["name"] == "Done"
    archived = await sdk(lambda c: c.pages.update(ids("audit"), archived=True))
    assert archived["archived"] is True and archived["in_trash"] is True
    restored = await sdk(lambda c: c.pages.update(ids("audit"), archived=False))
    assert restored["archived"] is False


async def test_pages_properties_retrieve_paginates_a_title_and_reads_a_number(sdk: Sdk) -> None:
    title = await sdk(lambda c: c.pages.properties.retrieve(ids("launch"), "title"))
    assert title["object"] == "list" and title["type"] == "property_item"
    assert title["results"][0]["title"]["plain_text"] == "Launch site"
    row = await sdk(lambda c: c.pages.retrieve(ids("launch")))
    estimate = row["properties"]["Estimate"]["id"]
    number = await sdk(lambda c: c.pages.properties.retrieve(ids("launch"), estimate))
    assert number == {"object": "property_item", "id": estimate, "type": "number", "number": 5}


# --------------------------------------------------------------------------- blocks


async def test_blocks_retrieve_a_block_and_a_page_as_a_block(sdk: Sdk) -> None:
    first = (await sdk(lambda c: c.blocks.children.list(ids("handbook"))))["results"][0]
    again = await sdk(lambda c: c.blocks.retrieve(first["id"]))
    assert again == first and again["heading_1"]["rich_text"][0]["plain_text"] == "Welcome"
    page = await sdk(lambda c: c.blocks.retrieve(ids("onboarding")))
    assert page["type"] == "child_page" and page["child_page"] == {"title": "Onboarding"}


async def test_blocks_children_list_paginates_at_a_hundred_and_nests(sdk: Sdk) -> None:
    first = await sdk(lambda c: c.blocks.children.list(ids("onboarding")))
    assert len(first["results"]) == 100 and first["has_more"] is True
    second = await sdk(lambda c: c.blocks.children.list(ids("onboarding"), start_cursor=first["next_cursor"]))
    assert len(second["results"]) == 50 and second["has_more"] is False
    assert second["results"][-1]["paragraph"]["rich_text"][0]["plain_text"] == "Step 149"
    top = (await sdk(lambda c: c.blocks.children.list(ids("handbook"))))["results"]
    kinds = [b["type"] for b in top]
    assert kinds[:14] == [
        "heading_1",
        "paragraph",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "toggle",
        "code",
        "quote",
        "callout",
        "divider",
        "table",
        "bookmark",
        "link_preview",
        "image",
    ]
    assert kinds[14:] == ["child_page", "child_database"]
    toggle = top[5]
    assert toggle["has_children"] is True
    inside = await sdk(lambda c: c.blocks.children.list(toggle["id"]))
    assert inside["results"][0]["parent"] == {"type": "block_id", "block_id": toggle["id"]}
    table = top[10]
    rows = await sdk(lambda c: c.blocks.children.list(table["id"]))
    assert [[cell[0]["plain_text"] for cell in r["table_row"]["cells"]] for r in rows["results"]] == [
        ["Team", "Lead"],
        ["Web", "Mara"],
    ]


async def test_blocks_children_append_at_the_end_and_after_a_block(sdk: Sdk) -> None:
    def paragraph(text: str) -> dict[str, Any]:
        return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

    first_two = (await sdk(lambda c: c.blocks.children.list(ids("handbook"), page_size=2)))["results"]
    appended = await sdk(lambda c: c.blocks.children.append(ids("handbook"), children=[paragraph("Last")]))
    assert appended["object"] == "list" and appended["results"][0]["paragraph"]["rich_text"][0]["plain_text"] == "Last"
    await sdk(
        lambda c: c.blocks.children.append(
            ids("handbook"), children=[paragraph("Inserted"), paragraph("Then")], after=first_two[0]["id"]
        )
    )
    order = (await sdk(lambda c: c.blocks.children.list(ids("handbook"), page_size=4)))["results"]
    assert order[0]["id"] == first_two[0]["id"] and order[3]["id"] == first_two[1]["id"]
    assert [b["paragraph"]["rich_text"][0]["plain_text"] for b in order[1:3]] == ["Inserted", "Then"]


async def test_blocks_update_merges_into_the_block(sdk: Sdk) -> None:
    todo = (await sdk(lambda c: c.blocks.children.list(ids("handbook"))))["results"][4]
    changed = await sdk(lambda c: c.blocks.update(todo["id"], to_do={"checked": False}))
    assert changed["to_do"]["checked"] is False
    assert changed["to_do"]["rich_text"][0]["plain_text"] == "Sign the policy"
    bold = await sdk(
        lambda c: c.blocks.update(
            todo["id"], to_do={"rich_text": [{"text": {"content": "Read it"}, "annotations": {"bold": True}}]}
        )
    )
    assert bold["to_do"]["rich_text"][0]["annotations"]["bold"] is True and bold["to_do"]["checked"] is False


async def test_blocks_delete_archives_the_block_and_hides_it(sdk: Sdk) -> None:
    quote = (await sdk(lambda c: c.blocks.children.list(ids("handbook"))))["results"][7]
    gone = await sdk(lambda c: c.blocks.delete(quote["id"]))
    assert gone["archived"] is True
    left = (await sdk(lambda c: c.blocks.children.list(ids("handbook"))))["results"]
    assert quote["id"] not in [b["id"] for b in left]


# --------------------------------------------------------------------------- databases


async def test_databases_retrieve_reads_the_schema(sdk: Sdk) -> None:
    database = await sdk(lambda c: c.databases.retrieve(ids("projects")))
    assert database["object"] == "database" and database["title"][0]["plain_text"] == "Projects"
    assert {n: p["type"] for n, p in database["properties"].items()}["Status"] == "status"
    assert database["properties"]["Name"]["id"] == "title"
    assert database["properties"]["Depends on"]["relation"]["database_id"] == ids("projects")


FILTERS: list[tuple[str, dict[str, Any], list[str]]] = [
    ("title equals", {"property": "Name", "title": {"equals": "Launch site"}}, ["Launch site"]),
    ("title contains", {"property": "Name", "title": {"contains": "AUDIT"}}, ["Security audit"]),
    ("title starts_with", {"property": "Name", "title": {"starts_with": "launch"}}, ["Launch site"]),
    ("title ends_with", {"property": "Name", "title": {"ends_with": "audit"}}, ["Security audit"]),
    ("title does_not_contain", {"property": "Name", "title": {"does_not_contain": "site"}}, ["Security audit"]),
    ("rich_text is_empty", {"property": "Notes", "rich_text": {"is_empty": True}}, ["Security audit"]),
    ("rich_text is_not_empty", {"property": "Notes", "rich_text": {"is_not_empty": True}}, ["Launch site"]),
    ("url equals", {"property": "Link", "url": {"equals": "https://example.com/launch"}}, ["Launch site"]),
    ("number greater_than", {"property": "Estimate", "number": {"greater_than": 3}}, ["Launch site"]),
    (
        "number less_than_or_equal_to",
        {"property": "Estimate", "number": {"less_than_or_equal_to": 2}},
        ["Security audit"],
    ),
    ("checkbox equals", {"property": "Signed off", "checkbox": {"equals": True}}, ["Security audit"]),
    ("select equals", {"property": "Priority", "select": {"equals": "High"}}, ["Launch site"]),
    ("select does_not_equal", {"property": "Priority", "select": {"does_not_equal": "High"}}, ["Security audit"]),
    ("status equals", {"property": "Status", "status": {"equals": "Not started"}}, ["Security audit"]),
    ("multi_select contains", {"property": "Tags", "multi_select": {"contains": "ops"}}, ["Security audit"]),
    ("date before", {"property": "Due", "date": {"before": "2026-09-25"}}, ["Launch site"]),
    ("date on_or_after", {"property": "Due", "date": {"on_or_after": "2026-10-01"}}, ["Security audit"]),
    ("date next_week", {"property": "Due", "date": {"next_week": {}}}, ["Launch site"]),
    ("date is_empty", {"property": "Due", "date": {"is_empty": True}}, []),
    (
        "relation contains",
        {"property": "Depends on", "relation": {"contains": ids("audit").replace("-", "")}},
        ["Launch site"],
    ),
    ("relation is_empty", {"property": "Depends on", "relation": {"is_empty": True}}, ["Security audit"]),
    (
        "timestamp created_time",
        {"timestamp": "created_time", "created_time": {"on_or_after": "2026-09-14"}},
        ["Launch site", "Security audit"],
    ),
    (
        "timestamp last_edited_time",
        {"timestamp": "last_edited_time", "last_edited_time": {"past_week": {}}},
        ["Launch site", "Security audit"],
    ),
    (
        "and",
        {
            "and": [
                {"property": "Tags", "multi_select": {"contains": "web"}},
                {"property": "Estimate", "number": {"equals": 5}},
            ]
        },
        ["Launch site"],
    ),
    (
        "or nested",
        {
            "or": [
                {"property": "Priority", "select": {"equals": "Low"}},
                {
                    "and": [
                        {"property": "Signed off", "checkbox": {"equals": False}},
                        {"property": "Estimate", "number": {"greater_than": 10}},
                    ]
                },
            ]
        },
        ["Security audit"],
    ),
]


@pytest.mark.parametrize(("shape", "found", "expected"), FILTERS, ids=[f[0] for f in FILTERS])
async def test_databases_query_by_filter_shape(
    sdk: Sdk, shape: str, found: dict[str, Any], expected: list[str]
) -> None:
    rows = await sdk(lambda c: c.databases.query(ids("projects"), filter=found))
    assert sorted(_titles(rows["results"])) == expected


async def test_databases_query_by_people(sdk: Sdk) -> None:
    users = (await sdk(lambda c: c.users.list()))["results"]
    dov = next(u["id"] for u in users if u["type"] == "person" and u["person"]["email"] == "dov@example.com")
    rows = await sdk(
        lambda c: c.databases.query(ids("projects"), filter={"property": "Owner", "people": {"contains": dov}})
    )
    assert _titles(rows["results"]) == ["Security audit"]


async def test_databases_query_sorts_and_paginates(sdk: Sdk) -> None:
    by_status = await sdk(
        lambda c: c.databases.query(ids("projects"), sorts=[{"property": "Status", "direction": "ascending"}])
    )
    assert _titles(by_status["results"]) == ["Security audit", "Launch site"]
    by_estimate = await sdk(
        lambda c: c.databases.query(
            ids("projects"), sorts=[{"property": "Estimate", "direction": "descending"}], page_size=1
        )
    )
    assert _titles(by_estimate["results"]) == ["Launch site"] and by_estimate["has_more"] is True
    rest = await sdk(
        lambda c: c.databases.query(
            ids("projects"),
            sorts=[{"property": "Estimate", "direction": "descending"}],
            start_cursor=by_estimate["next_cursor"],
        )
    )
    assert _titles(rest["results"]) == ["Security audit"]


async def test_databases_create_and_update_the_schema(sdk: Sdk) -> None:
    made = await sdk(
        lambda c: c.databases.create(
            parent={"type": "page_id", "page_id": ids("handbook")},
            title=[{"type": "text", "text": {"content": "Risks"}}],
            properties={
                "Risk": {"title": {}},
                "Level": {"select": {"options": [{"name": "Low"}, {"name": "High"}]}},
                "Owner": {"people": {}},
            },
        )
    )
    assert sorted(made["properties"]) == ["Level", "Owner", "Risk"]
    changed = await sdk(
        lambda c: c.databases.update(
            made["id"],
            title=[{"text": {"content": "Risk register"}}],
            properties={"Level": {"name": "Severity"}, "Owner": None, "Reviewed": {"checkbox": {}}},
        )
    )
    assert sorted(changed["properties"]) == ["Reviewed", "Risk", "Severity"]
    assert changed["title"][0]["plain_text"] == "Risk register"
    assert changed["properties"]["Severity"]["id"] == made["properties"]["Level"]["id"]


# --------------------------------------------------------------------------- comments


async def test_comments_create_and_list(sdk: Sdk) -> None:
    made = await sdk(
        lambda c: c.comments.create(
            parent={"page_id": ids("handbook")}, rich_text=[{"text": {"content": "Looks good"}}]
        )
    )
    await sdk(lambda c: c.comments.create(discussion_id=made["discussion_id"], rich_text=[{"text": {"content": "+1"}}]))
    listed = await sdk(lambda c: c.comments.list(block_id=ids("handbook")))
    assert [c["rich_text"][0]["plain_text"] for c in listed["results"]] == ["Looks good", "+1"]
    assert {c["discussion_id"] for c in listed["results"]} == {made["discussion_id"]}


# --------------------------------------------------------------------------- what the SDK raises


async def test_a_page_the_integration_cannot_reach_is_raised_as_object_not_found(sdk: Sdk) -> None:
    with pytest.raises(APIResponseError) as raised:
        await sdk(lambda c: c.pages.retrieve(ids("private")))
    assert raised.value.code == "object_not_found" and raised.value.status == 404


async def test_a_read_and_a_write_land_in_the_log_with_the_page_as_the_entity(sdk: Sdk) -> None:
    before = sdk.store.head()
    await sdk(lambda c: c.blocks.children.list(ids("handbook")))
    first = (await sdk(lambda c: c.blocks.children.list(ids("handbook"), page_size=1)))["results"][0]
    await sdk(lambda c: c.blocks.update(first["id"], heading_1={"rich_text": [{"text": {"content": "Hello"}}]}))
    events = sdk.store.events(since=before)
    assert [(e.operation, e.entity.kind) for e in events] == [
        (Operation.READ, EntityKind.DOCUMENT),
        (Operation.READ, EntityKind.DOCUMENT),
        (Operation.UPDATE, EntityKind.DOCUMENT),
    ]
    assert {e.entity.external_id for e in events} == {ids("handbook")} and events[-1].actor is Actor.AGENT
