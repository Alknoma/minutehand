"""The world a Notion seed writes, what people do in it without the agent, and how either reads in the log."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from minutehand.adapters.providers.notion.seed import NotionSeed
from minutehand.domain.scenario import (
    Commented,
    DocumentAction,
    DocumentHappening,
    Edited,
    FieldSet,
    Renamed,
    SeededDocument,
    Trashed,
)
from minutehand.domain.world import Actor, DocumentSnapshot, EntityKind, Operation, RecordSnapshot
from tests.providers.notion.notion_world import (
    NOTION,
    START,
    World,
    answer,
    direct,
    ids,
    scenario,
    seeded,
)


def test_a_seeded_page_reads_as_a_document_with_its_whole_text(world: World) -> None:
    created = [
        e for e in world.store.events() if e.entity.external_id == ids("handbook") and e.operation is Operation.CREATE
    ]
    assert len(created) == 1 and created[0].actor is Actor.SCENARIO
    latest = world.store.events()
    snapshot = next(e.after for e in reversed(latest) if e.entity.external_id == ids("handbook"))
    assert isinstance(snapshot, DocumentSnapshot)
    assert snapshot.title == "Team Handbook" and snapshot.parent is None
    assert snapshot.text is not None and snapshot.text.splitlines()[:5] == [
        "Welcome",
        "Read this first.",
        "Be kind",
        "  Always.",
        "Ask early",
    ]
    assert "[x] Sign the policy" in snapshot.text and "Team | Lead" in snapshot.text and "Onboarding" in snapshot.text
    assert snapshot.last_edited_by == "mara@example.com" and snapshot.last_edited_at == START


def test_a_seeded_row_reads_as_a_record_with_its_properties(world: World) -> None:
    row = next(e.after for e in world.store.events() if e.entity.external_id == ids("launch"))
    assert isinstance(row, RecordSnapshot) and row.resource == "database_row"
    assert "Launch site" in row.text and "Status: In progress" in row.text and "Priority: High" in row.text


def test_a_scenarios_documents_become_pages_shared_with_every_integration(tmp_path: Path) -> None:
    documents = [
        SeededDocument(
            provider="notion", title="Pricing memo", text="Tier one is 40.\nTier two is 90.", folder="Finance"
        ),
        SeededDocument(provider="google_workspace", title="Not Notion's", text="elsewhere"),
    ]
    world = seeded(tmp_path, scenario(documents=documents))
    pages = [e.after for e in world.store.events() if isinstance(e.after, DocumentSnapshot)]
    memo = next(p for p in pages if p.title == "Pricing memo")
    assert memo.text == "Tier one is 40.\nTier two is 90." and memo.parent == "Finance"
    assert not any(p.title == "Not Notion's" for p in pages)


async def test_a_scenarios_document_is_found_by_the_agent(tmp_path: Path) -> None:
    world = seeded(tmp_path, scenario(documents=[SeededDocument(provider="notion", title="Pricing memo", text="40")]))
    async with direct(world) as api:
        found = answer(await api.post("/v1/search", json={"query": "pricing"}))
        assert [r["properties"]["title"]["title"][0]["plain_text"] for r in found["results"]] == ["Pricing memo"]


def test_a_seed_naming_a_page_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match="used twice"):
        NotionSeed.model_validate(
            {
                "workspaces": [
                    {"key": "a", "name": "A", "pages": [{"key": "p", "title": "1"}, {"key": "p", "title": "2"}]}
                ]
            }
        )


def test_a_seed_sharing_what_is_not_there_is_refused() -> None:
    with pytest.raises(ValidationError, match="shared"):
        NotionSeed.model_validate(
            {
                "workspaces": [
                    {"key": "a", "name": "A", "integrations": [{"key": "i", "name": "I", "shared": ["ghost"]}]}
                ]
            }
        )


def test_a_public_integration_without_its_client_is_refused() -> None:
    with pytest.raises(ValidationError, match="client_id"):
        NotionSeed.model_validate(
            {"workspaces": [{"key": "a", "name": "A", "integrations": [{"key": "i", "name": "I", "type": "public"}]}]}
        )


def test_the_same_seed_mints_the_same_ids_in_every_run(tmp_path: Path) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    first = seeded(tmp_path / "one")
    second = seeded(tmp_path / "two")
    assert [e.entity for e in first.store.events()] == [e.entity for e in second.store.events()]


def test_without_a_notion_seed_there_is_a_workspace_and_no_integration(tmp_path: Path) -> None:
    world = seeded(tmp_path, scenario(notion={}))
    assert {e.entity.kind for e in world.store.events()} == {EntityKind.RECORD}


# --------------------------------------------------------------------------- people acting


def later(world: World, hours: int) -> None:
    world.clock.jump(START + timedelta(hours=hours))


def does(world: World, person: str, document: str, action: DocumentAction) -> None:
    """The person does `action` to the seeded document now, through the port a run lands it with."""
    happening = DocumentHappening(person=person, document=document, after=timedelta(minutes=1), action=action)
    world.provider.change(happening, scenario(), world.store, world.clock)


async def test_a_person_edits_a_page_and_the_agent_reads_it_with_who_and_when(world: World) -> None:
    later(world, 5)
    before = world.store.head()
    does(world, "dov", "Team Handbook", Edited(append="Updated by Dov."))
    [event] = world.store.events(since=before)
    assert (event.actor, event.operation, event.entity.external_id) == (Actor.PERSON, Operation.UPDATE, ids("handbook"))
    assert event.sim_time == START + timedelta(hours=5)
    assert isinstance(event.after, DocumentSnapshot) and event.after.text is not None
    assert event.after.text.endswith("Updated by Dov.") and event.after.last_edited_by == "dov@example.com"
    async with direct(world) as api:
        page = answer(await api.get(f"/v1/pages/{ids('handbook')}"))
        users = answer(await api.get("/v1/users"))["results"]
        dov = next(u["id"] for u in users if u["type"] == "person" and u["person"]["email"] == "dov@example.com")
        assert page["last_edited_by"] == {"object": "user", "id": dov}
        assert page["last_edited_time"] == "2026-09-14T13:30:00.000Z"


async def test_a_person_sets_a_rows_status_and_the_agent_finds_it_by_filter(world: World) -> None:
    later(world, 26)
    does(world, "mara", "Launch site", FieldSet(field="Status", value="Done"))
    event = world.store.events()[-1]
    assert (
        event.actor is Actor.PERSON and isinstance(event.after, RecordSnapshot) and "Status: Done" in event.after.text
    )
    async with direct(world) as api:
        done = answer(
            await api.post(
                f"/v1/databases/{ids('projects')}/query",
                json={"filter": {"property": "Status", "status": {"equals": "Done"}}},
            )
        )
        assert [r["id"] for r in done["results"]] == [ids("launch")]
        assert done["results"][0]["last_edited_time"] == "2026-09-15T10:30:00.000Z"


async def test_a_person_sets_people_by_email(world: World) -> None:
    does(world, "mara", "Launch site", FieldSet(field="Owner", value="dov"))
    async with direct(world) as api:
        row = answer(await api.get(f"/v1/pages/{ids('launch')}"))
        assert [p["person"]["email"] for p in row["properties"]["Owner"]["people"]] == ["dov@example.com"]


async def test_a_person_comments_and_the_agent_lists_it(world: World) -> None:
    does(world, "dov", "Team Handbook", Commented(text="Please review."))
    assert world.store.events()[-1].entity.kind is EntityKind.COMMENT
    async with direct(world) as api:
        listed = answer(await api.get("/v1/comments", params={"block_id": ids("handbook")}))
        assert [c["rich_text"][0]["plain_text"] for c in listed["results"]] == ["Please review."]


async def test_a_person_archives_a_page_and_the_agent_no_longer_finds_it(world: World) -> None:
    does(world, "mara", "Onboarding", Trashed())
    async with direct(world) as api:
        found = answer(await api.post("/v1/search", json={"query": "Onboarding"}))
        assert found["results"] == []
        children = answer(await api.get(f"/v1/blocks/{ids('handbook')}/children"))["results"]
        assert ids("onboarding") not in [b["id"] for b in children]
        assert answer(await api.get(f"/v1/pages/{ids('onboarding')}"))["archived"] is True


async def test_a_person_renames_a_page_and_a_row(world: World) -> None:
    does(world, "dov", "Team Handbook", Renamed(to="Handbook 2026"))
    does(world, "dov", "Launch site", Renamed(to="Launch the site"))
    async with direct(world) as api:
        page = answer(await api.get(f"/v1/pages/{ids('handbook')}"))
        row = answer(await api.get(f"/v1/pages/{ids('launch')}"))
    assert page["properties"]["title"]["title"][0]["plain_text"] == "Handbook 2026"
    assert row["properties"]["Name"]["title"][0]["plain_text"] == "Launch the site"


def test_a_field_set_on_a_page_that_is_no_row_is_refused(world: World) -> None:
    with pytest.raises(ValueError, match="is a page, not a row"):
        does(world, "dov", "Team Handbook", FieldSet(field="Status", value="Done"))


def test_someone_outside_the_workspace_cannot_act(tmp_path: Path) -> None:
    notion = json.loads(json.dumps(NOTION))
    notion["workspaces"][0]["members"] = ["mara"]
    notion["workspaces"][0]["databases"] = []
    notion["workspaces"][0]["integrations"] = notion["workspaces"][0]["integrations"][:3]
    world = seeded(tmp_path, scenario(notion))
    happening = DocumentHappening(person="dov", document="Team Handbook", after=timedelta(hours=1), action=Trashed())
    with pytest.raises(ValueError, match="not a member"):
        world.provider.change(happening, scenario(notion), world.store, world.clock)


# --------------------------------------------------------------------------- the log


async def test_one_block_edit_is_one_event_on_its_page(api: httpx.AsyncClient, world: World) -> None:
    children = answer(await api.get(f"/v1/blocks/{ids('handbook')}/children"))["results"]
    toggle = children[5]
    inside = answer(await api.get(f"/v1/blocks/{toggle['id']}/children"))["results"][0]
    before = world.store.head()
    answer(
        await api.patch(
            f"/v1/blocks/{inside['id']}", json={"paragraph": {"rich_text": [{"text": {"content": "Shown"}}]}}
        )
    )
    [event] = world.store.events(since=before)
    assert event.entity.external_id == ids("handbook") and event.operation is Operation.UPDATE
    assert isinstance(event.after, DocumentSnapshot) and "  Shown" in (event.after.text or "")
    assert event.after.last_edited_by == "Planning bot"


async def test_a_mention_reads_as_the_name_it_points_at(api: httpx.AsyncClient) -> None:
    users = answer(await api.get("/v1/users"))["results"]
    mara = next(u["id"] for u in users if u["type"] == "person" and u["person"]["email"] == "mara@example.com")
    text = [
        {"type": "mention", "mention": {"type": "user", "user": {"id": mara}}},
        {"type": "text", "text": {"content": " see "}},
        {"type": "mention", "mention": {"type": "page", "page": {"id": ids("onboarding")}}},
        {"type": "equation", "equation": {"expression": "e=mc^2"}},
        {"type": "text", "text": {"content": " link", "link": {"url": "https://example.com"}}},
    ]
    made = answer(
        await api.patch(
            f"/v1/blocks/{ids('handbook')}/children", json={"children": [{"paragraph": {"rich_text": text}}]}
        )
    )["results"][0]
    items = made["paragraph"]["rich_text"]
    assert "".join(i["plain_text"] for i in items) == "@Mara Lindqvist see Onboardinge=mc^2 link"
    assert items[-1]["href"] == "https://example.com" and items[0]["annotations"]["bold"] is False
