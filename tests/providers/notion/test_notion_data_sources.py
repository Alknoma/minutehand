"""Notion-Version 2025-09-03, the version that brought data sources: each database of this fake has one, named with
its title, and the shapes are those recorded of the real service on that version (`CLAIMS.md` beside the provider)."""

from __future__ import annotations

from typing import Any

import httpx
from notion_client import AsyncClient

from tests.providers.notion.notion_world import (
    AGENT_TOKEN,
    API,
    World,
    answer,
    direct,
    ids,
    refusal,
    served,
    unserved,
)

NEW = {"Notion-Version": "2025-09-03"}


def new(world: World) -> httpx.AsyncClient:
    client = direct(world)
    client.headers.update(NEW)
    return client


def _title(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text}}]


async def test_a_database_answers_its_one_data_source_named_with_its_title_and_no_schema(world: World) -> None:
    """RECORDED (2025-09-03): GET /v1/databases answers `data_sources` `[{id, name}]`, the name its title, and no
    `properties`; the data source's own `title` is the same text, its `parent` the database and `database_parent`
    the database's parent; the ids are not the same
    (https://github.com/ramnes/notion-sdk-py/blob/af5e5e2b5e889bad0e4bbbcb9b11a3fd1dca5525/tests/cassettes/test_databases_retrieve.yaml#L92,
    https://github.com/ramnes/notion-sdk-py/blob/af5e5e2b5e889bad0e4bbbcb9b11a3fd1dca5525/tests/cassettes/test_data_sources_retrieve.yaml#L127)."""
    async with new(world) as api:
        database = answer(await api.get(f"/v1/databases/{ids('projects')}"))
        assert sorted(database) == sorted(
            [
                "object",
                "id",
                "title",
                "description",
                "parent",
                "is_inline",
                "in_trash",
                "is_locked",
                "created_time",
                "last_edited_time",
                "data_sources",
                "icon",
                "cover",
                "url",
                "public_url",
                "archived",
            ]
        )
        [source] = database["data_sources"]
        assert source["name"] == "Projects" and source["id"] != ids("projects")

        found = answer(await api.get(f"/v1/data_sources/{source['id']}"))
    assert (found["object"], found["id"]) == ("data_source", source["id"])
    assert [t["plain_text"] for t in found["title"]] == ["Projects"]
    assert found["parent"] == {"type": "database_id", "database_id": ids("projects")}
    assert found["database_parent"] == {"type": "page_id", "page_id": ids("handbook")}
    assert "Status" in found["properties"] and found["properties"]["Status"]["description"] is None
    assert "is_locked" not in found


async def test_a_data_source_is_queried_and_its_rows_name_it_beside_their_database(world: World) -> None:
    """RECORDED (2025-09-03): `POST /v1/data_sources/{id}/query` lists `page_or_data_source`, and each row's
    `parent` is `{type: data_source_id, data_source_id, database_id}`
    (https://github.com/ultimate-notion/ultimate-notion/blob/913f444c69cae690d795dff0aeea0b5722fe4721/tests/cassettes/test_query/test_people_relation_query.yaml#L457).
    On 2022-06-28 the same row's parent is its database."""
    async with new(world) as api:
        [source] = answer(await api.get(f"/v1/databases/{ids('projects')}"))["data_sources"]
        listed = answer(await api.post(f"/v1/data_sources/{source['id']}/query", json={}))
    assert (listed["type"], listed["page_or_data_source"]) == ("page_or_data_source", {})
    assert {r["id"] for r in listed["results"]} == {ids("launch"), ids("audit")}
    assert all(
        r["parent"] == {"type": "data_source_id", "data_source_id": source["id"], "database_id": ids("projects")}
        for r in listed["results"]
    )
    async with direct(world) as legacy:
        row = answer(await legacy.get(f"/v1/pages/{ids('launch')}"))
    assert row["parent"] == {"type": "database_id", "database_id": ids("projects")}


async def test_a_row_is_made_under_a_data_source_on_either_version(world: World) -> None:
    """DOCUMENTED: a `data_source_id` parent "works for any API version"
    (https://developers.notion.com/guides/get-started/upgrade-guide-2025-09-03)."""
    async with new(world) as api:
        [source] = answer(await api.get(f"/v1/databases/{ids('projects')}"))["data_sources"]
    parent = {"type": "data_source_id", "data_source_id": source["id"]}
    async with direct(world) as legacy:
        made = answer(
            await legacy.post("/v1/pages", json={"parent": parent, "properties": {"Name": {"title": _title("Old")}}})
        )
    assert made["parent"] == {"type": "database_id", "database_id": ids("projects")}
    async with new(world) as api:
        made = answer(
            await api.post("/v1/pages", json={"parent": parent, "properties": {"Name": {"title": _title("New")}}})
        )
        listed = answer(await api.post(f"/v1/data_sources/{source['id']}/query", json={}))
    assert made["parent"]["data_source_id"] == source["id"]
    assert made["id"] in {r["id"] for r in listed["results"]}


async def test_a_database_is_made_with_its_first_data_sources_schema_and_relations_name_data_sources(
    world: World,
) -> None:
    """DOCUMENTED: on 2025-09-03 the schema goes under `initial_data_source.properties`, and a relation names its
    target only by `data_source_id`, answering both ids
    (https://developers.notion.com/guides/get-started/upgrade-guide-2025-09-03); RECORDED, a database made so names
    its data source with its title
    (https://github.com/ultimate-notion/ultimate-notion/blob/913f444c69cae690d795dff0aeea0b5722fe4721/tests/cassettes/test_query/test_people_relation_query.yaml#L33)."""
    async with new(world) as api:
        [projects] = answer(await api.get(f"/v1/databases/{ids('projects')}"))["data_sources"]
        body = {
            "parent": {"type": "page_id", "page_id": ids("handbook")},
            "title": _title("Risks"),
            "initial_data_source": {
                "properties": {
                    "Name": {"title": {}},
                    "Project": {"relation": {"data_source_id": projects["id"], "single_property": {}}},
                }
            },
        }
        made = answer(await api.post("/v1/databases", json=body))
        [source] = made["data_sources"]
        assert source["name"] == "Risks"
        schema = answer(await api.get(f"/v1/data_sources/{source['id']}"))["properties"]
        relation = schema["Project"]["relation"]
        assert (relation["database_id"], relation["data_source_id"]) == (ids("projects"), projects["id"])

        by_database = {"relation": {"database_id": ids("projects"), "single_property": {}}}
        refused = await api.patch(f"/v1/data_sources/{source['id']}", json={"properties": {"Again": by_database}})
        assert "relation.database_id on 2025-09-03" in unserved(refused)

        added = await api.patch(f"/v1/data_sources/{source['id']}", json={"properties": {"Done": {"checkbox": {}}}})
        assert "Done" in answer(added)["properties"]


async def test_search_finds_data_sources_on_2025_09_03_and_databases_on_2022_06_28(world: World) -> None:
    """DOCUMENTED: on 2025-09-03 search's `filter.value` is "page" or "data_source", and it answers data sources,
    found by their database's title (https://developers.notion.com/guides/get-started/upgrade-guide-2025-09-03)."""
    wanted = {"query": "Projects", "filter": {"property": "object", "value": "data_source"}}
    async with new(world) as api:
        found = answer(await api.post("/v1/search", json=wanted))
        old_value = await api.post("/v1/search", json={"filter": {"property": "object", "value": "database"}})
    assert found["type"] == "page_or_data_source"
    assert [(r["object"], r["parent"]["database_id"]) for r in found["results"]] == [("data_source", ids("projects"))]
    refusal(old_value, 400, "validation_error")

    async with direct(world) as legacy:
        found = answer(
            await legacy.post("/v1/search", json={**wanted, "filter": {"property": "object", "value": "database"}})
        )
    assert found["type"] == "page_or_database"
    assert [(r["object"], r["id"]) for r in found["results"]] == [("database", ids("projects"))]


async def test_an_id_of_the_other_kind_or_a_data_source_on_2022_06_28_is_refused(world: World) -> None:
    """MEASURED (2025-09-03, brekkylab/backlot#376): a database's id on `/v1/data_sources` and a data source's id on
    `/v1/databases` are 404, "Could not find data_source with ID: ..."; data sources do not exist on 2022-06-28, and
    the old query path on 2025-09-03, whose answer is not documented, is refused by name."""
    async with new(world) as api:
        [source] = answer(await api.get(f"/v1/databases/{ids('projects')}"))["data_sources"]
        missing = refusal(await api.get(f"/v1/data_sources/{ids('projects')}"), 404, "object_not_found")
        assert missing.startswith(f"Could not find data_source with ID: {ids('projects')}.")
        refusal(await api.get(f"/v1/databases/{source['id']}"), 404, "object_not_found")
        assert "querying moved" in unserved(await api.post(f"/v1/databases/{ids('projects')}/query", json={}))
    async with direct(world) as legacy:
        assert "2022-06-28" in unserved(await legacy.get(f"/v1/data_sources/{source['id']}"))


async def test_a_renamed_databases_data_source_is_refused_by_name_on_2025_09_03(world: World) -> None:
    """Whether a data source's name follows its database's title after a rename is neither documented nor recorded."""
    async with direct(world) as legacy:
        answer(await legacy.patch(f"/v1/databases/{ids('projects')}", json={"title": _title("Programs")}))
    async with new(world) as api:
        assert "renamed since it was made" in unserved(await api.get(f"/v1/databases/{ids('projects')}"))


async def test_the_stock_sdk_on_its_default_version_reads_and_queries_a_data_source(world: World) -> None:
    """The official `notion-client` sends 2025-09-03 unless pinned."""
    transport = httpx.ASGITransport(app=served(world))
    async with httpx.AsyncClient(transport=transport, base_url=API) as http:
        client = AsyncClient(auth=AGENT_TOKEN, client=http, retry=False)
        assert client.options.notion_version == "2025-09-03"
        database = await client.databases.retrieve(database_id=ids("projects"))
        source = database["data_sources"][0]["id"]
        rows = await client.data_sources.query(data_source_id=source)
    assert {r["id"] for r in rows["results"]} == {ids("launch"), ids("audit")}
