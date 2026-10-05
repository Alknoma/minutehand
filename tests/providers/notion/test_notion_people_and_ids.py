"""People without an email or with a hidden one, a person's own Notion account (its user id and name), the ids a
seed declares for its documents, and what the Notion seed cannot hold, refused at load. Driven through the real
proxy with the official `notion-client` SDK, sync and async, as `test_notion_sdk.py` is."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from minutehand.adapters.providers.notion import wire
from minutehand.adapters.providers.notion.manifest import MANIFEST
from minutehand.adapters.providers.notion.seed import object_id, user_id
from minutehand.application.refusals import RunRefused, refuse_unheld
from minutehand.domain.provider import PersonChange
from minutehand.domain.scenario import (
    Access,
    DocumentHappening,
    Edited,
    Person,
    PersonAccount,
    ProviderSeed,
    Scenario,
    SeededDocument,
    SharedSpace,
)
from minutehand.domain.world import DocumentSnapshot, Operation
from tests.providers.notion.notion_world import AGENT_TOKEN, START, Sdk, seeded, through_proxy

DECLARED_USER = "5f1a2b3c4d5e4f60a1b2c3d4e5f60718"
DECLARED_PAGE = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"

MARA = Person(key="mara", name="Mara Lindqvist", email="mara@example.com")
SERVICE = Person(key="robot", name="Release robot")
PRIVATE = Person(
    key="ines",
    name="Ines Duarte",
    email="ines@example.com",
    accounts=[PersonAccount(provider="notion", id=DECLARED_USER, name="Ines D.", email_visible=False)],
)
SEED = {
    "workspaces": [
        {
            "key": "acme",
            "name": "Acme",
            "integrations": [{"key": "agent", "name": "Planning bot", "tokens": [AGENT_TOKEN], "shared": []}],
        }
    ]
}


def played(*, documents: list[SeededDocument] | None = None, people: list[Person] | None = None) -> Scenario:
    return Scenario(
        name="people_and_ids",
        goal="The notes are current.",
        owner="mara",
        starts_at=START,
        people=people or [MARA, SERVICE, PRIVATE],
        documents=documents or [],
        provider_seeds=[ProviderSeed(provider="notion", body=json.dumps(SEED))],
    )


async def _sdk(tmp_path: Path, scenario: Scenario, flavour: str) -> AsyncIterator[Sdk]:
    world = seeded(tmp_path, scenario)
    try:
        async for found in through_proxy(tmp_path, world, flavour):
            yield found
    finally:
        world.store.close()


@pytest.fixture(params=["sync", "async"])
async def people_sdk(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Sdk]:
    async for found in _sdk(tmp_path, played(), str(request.param)):
        yield found


def _person(users: list[Any], name: str) -> dict[str, Any]:
    found = next(u for u in users if u["name"] == name)
    assert isinstance(found, dict)
    return found


async def test_a_person_with_no_email_is_a_member_whose_person_object_holds_none(people_sdk: Sdk) -> None:
    listed = (await people_sdk(lambda c: c.users.list()))["results"]
    assert _person(listed, "Release robot")["person"] == {}
    assert _person(listed, "Mara Lindqvist")["person"] == {"email": "mara@example.com"}


async def test_an_account_that_hides_its_email_reads_without_one_under_its_own_id_and_name(people_sdk: Sdk) -> None:
    listed = (await people_sdk(lambda c: c.users.list()))["results"]
    ines = _person(listed, "Ines D.")
    assert ines["person"] == {}
    assert ines["id"] == "5f1a2b3c-4d5e-4f60-a1b2-c3d4e5f60718"
    one = await people_sdk(lambda c: c.users.retrieve("5f1a2b3c-4d5e-4f60-a1b2-c3d4e5f60718"))
    assert one == ines


@pytest.fixture(params=["sync", "async"])
async def document_sdk(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Sdk]:
    documents = [
        SeededDocument(provider="notion", title="Release notes", text="v1 shipped", id=DECLARED_PAGE.replace("-", "")),
        SeededDocument(
            provider="notion",
            title="Runbook",
            text="Restart the job.",
            owner="robot",
            modified_by="ines",
            modified_before_start=timedelta(days=2),
        ),
    ]
    async for found in _sdk(tmp_path, played(documents=documents), str(request.param)):
        yield found


async def test_a_declared_page_id_is_the_id_the_api_serves_the_document_under(document_sdk: Sdk) -> None:
    page = await document_sdk(lambda c: c.pages.retrieve(DECLARED_PAGE))
    assert page["id"] == DECLARED_PAGE
    title = page["properties"]["title"]["title"]
    assert "".join(t["plain_text"] for t in title) == "Release notes"


async def test_a_seeded_documents_owner_last_editor_and_age_are_what_the_api_serves(document_sdk: Sdk) -> None:
    found = await document_sdk(lambda c: c.search(query="Runbook"))
    page = found["results"][0]
    workspace = object_id("acme", "acme")
    assert page["created_by"]["id"] == user_id(workspace, SERVICE)
    assert page["last_edited_by"]["id"] == "5f1a2b3c-4d5e-4f60-a1b2-c3d4e5f60718"
    assert page["created_time"] == page["last_edited_time"] == wire.stamp(START - timedelta(days=2))


def test_a_document_a_person_without_email_owns_names_them_by_key_in_the_record(tmp_path: Path) -> None:
    documents = [SeededDocument(provider="notion", title="Runbook", text="Restart.", owner="robot")]
    world = seeded(tmp_path, played(documents=documents))
    try:
        written = [
            e.after
            for e in world.store.events()
            if isinstance(e.after, DocumentSnapshot) and e.after.title == "Runbook"
        ]
        assert written and written[-1].owned_by == "robot"
        assert written[-1].owner == "Release robot"
        scenario = played(documents=documents)
        edited = DocumentHappening(
            person="robot", document="Runbook", after=timedelta(hours=1), action=Edited(append="Then check it.")
        )
        world.provider.change(edited, scenario, world.store, world.clock)
        last = world.store.events()[-1]
        assert last.operation is Operation.UPDATE
        assert isinstance(last.after, DocumentSnapshot) and last.after.last_edited_by == "Release robot"
    finally:
        world.store.close()


def test_removing_a_person_with_no_email_removes_their_member(tmp_path: Path) -> None:
    world = seeded(tmp_path, played())
    try:
        world.provider.change_person(PersonChange.REMOVED, SERVICE, world.store, world.clock)
        from minutehand.adapters.providers.notion.state import NotionWorld

        notion = NotionWorld(world.store)
        names = [u.name for u in notion.users(object_id("acme", "acme"))]
        assert "Release robot" not in names and "Mara Lindqvist" in names
    finally:
        world.store.close()


def test_a_declared_notion_id_that_is_no_uuid_is_refused(tmp_path: Path) -> None:
    documents = [SeededDocument(provider="notion", title="Release notes", id="release-notes")]
    with pytest.raises(ValueError, match="no UUID"):
        seeded(tmp_path, played(documents=documents))


def test_a_declared_page_id_a_derived_one_already_is_is_refused(tmp_path: Path) -> None:
    taken = user_id(object_id("acme", "acme"), MARA)
    documents = [SeededDocument(provider="notion", title="Release notes", id=taken)]
    with pytest.raises(ValueError, match="would both have the Notion id"):
        seeded(tmp_path, played(documents=documents))


def test_the_same_page_id_declared_with_and_without_dashes_is_refused(tmp_path: Path) -> None:
    documents = [
        SeededDocument(provider="notion", title="Release notes", id=DECLARED_PAGE),
        SeededDocument(provider="notion", title="Runbook", id=DECLARED_PAGE.replace("-", "")),
    ]
    with pytest.raises(ValueError, match="would both have the Notion id"):
        seeded(tmp_path, played(documents=documents))


def _refused(scenario: Scenario) -> str:
    with pytest.raises(RunRefused) as refused:
        refuse_unheld(scenario, {MANIFEST.key: MANIFEST})
    return str(refused.value)


def test_a_login_on_a_notion_account_is_refused() -> None:
    named = Person(key="john", name="John", accounts=[PersonAccount(provider="notion", login="john.smith")])
    assert "login" in _refused(played(people=[MARA, named]))


def test_a_shared_space_on_notion_is_refused() -> None:
    scenario = played().model_copy(
        update={"spaces": [SharedSpace(provider="notion", name="Team", members=[Access(person="mara")])]}
    )
    assert "holds no shared spaces" in _refused(scenario)


def test_a_notion_document_shared_with_a_person_is_refused() -> None:
    documents = [SeededDocument(provider="notion", title="Runbook", shared_with=[Access(person="robot")])]
    assert "shared_with" in _refused(played(documents=documents))
