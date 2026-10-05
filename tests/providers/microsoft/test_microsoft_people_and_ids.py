"""A person with no email, or one whose email the tenant hides, a person's account as the scenario declares it, and
every id a seed may declare (an object id, a principal name, a driveItem id, a channel's, a message's), driven
through Graph and the connector as a service reaches them, and refused at load when it is not Microsoft's format or
another thing of the tenant has it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from minutehand.adapters.proxy.registry import Registry
from minutehand.application.refusals import RunRefused, refuse_unheld
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import DocumentSnapshot, EntityKind, GrantSnapshot, MessageSnapshot
from tests.providers.microsoft.tenant import CONNECTOR, START, Intercepted, Tenant, bearer, seeded, token

GRAPH = "https://graph.microsoft.com/v1.0"
OBJECT_ID = "6e0c1f6a-1b5f-4c3a-9d2e-0a1b2c3d4e5f"
ITEM = "01ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
CHANNEL = "19:4a95f7d8db4c4e7fae857bcebe0623e6@thread.tacv2"

PEOPLE: list[dict[str, Any]] = [
    {"key": "owen", "name": "Owen Okafor", "email": "owen@example.com"},
    {"key": "svc", "name": "Build Service"},
    {
        "key": "hana",
        "name": "Hana Ito",
        "email": "hana@example.com",
        "accounts": [{"provider": "microsoft", "email_visible": False}],
    },
    {
        "key": "jo",
        "name": "Jo Smith",
        "email": "jo@example.com",
        "accounts": [
            {"provider": "microsoft", "id": OBJECT_ID, "login": "john.smith@contoso.com", "name": "John Smith"}
        ],
    },
    {"key": "dee", "name": "Dee Left", "email": "dee@example.com", "account": "deactivated"},
]


def _scenario(**more: Any) -> Scenario:
    return Scenario.model_validate(
        {
            "name": "people_ids",
            "goal": "g",
            "owner": "owen",
            "starts_at": START,
            "people": PEOPLE,
            "channels": [
                {
                    "provider": "microsoft",
                    "name": "launch",
                    "id": CHANNEL,
                    "private": True,
                    "purpose": "Shipping the launch",
                    "members": ["owen", "svc", "hana"],
                    "history": [{"by": "owen", "text": "kickoff", "ago": "PT1H", "id": "1757830000000"}],
                }
            ],
            "documents": [
                {
                    "provider": "microsoft",
                    "title": "Plan",
                    "text": "the plan",
                    "id": ITEM,
                    "owner": "jo",
                    "modified_by": "hana",
                    "modified_before_start": "P2D",
                    "shared_with": [{"person": "svc", "role": "reader"}],
                },
                {"provider": "microsoft", "title": "Budget", "text": "money", "space": "Finance"},
            ],
            "spaces": [
                {
                    "provider": "microsoft",
                    "name": "Finance",
                    "members": [{"person": "owen", "role": "organizer"}, {"person": "hana", "role": "reader"}],
                }
            ],
            **more,
        }
    )


@pytest.fixture
def world(tmp_path: Path) -> Tenant:
    return seeded(tmp_path / "world.db", _scenario())


@pytest.fixture
async def graph(world: Tenant, microsoft: Intercepted) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    async with microsoft.http() as http:
        yield http, bearer(await token(http, world, "https://graph.microsoft.com/.default"))


@pytest.fixture
def tenant(world: Tenant) -> Tenant:
    """`microsoft` from tenant.py mounts whichever tenant this fixture answers."""
    return world


async def test_a_person_with_no_email_is_a_user_with_a_principal_name_and_no_mail(
    world: Tenant, graph: tuple[httpx.AsyncClient, dict[str, str]]
) -> None:
    http, auth = graph
    svc = world.world.person("svc")
    assert svc is not None
    answered = (await http.get(f"{GRAPH}/users/{svc.user.id}", headers=auth)).json()
    assert answered["mail"] is None
    assert answered["userPrincipalName"] == f"svc@{world.directory.tenant_domain}"
    by_upn = await http.get(f"{GRAPH}/users/svc@{world.directory.tenant_domain}", headers=auth)
    assert by_upn.status_code == 200 and by_upn.json()["id"] == svc.user.id


async def test_a_hidden_email_is_null_in_graph_and_still_names_whom_a_message_reached(
    world: Tenant, graph: tuple[httpx.AsyncClient, dict[str, str]], microsoft: Intercepted
) -> None:
    http, auth = graph
    hana = world.world.person("hana")
    assert hana is not None
    assert (await http.get(f"{GRAPH}/users/{hana.user.id}", headers=auth)).json()["mail"] is None
    bot = bearer(await token(http, world, "https://api.botframework.com/.default"))
    sent = await http.post(
        f"{CONNECTOR}v3/conversations/{CHANNEL}/activities", json={"type": "message", "text": "status?"}, headers=bot
    )
    assert sent.status_code == 201, sent.text
    message = [e.after for e in world.store.events() if isinstance(e.after, MessageSnapshot)][-1]
    assert message.text == "status?"
    assert message.recipients == ["owen", "svc", "hana"]
    assert message.recipient_emails == ["owen@example.com", "hana@example.com"]


async def test_a_declared_account_is_the_users_object_id_principal_name_and_display_name(
    graph: tuple[httpx.AsyncClient, dict[str, str]],
) -> None:
    http, auth = graph
    answered = (await http.get(f"{GRAPH}/users/john.smith@contoso.com", headers=auth)).json()
    assert (answered["id"], answered["displayName"], answered["mail"]) == (OBJECT_ID, "John Smith", "jo@example.com")


async def test_a_deactivated_person_is_a_disabled_user(
    world: Tenant, graph: tuple[httpx.AsyncClient, dict[str, str]]
) -> None:
    http, auth = graph
    dee = world.world.person("dee")
    assert dee is not None
    assert (await http.get(f"{GRAPH}/users/{dee.user.id}", headers=auth)).json()["accountEnabled"] is False


async def test_a_declared_channel_is_private_with_its_id_description_and_message_id(
    world: Tenant, graph: tuple[httpx.AsyncClient, dict[str, str]]
) -> None:
    http, auth = graph
    channel = (await http.get(f"{GRAPH}/teams/{world.directory.team_id}/channels/{CHANNEL}", headers=auth)).json()
    assert (channel["id"], channel["membershipType"], channel["description"]) == (
        CHANNEL,
        "private",
        "Shipping the launch",
    )
    assert world.world.message("1757830000000") is not None


async def test_a_declared_document_has_its_id_owner_editor_age_and_sharing(
    world: Tenant, graph: tuple[httpx.AsyncClient, dict[str, str]]
) -> None:
    http, auth = graph
    site = (await http.get(f"{GRAPH}/sites/root", headers=auth)).json()
    item = (await http.get(f"{GRAPH}/sites/{site['id']}/drive/items/{ITEM}", headers=auth)).json()
    assert item["name"] == "Plan.docx"
    assert item["createdBy"]["user"]["id"] == OBJECT_ID
    assert item["lastModifiedBy"]["user"]["displayName"] == "Hana Ito"
    assert item["lastModifiedDateTime"].startswith("2026-09-12")
    shared = (await http.get(f"{GRAPH}/sites/{site['id']}/drive/items/{ITEM}/permissions", headers=auth)).json()
    svc = world.world.person("svc")
    assert svc is not None
    assert [p["grantedToV2"]["user"]["id"] for p in shared["value"]] == [svc.user.id]
    documents = [e.after for e in world.store.events() if isinstance(e.after, DocumentSnapshot)]
    plan = [d for d in documents if d.title == "Plan.docx"][-1]
    assert (plan.owned_by, plan.owner, plan.last_edited_by) == ("jo", "jo@example.com", "hana@example.com")
    grants = [e.after for e in world.store.events() if isinstance(e.after, GrantSnapshot)]
    assert any(g.document == "Plan.docx" and g.person == "svc" for g in grants)


async def test_a_shared_space_is_a_site_whose_library_holds_its_documents_and_its_members(
    world: Tenant, graph: tuple[httpx.AsyncClient, dict[str, str]]
) -> None:
    http, auth = graph
    found = (await http.get(f"{GRAPH}/sites", params={"search": "Finance"}, headers=auth)).json()["value"]
    assert [s["displayName"] for s in found] == ["Finance"]
    site = found[0]
    children = (await http.get(f"{GRAPH}/sites/{site['id']}/drive/root/children", headers=auth)).json()
    assert [c["name"] for c in children["value"]] == ["Budget.docx"]
    members = (await http.get(f"{GRAPH}/sites/{site['id']}/drive/root/permissions", headers=auth)).json()["value"]
    owen, hana = world.world.person("owen"), world.world.person("hana")
    assert owen is not None and hana is not None
    assert sorted((p["grantedToV2"]["user"]["id"], p["roles"][0]) for p in members) == sorted(
        [(owen.user.id, "owner"), (hana.user.id, "read")]
    )
    budget = [
        e.after
        for e in world.store.events()
        if e.entity.kind is EntityKind.DOCUMENT and isinstance(e.after, DocumentSnapshot)
        if e.after.title == "Budget.docx"
    ]
    assert budget and budget[-1].space == "Finance"


def _refused(tmp_path: Path, match: str, **more: Any) -> None:
    with pytest.raises(ValueError, match=match):
        seeded(tmp_path / "refused.db", _scenario(**more))


def test_an_object_id_that_is_no_guid_is_refused(tmp_path: Path) -> None:
    people = [*PEOPLE, {"key": "zed", "name": "Zed", "accounts": [{"provider": "microsoft", "id": "2-5"}]}]
    _refused(tmp_path, "is not a GUID", people=people)


def test_a_principal_name_that_is_no_alias_at_domain_is_refused(tmp_path: Path) -> None:
    people = [*PEOPLE, {"key": "zed", "name": "Zed", "accounts": [{"provider": "microsoft", "login": "zed"}]}]
    _refused(tmp_path, "is not alias@domain", people=people)


def test_an_object_id_another_user_has_is_refused(tmp_path: Path) -> None:
    owen_id = seeded(tmp_path / "plain.db", _scenario()).world.person("owen")
    assert owen_id is not None
    people = [*PEOPLE, {"key": "zed", "name": "Zed", "accounts": [{"provider": "microsoft", "id": owen_id.user.id}]}]
    _refused(tmp_path, "object id or principal name", people=people)


def test_a_document_id_that_is_no_drive_item_id_is_refused(tmp_path: Path) -> None:
    documents = [{"provider": "microsoft", "title": "Odd", "text": "x", "id": "not-an-item"}]
    _refused(tmp_path, "no driveItem id", documents=documents)


def test_a_channel_id_that_is_no_teams_channel_id_is_refused(tmp_path: Path) -> None:
    channels = [{"provider": "microsoft", "name": "ops", "id": "C0123ABCD"}]
    _refused(tmp_path, "not a Teams channel id", channels=channels)


def test_a_message_id_minted_after_the_start_is_refused(tmp_path: Path) -> None:
    late = str(int(START.timestamp()) * 1_000_000 + 5)
    channels = [
        {"provider": "microsoft", "name": "ops", "history": [{"by": "owen", "text": "t", "ago": "PT1M", "id": late}]}
    ]
    _refused(tmp_path, "mints for a message after the start", channels=channels)


def test_a_site_id_on_another_host_is_refused(tmp_path: Path) -> None:
    spaces = [
        {
            "provider": "microsoft",
            "name": "Finance",
            "id": f"else.sharepoint.com,{OBJECT_ID},{OBJECT_ID}",
            "members": [{"person": "owen"}],
        }
    ]
    _refused(tmp_path, "declares a site on else.sharepoint.com", spaces=spaces)


def test_a_spreadsheet_or_an_archived_channel_on_microsoft_is_refused() -> None:
    manifests = {m.key: m for m in Registry.installed().manifests}
    sheet = _scenario(documents=[{"provider": "microsoft", "title": "S", "kind": "spreadsheet", "rows": [["a"]]}])
    with pytest.raises(RunRefused, match="sets spreadsheet"):
        refuse_unheld(sheet, manifests)
    archived = _scenario(channels=[{"provider": "microsoft", "name": "old", "archived": True}])
    with pytest.raises(RunRefused, match="sets archived"):
        refuse_unheld(archived, manifests)
    refuse_unheld(_scenario(), manifests)
