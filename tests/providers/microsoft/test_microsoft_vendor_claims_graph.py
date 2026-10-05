"""Vendor claims about Microsoft Graph's drive items, carried over from an older emulator's own tests and checked
against Microsoft's documentation. `CLAIMS.md` beside the provider lists each one with its source."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest

from tests.providers.microsoft.tenant import GRAPH, Intercepted, Tenant, bearer, token

DOCS = "https://learn.microsoft.com/en-us/graph/api"
LIST_CHILDREN = f"{DOCS}/driveitem-list-children"
GET_ITEM = f"{DOCS}/driveitem-get"
GET_CONTENT = f"{DOCS}/driveitem-get-content"
CREATE_FOLDER = f"{DOCS}/driveitem-post-children"
PUT_CONTENT = f"{DOCS}/driveitem-put-content"
MOVE = f"{DOCS}/driveitem-move"
DELETE = f"{DOCS}/driveitem-delete"
SEARCH = f"{DOCS}/driveitem-search"
INVITE = f"{DOCS}/driveitem-invite"
ERRORS = "https://learn.microsoft.com/en-us/graph/errors"


@dataclass
class Drive:
    http: httpx.AsyncClient
    auth: dict[str, str]
    id: str

    def url(self, rest: str) -> str:
        return f"{GRAPH}/drives/{self.id}{rest}"

    async def upload(self, name: str, content: bytes) -> dict[str, object]:
        made = await self.http.put(self.url(f"/root:/{name}:/content"), content=content, headers=self.auth)
        assert made.status_code == 201, made.text
        return made.json()

    async def download(self, item: object) -> httpx.Response:
        return await self.http.get(self.url(f"/items/{item}/content"), headers=self.auth, follow_redirects=True)


@pytest.fixture
async def drive(tenant: Tenant, microsoft: Intercepted) -> AsyncIterator[Drive]:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        site = (
            await http.get(f"{GRAPH}/sites/{tenant.directory.sharepoint_host}:/sites/VendorReviewTeam", headers=auth)
        ).json()
        found = (await http.get(f"{GRAPH}/sites/{site['id']}/drive", headers=auth)).json()
        yield Drive(http=http, auth=auth, id=found["id"])


async def test_a_folders_children_are_listed_under_value(drive: Drive) -> None:
    """Documented: listing a folder's children answers 200 with the items in `value`. Class (a), LIST_CHILDREN."""
    answered = await drive.http.get(drive.url("/items/root/children"), headers=drive.auth)
    assert answered.status_code == 200
    assert {"Plans", "notes.txt"} <= {child["name"] for child in answered.json()["value"]}


async def test_a_file_is_read_by_id_with_its_file_facet(drive: Drive) -> None:
    """Documented: getting an item by id answers it, and a file carries the `file` facet. Class (a), GET_ITEM."""
    made = await drive.upload("brief.txt", b"Venue shortlist")
    answered = await drive.http.get(drive.url(f"/items/{made['id']}"), headers=drive.auth)
    assert answered.status_code == 200
    assert answered.json()["name"] == "brief.txt" and "file" in answered.json()


async def test_an_item_that_does_not_exist_is_refused_404_item_not_found(drive: Drive) -> None:
    """Documented: an item that does not exist is 404 `itemNotFound` in Graph's error shape. Class (a), ERRORS."""
    answered = await drive.http.get(drive.url("/items/01NOSUCHITEM"), headers=drive.auth)
    assert answered.status_code == 404
    assert answered.json()["error"]["code"] == "itemNotFound"


async def test_a_folder_is_created_under_a_parent_201_with_its_folder_facet(drive: Drive) -> None:
    """Documented: POST to a folder's children with a `folder` facet creates the folder, 201. Class (a),
    CREATE_FOLDER."""
    answered = await drive.http.post(
        drive.url("/items/root/children"), json={"name": "Quotes", "folder": {}}, headers=drive.auth
    )
    assert answered.status_code == 201
    assert answered.json()["name"] == "Quotes" and "folder" in answered.json()


async def test_an_upload_by_path_creates_the_file_201_with_its_size(drive: Drive) -> None:
    """Documented: PUT to `root:/name:/content` creates a new file, 201, and the item reports its size in bytes.
    Class (a), PUT_CONTENT."""
    content = b"Quote from Harbour Print: 1,240 units."
    made = await drive.upload("harbour-quote.txt", content)
    assert made["name"] == "harbour-quote.txt" and made["size"] == len(content)
    downloaded = await drive.download(made["id"])
    assert downloaded.status_code == 200 and downloaded.content == content


async def test_downloading_content_redirects_302_to_a_preauthenticated_url(drive: Drive) -> None:
    """Documented: GET on an item's `content` answers 302 with a Location the caller follows without the bearer
    token. Class (a), GET_CONTENT. The old emulator answered 200 with the bytes: contradicted, see `CLAIMS.md`."""
    made = await drive.upload("terms.txt", b"Net 30")
    answered = await drive.http.get(drive.url(f"/items/{made['id']}/content"), headers=drive.auth)
    assert answered.status_code == 302
    followed = await drive.http.get(answered.headers["location"])
    assert followed.status_code == 200 and followed.content == b"Net 30"


async def test_putting_content_on_an_item_id_replaces_it_200(drive: Drive) -> None:
    """Documented: PUT on an existing item's `content` replaces its bytes and answers 200. Class (a), PUT_CONTENT."""
    made = await drive.upload("draft.txt", b"first cut")
    answered = await drive.http.put(
        drive.url(f"/items/{made['id']}/content"), content=b"second cut", headers=drive.auth
    )
    assert answered.status_code == 200 and answered.json()["id"] == made["id"]
    assert (await drive.download(made["id"])).content == b"second cut"


async def test_patching_parent_reference_moves_the_item_200(drive: Drive) -> None:
    """Documented: PATCH with a new `parentReference.id` moves the item and answers it, 200. Class (a), MOVE."""
    made = await drive.upload("loose.txt", b"to file away")
    folder = (
        await drive.http.post(
            drive.url("/items/root/children"), json={"name": "Filed", "folder": {}}, headers=drive.auth
        )
    ).json()
    moved = await drive.http.patch(
        drive.url(f"/items/{made['id']}"), json={"parentReference": {"id": folder["id"]}}, headers=drive.auth
    )
    assert moved.status_code == 200
    again = await drive.http.get(drive.url(f"/items/{made['id']}"), headers=drive.auth)
    assert again.json()["parentReference"]["id"] == folder["id"]


async def test_a_deleted_item_answers_204_and_is_then_not_found(drive: Drive) -> None:
    """Documented: DELETE answers 204 No Content, and the item is gone afterwards. Class (a), DELETE and ERRORS."""
    made = await drive.upload("scratch.txt", b"throwaway")
    assert (await drive.http.delete(drive.url(f"/items/{made['id']}"), headers=drive.auth)).status_code == 204
    assert (await drive.http.get(drive.url(f"/items/{made['id']}"), headers=drive.auth)).status_code == 404


async def test_search_from_the_root_finds_items_by_name_and_answers_empty_when_nothing_matches(drive: Drive) -> None:
    """Documented: `root/search(q='…')` answers 200 with the matching items in `value`, an empty list when none match.
    Class (a), SEARCH."""
    await drive.upload("Lighthouse budget.txt", b"figures")
    found = await drive.http.get(drive.url("/root/search(q='Lighthouse')"), headers=drive.auth)
    assert found.status_code == 200
    assert [i["name"] for i in found.json()["value"]] == ["Lighthouse budget.txt"]
    none = await drive.http.get(drive.url("/root/search(q='zzqxnothing')"), headers=drive.auth)
    assert none.status_code == 200 and none.json()["value"] == []


async def test_search_from_a_folder_finds_only_what_lies_beneath_it(drive: Drive) -> None:
    """Documented: search on a folder item searches that folder's hierarchy. Class (a), SEARCH."""
    await drive.http.put(drive.url("/root:/Inside/ferry-schedule.txt:/content"), content=b"in", headers=drive.auth)
    await drive.upload("ferry-notes.txt", b"out")
    folder = (await drive.http.get(drive.url("/root:/Inside"), headers=drive.auth)).json()
    found = await drive.http.get(drive.url(f"/items/{folder['id']}/search(q='ferry')"), headers=drive.auth)
    assert [i["name"] for i in found.json()["value"]] == ["ferry-schedule.txt"]


async def test_an_invite_answers_the_permissions_it_granted(drive: Drive) -> None:
    """Documented: POST `invite` answers 200 with one permission per recipient in `value`, carrying the roles granted.
    Class (a), INVITE."""
    made = await drive.upload("contract.txt", b"terms")
    answered = await drive.http.post(
        drive.url(f"/items/{made['id']}/invite"),
        json={
            "recipients": [{"email": "dania@example.com"}],
            "roles": ["write"],
            "requireSignIn": True,
            "sendInvitation": False,
        },
        headers=drive.auth,
    )
    assert answered.status_code == 200
    granted = answered.json()["value"]
    assert len(granted) == 1 and granted[0]["roles"] == ["write"]
