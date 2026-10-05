"""Graph for files call by call, with each refusal Graph makes in its own shape, the faults a scenario declares,
and subscriptions on the run's clock."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.microsoft import docx as word
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, Operation
from tests.providers.microsoft.tenant import GRAPH, SCENARIO, Intercepted, Tenant, bearer, seeded, token


@dataclass
class Files:
    http: httpx.AsyncClient
    auth: dict[str, str]
    tenant: Tenant
    drive: str

    def url(self, rest: str) -> str:
        return f"{GRAPH}/drives/{self.drive}{rest}"


@pytest.fixture
async def files(tenant: Tenant, microsoft: Intercepted) -> AsyncIterator[Files]:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        site = (
            await http.get(f"{GRAPH}/sites/{tenant.directory.sharepoint_host}:/sites/VendorReviewTeam", headers=auth)
        ).json()
        answered = await http.get(f"{GRAPH}/sites/{site['id']}/drive", headers=auth)
        assert answered.status_code == 200, (site, answered.text)
        drive = answered.json()["id"]
        yield Files(http=http, auth=auth, tenant=tenant, drive=drive)


def code(answered: httpx.Response, status: int) -> str:
    assert answered.status_code == status, answered.text
    return str(answered.json()["error"]["code"])


async def test_put_by_path_makes_the_folders_and_honours_conflict_behaviour(files: Files) -> None:
    made = await files.http.put(
        files.url("/root:/Reports/2026/summary.txt:/content"), content=b"one", headers=files.auth
    )
    assert made.status_code == 201
    assert made.json()["parentReference"]["path"].endswith("/root:/Reports/2026")
    replaced = await files.http.put(
        files.url("/root:/Reports/2026/summary.txt:/content"), content=b"two", headers=files.auth
    )
    assert replaced.status_code == 200 and replaced.json()["id"] == made.json()["id"]
    renamed = await files.http.put(
        files.url("/root:/Reports/2026/summary.txt:/content"),
        params={"@microsoft.graph.conflictBehavior": "rename"},
        content=b"three",
        headers=files.auth,
    )
    assert renamed.json()["name"] == "summary 1.txt"
    parent = made.json()["parentReference"]["id"]
    refused = await files.http.put(
        files.url(f"/items/{parent}:/summary.txt:/content"),
        params={"@microsoft.graph.conflictBehavior": "fail"},
        content=b"four",
        headers=files.auth,
    )
    assert code(refused, 409) == "nameAlreadyExists"


async def test_folders_move_copy_share_and_link(files: Files) -> None:
    folder = await files.http.post(
        files.url("/items/root/children"),
        json={"name": "Archive", "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        headers=files.auth,
    )
    assert folder.status_code == 201
    again = await files.http.post(
        files.url("/items/root/children"),
        json={"name": "Archive", "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        headers=files.auth,
    )
    assert code(again, 409) == "nameAlreadyExists"
    notes = (await files.http.get(files.url("/root:/notes.txt:"), headers=files.auth)).json()
    moved = await files.http.patch(
        files.url(f"/items/{notes['id']}"), json={"parentReference": {"id": folder.json()["id"]}}, headers=files.auth
    )
    assert moved.status_code == 200 and moved.json()["webUrl"]
    assert (await files.http.get(files.url("/root:/Archive/notes.txt:"), headers=files.auth)).status_code == 200
    copied = await files.http.post(
        files.url(f"/items/{notes['id']}/copy"), json={"name": "notes copy.txt"}, headers=files.auth
    )
    assert copied.status_code == 202
    status = (await files.http.get(copied.headers["location"])).json()
    assert status["status"] == "completed"
    assert (await files.http.get(files.url(f"/items/{status['resourceId']}"), headers=files.auth)).json()[
        "name"
    ] == "notes copy.txt"
    invited = await files.http.post(
        files.url(f"/items/{notes['id']}/invite"),
        json={
            "recipients": [{"email": "dania@example.com"}],
            "requireSignIn": True,
            "sendInvitation": False,
            "roles": ["read"],
        },
        headers=files.auth,
    )
    assert invited.status_code == 200 and invited.json()["value"][0]["roles"] == ["read"]
    link = await files.http.post(
        files.url(f"/items/{notes['id']}/createLink"),
        json={"type": "view", "scope": "organization"},
        headers=files.auth,
    )
    assert link.status_code == 201 and link.json()["link"]["type"] == "view"
    listed = (await files.http.get(files.url(f"/items/{notes['id']}/permissions"), headers=files.auth)).json()
    assert len(listed["value"]) == 2


async def test_search_reads_a_word_files_text_and_children_page(files: Files) -> None:
    found = await files.http.get(files.url("/root/search(q='vendors remain')"), headers=files.auth)
    assert [i["name"] for i in found.json()["value"]] == ["Vendor Plan.docx"]
    for n in range(3):
        await files.http.put(files.url(f"/root:/Bulk/f{n}.txt:/content"), content=b"x", headers=files.auth)
    bulk = (await files.http.get(files.url("/root:/Bulk:"), headers=files.auth)).json()
    first = (
        await files.http.get(files.url(f"/items/{bulk['id']}/children"), params={"$top": "2"}, headers=files.auth)
    ).json()
    assert len(first["value"]) == 2
    rest = (await files.http.get(first["@odata.nextLink"], headers=files.auth)).json()
    assert [i["name"] for i in rest["value"]] == ["f2.txt"] and "@odata.nextLink" not in rest


async def test_a_file_a_person_holds_open_refuses_writes_with_423(files: Files) -> None:
    plan = (await files.http.get(files.url("/root:/Plans/Vendor Plan.docx:"), headers=files.auth)).json()
    held = files.tenant.provider.hold_file("sofia", plan["id"], files.tenant.store, files.tenant.clock, held=True)
    assert (held.actor, held.operation) == (Actor.PERSON, Operation.UPDATE)
    refused = await files.http.put(
        files.url(f"/items/{plan['id']}/content"), content=word.build("mine"), headers=files.auth
    )
    assert code(refused, 423) == "resourceLocked"
    files.tenant.provider.hold_file("sofia", plan["id"], files.tenant.store, files.tenant.clock, held=False)
    assert (
        await files.http.patch(files.url(f"/items/{plan['id']}"), json={"name": "Plan.docx"}, headers=files.auth)
    ).status_code == 200


async def test_an_old_delta_token_is_refused_410_resync_required(files: Files) -> None:
    link = (await files.http.get(files.url("/root/delta"), headers=files.auth)).json()["@odata.deltaLink"]
    files.tenant.clock.jump(files.tenant.clock.now() + timedelta(days=31))
    assert code(await files.http.get(link, headers=files.auth), 410) == "resyncRequired"


async def test_another_users_onedrive_is_refused_to_a_user(files: Files, microsoft: Intercepted) -> None:
    from urllib.parse import parse_qs, urlsplit

    d = files.tenant.directory
    signed = await files.http.get(
        f"https://login.microsoftonline.com/{d.tenant_id}/oauth2/v2.0/authorize",
        params={
            "client_id": d.bot_app_id,
            "redirect_uri": "https://a.example.com/cb",
            "login_hint": "owen@example.com",
        },
    )
    code_ = parse_qs(urlsplit(signed.headers["location"]).query)["code"][0]
    owen = (
        await files.http.post(
            f"https://login.microsoftonline.com/{d.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "authorization_code",
                "client_id": d.bot_app_id,
                "client_secret": d.bot_app_secret,
                "code": code_,
                "redirect_uri": "https://a.example.com/cb",
            },
        )
    ).json()["access_token"]
    assert (
        code(await files.http.get(f"{GRAPH}/users/sofia@example.com/drive/root/children", headers=bearer(owen)), 403)
        == "accessDenied"
    )
    assert (await files.http.get(f"{GRAPH}/me/drive/root/children", headers=bearer(owen))).status_code == 200


async def test_a_missing_item_and_an_unknown_segment_are_refused(files: Files) -> None:
    assert code(await files.http.get(files.url("/items/01NOPE"), headers=files.auth), 404) == "itemNotFound"
    assert code(await files.http.get(files.url("/items/root/thumbnails"), headers=files.auth), 400) == "invalidRequest"


async def test_declared_faults_answer_in_each_surfaces_shape_then_stop(tmp_path: Path) -> None:
    import ssl

    from minutehand.adapters.proxy.policy import Routing
    from minutehand.adapters.proxy.registry import Registry
    from minutehand.adapters.proxy.server import Proxy

    faulted = Scenario.model_validate(
        {
            **SCENARIO.model_dump(),
            "provider_seeds": [
                {
                    "provider": "microsoft",
                    "body": {
                        "faults": [
                            {
                                "call": "GET /v1.0/drives",
                                "answer": {"kind": "rate_limited", "retry_after": "PT7S"},
                            },
                            {
                                "call": "/v1.0/sites",
                                "answer": {"kind": "refused", "error": "serviceNotAvailable"},
                            },
                            {
                                "call": "POST /teams/v3/conversations",
                                "answer": {"kind": "rate_limited"},
                                "times": 2,
                            },
                        ]
                    },
                }
            ],
        }
    )
    tenant = seeded(tmp_path / "world.db", faulted)
    async with Proxy(Routing(Registry.installed()), tenant.store, tenant.clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(tenant.store, tenant.clock, {"microsoft": tenant.provider.app(tenant.store, tenant.clock)})
        trust = ssl.create_default_context(cafile=str(proxy.ca_bundle))
        async with httpx.AsyncClient(proxy=proxy.url, verify=trust, trust_env=False) as http:
            auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
            drive = tenant.world.drives()[0].drive.id
            limited = await http.get(f"{GRAPH}/drives/{drive}", headers=auth)
            assert code(limited, 429) == "TooManyRequests" and limited.headers["retry-after"] == "7"
            assert (await http.get(f"{GRAPH}/drives/{drive}", headers=auth)).status_code == 200
            assert code(await http.get(f"{GRAPH}/sites?search=*", headers=auth), 503) == "serviceNotAvailable"
            bot = bearer(await token(http, tenant, "https://api.botframework.com/.default"))
            url = f"https://smba.trafficmanager.net/teams/v3/conversations/{tenant.directory.general_channel_id}/activities"
            answers = [
                (await http.post(url, json={"type": "message", "text": "x"}, headers=bot)).status_code for _ in range(3)
            ]
            assert answers == [429, 429, 201]
