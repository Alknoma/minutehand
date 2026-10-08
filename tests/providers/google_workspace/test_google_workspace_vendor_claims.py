"""Facts about real Drive v3 that an older stand-in for Drive was built to keep, each held here against this fake.

Every test names its class in its docstring: DOCUMENTED (Google's public reference says so; the page is cited) or
OBSERVED (no page says so; the older stand-in asserted it from someone's sighting of the real service).
`src/minutehand/adapters/providers/google_workspace/CLAIMS.md` is the index."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.google_workspace import wire
from minutehand.adapters.providers.google_workspace.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Access, AccessRole, SeededDocument, SharedSpace
from tests.providers.google_workspace.drive_world import (
    AUTH,
    DOC,
    FOLDER,
    OWNER,
    SCENARIO,
    START,
    TOKEN,
    answer,
    client_for,
    create_doc,
    files_of,
    issue,
    listed,
    multipart,
    names_of,
    reason_of,
)
from tests.providers.google_workspace.test_docs_batch_update import content, get, text_of

SHEET = "application/vnd.google-apps.spreadsheet"


@dataclass
class Spaced:
    api: httpx.AsyncClient
    drive_id: str


@pytest.fixture
async def spaced(tmp_path: Path) -> AsyncIterator[Spaced]:
    """The owner organises a shared drive, "Harbour Works", holding one document."""
    scenario = SCENARIO.model_copy(
        update={
            "spaces": [
                SharedSpace(
                    provider="google_workspace",
                    name="Harbour Works",
                    members=[Access(person="mara", role=AccessRole.ORGANIZER)],
                )
            ],
            "documents": [
                SeededDocument(
                    provider="google_workspace", title="Berth Schedule", text="Pier 4 opens.", space="Harbour Works"
                )
            ],
        }
    )
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "spaced.db", "root", clock)
    provider = build()
    provider.seed(scenario, store)
    issue(store, TOKEN, OWNER)
    async with client_for(provider, store, clock) as api:
        drives = answer(await api.get("/drive/v3/drives", headers=AUTH))["drives"]
        assert isinstance(drives, list)
        yield Spaced(api=api, drive_id=str(drives[0]["id"]))


async def _shared_folder(spaced: Spaced) -> str:
    made = answer(
        await spaced.api.post(
            "/drive/v3/files",
            params={"supportsAllDrives": "true"},
            json={"name": "Tide Tables", "mimeType": FOLDER, "parents": [spaced.drive_id]},
            headers=AUTH,
        )
    )
    return str(made["id"])


async def test_a_shared_drive_file_is_not_found_without_supports_all_drives_is_refused_404(spaced: Spaced) -> None:
    """DOCUMENTED: an app that does not say it supports shared drives is answered as if their items did not exist.
    https://developers.google.com/workspace/drive/api/guides/enable-shareddrives"""
    folder = await _shared_folder(spaced)

    without = await spaced.api.get(f"/drive/v3/files/{folder}", headers=AUTH)
    flagged = await spaced.api.get(f"/drive/v3/files/{folder}", params={"supportsAllDrives": "true"}, headers=AUTH)

    assert reason_of(without, 404) == "notFound"
    assert answer(flagged)["id"] == folder


async def test_creating_in_a_shared_drive_folder_without_supports_all_drives_is_refused_404(spaced: Spaced) -> None:
    """DOCUMENTED: the parent sits in a shared drive, so without the flag it does not exist for the caller.
    https://developers.google.com/workspace/drive/api/guides/enable-shareddrives"""
    folder = await _shared_folder(spaced)
    body = {"name": "Crane Rota", "mimeType": DOC, "parents": [folder]}

    refused = await spaced.api.post("/drive/v3/files", json=body, headers=AUTH)
    accepted = await spaced.api.post("/drive/v3/files", params={"supportsAllDrives": "true"}, json=body, headers=AUTH)

    assert reason_of(refused, 404) == "notFound"
    assert answer(accepted)["name"] == "Crane Rota"


async def test_a_listing_shows_shared_drive_files_only_with_both_flags(spaced: Spaced) -> None:
    """DOCUMENTED: `includeItemsFromAllDrives` and `supportsAllDrives` together bring shared-drive items into a
    listing; with neither, a query on a shared folder's children answers nothing.
    https://developers.google.com/workspace/drive/api/guides/enable-shareddrives"""
    folder = await _shared_folder(spaced)
    await spaced.api.post(
        "/drive/v3/files",
        params={"supportsAllDrives": "true"},
        json={"name": "Crane Rota", "mimeType": DOC, "parents": [folder]},
        headers=AUTH,
    )
    q = f"'{folder}' in parents"

    hidden = await listed(spaced.api, q)
    shown = await listed(spaced.api, q, supportsAllDrives="true", includeItemsFromAllDrives="true")

    assert files_of(hidden) == []
    assert names_of(shown) == ["Crane Rota"]


async def test_a_create_in_a_shared_drive_needs_no_drive_id_in_its_body(spaced: Spaced) -> None:
    """DOCUMENTED: `driveId` is output-only; the parent and the flag place the file, and the answer carries the drive.
    https://developers.google.com/workspace/drive/api/reference/rest/v3/files"""
    folder = await _shared_folder(spaced)

    made = answer(
        await spaced.api.post(
            "/drive/v3/files",
            params={"supportsAllDrives": "true", "fields": "id,driveId"},
            json={"name": "Crane Rota", "mimeType": DOC, "parents": [folder]},
            headers=AUTH,
        )
    )

    assert made["driveId"] == spaced.drive_id


async def test_a_page_of_a_thousand_is_answered_whole_with_a_token_for_the_rest(tmp_path: Path) -> None:
    """DOCUMENTED: `pageSize` reaches 1000, and a folder past one page says so with `nextPageToken`.
    https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list"""
    many = [SeededDocument(provider="google_workspace", title=f"Manifest {n:04d}", folder="Cargo") for n in range(1003)]
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "many.db", "root", clock)
    provider = build()
    provider.seed(SCENARIO.model_copy(update={"documents": many}), store)
    issue(store, TOKEN, OWNER)
    async with client_for(provider, store, clock) as api:
        [cargo] = files_of(await listed(api, f"name = 'Cargo' and mimeType = '{FOLDER}'"))
        q = f"'{cargo['id']}' in parents"
        first = await listed(api, q, pageSize="1000")
        rest = await listed(api, q, pageSize="1000", pageToken=str(first["nextPageToken"]))

    assert len(files_of(first)) == 1000
    assert len(files_of(rest)) == 3 and "nextPageToken" not in rest
    assert {f["id"] for f in files_of(first)}.isdisjoint({f["id"] for f in files_of(rest)})


async def test_a_listing_answers_only_the_file_fields_asked_for(api: httpx.AsyncClient) -> None:
    """DOCUMENTED: a field mask on `files.list` narrows each listed file to the fields named.
    https://developers.google.com/workspace/drive/api/guides/fields-parameter"""
    await create_doc(api, "Dredging Notes", "depth readings")

    page = answer(await api.get("/drive/v3/files", params={"fields": "nextPageToken, files(id, name)"}, headers=AUTH))

    assert files_of(page)
    assert all(set(entry) == {"id", "name"} for entry in files_of(page))


async def test_a_mime_type_filter_leaves_folders_out(api: httpx.AsyncClient) -> None:
    """DOCUMENTED: `mimeType = '...'` matches that type only, so a folder is not a document.
    https://developers.google.com/workspace/drive/api/guides/ref-search-terms"""
    await api.post("/drive/v3/files", json={"name": "Ballast Folder", "mimeType": FOLDER}, headers=AUTH)
    await create_doc(api, "Ballast Doc", "trim")

    page = answer(
        await api.get(
            "/drive/v3/files", params={"q": f"mimeType = '{DOC}'", "fields": "files(name,mimeType)"}, headers=AUTH
        )
    )

    assert {f["mimeType"] for f in files_of(page)} == {DOC}
    assert "Ballast Doc" in names_of(page) and "Ballast Folder" not in names_of(page)


async def test_trashed_true_lists_only_what_is_in_the_trash(api: httpx.AsyncClient) -> None:
    """DOCUMENTED: `trashed` is true or false and selects on whether the file is in the trash; with nothing trashed,
    `trashed = true` lists nothing. https://developers.google.com/workspace/drive/api/guides/ref-search-terms"""
    nothing = await listed(api, "trashed = true")
    binned = await create_doc(api, "Old Manifest", "superseded")
    await api.patch(f"/drive/v3/files/{binned['id']}", json={"trashed": True}, headers=AUTH)

    assert files_of(nothing) == []
    assert names_of(await listed(api, "trashed = true")) == ["Old Manifest"]


async def test_an_escaped_apostrophe_finds_the_name_that_holds_one(api: httpx.AsyncClient) -> None:
    """DOCUMENTED: a single quote inside a quoted value is written `\\'`.
    https://developers.google.com/workspace/drive/api/guides/ref-search-terms"""
    folder = answer(await api.post("/drive/v3/files", json={"name": "Skipper's Log", "mimeType": FOLDER}, headers=AUTH))

    found = await listed(api, f"name = 'Skipper\\'s Log' and mimeType = '{FOLDER}' and trashed = false")

    assert [f["id"] for f in files_of(found)] == [folder["id"]]


async def test_an_unknown_query_term_is_refused_400_invalid_value_on_q(api: httpx.AsyncClient) -> None:
    """OBSERVED (https://stackoverflow.com/q/67608827, https://stackoverflow.com/q/69699515): a `q` Drive cannot read
    is a 400 `invalid` "Invalid Value" located at `q`, and the message does not name the term."""
    response = await api.get("/drive/v3/files", params={"q": "hullColour = 'grey'"}, headers=AUTH)

    assert answer(response, 400)["error"]["errors"] == [  # type: ignore[index]
        {
            "domain": "global",
            "reason": "invalid",
            "message": "Invalid Value",
            "locationType": "parameter",
            "location": "q",
        }
    ]


async def test_name_contains_matches_a_prefix_of_a_word_never_a_fragment_inside_one(api: httpx.AsyncClient) -> None:
    """DOCUMENTED that `contains` on a name is prefix matching only
    (https://developers.google.com/workspace/drive/api/guides/ref-search-terms); OBSERVED that a later word's start
    is a prefix too."""
    await api.post("/drive/v3/files", json={"name": "Quayside Survey", "mimeType": FOLDER}, headers=AUTH)

    assert "Quayside Survey" in names_of(await listed(api, "name contains 'Quay'"))
    assert "Quayside Survey" in names_of(await listed(api, "name contains 'Surv'"))
    assert "Quayside Survey" not in names_of(await listed(api, "name contains 'yside'"))


async def test_exporting_a_doc_over_ten_megabytes_is_refused_403_export_size_limit_exceeded(tmp_path: Path) -> None:
    """DOCUMENTED that exported content is limited to 10 MB
    (https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export); OBSERVED that the refusal is
    a 403 with reason `exportSizeLimitExceeded`."""
    huge = SeededDocument(provider="google_workspace", title="Full Ledger", text="x" * wire.EXPORT_LIMIT_BYTES)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "huge.db", "root", clock)
    provider = build()
    provider.seed(SCENARIO.model_copy(update={"documents": [huge]}), store)
    issue(store, TOKEN, OWNER)
    async with client_for(provider, store, clock) as api:
        [ledger] = files_of(await listed(api, "name = 'Full Ledger'"))
        response = await api.get(
            f"/drive/v3/files/{ledger['id']}/export", params={"mimeType": "text/plain"}, headers=AUTH
        )

    assert reason_of(response, 403) == "exportSizeLimitExceeded"


async def test_a_link_share_to_anyone_as_reader_is_granted(api: httpx.AsyncClient) -> None:
    """DOCUMENTED: type `anyone` needs no address, and `reader` is one of the roles.
    https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions"""
    made = await create_doc(api, "Visitor Guide", "gate codes withheld")

    granted = answer(
        await api.post(
            f"/drive/v3/files/{made['id']}/permissions", json={"type": "anyone", "role": "reader"}, headers=AUTH
        )
    )

    assert (granted["type"], granted["role"]) == ("anyone", "reader")


async def test_a_deleted_file_leaves_every_listing(api: httpx.AsyncClient) -> None:
    """DOCUMENTED: `files.delete` removes the file outright, skipping the trash.
    https://developers.google.com/workspace/drive/api/reference/rest/v3/files/delete"""
    made = await create_doc(api, "Spare Berth", "unused")

    gone = await api.delete(f"/drive/v3/files/{made['id']}", headers=AUTH)

    assert gone.status_code == 204
    assert "Spare Berth" not in names_of(await listed(api, "trashed = false"))
    assert "Spare Berth" not in names_of(await listed(api, "trashed = true"))
    assert reason_of(await api.get(f"/drive/v3/files/{made['id']}", headers=AUTH), 404) == "notFound"


async def test_an_uploaded_doc_reads_the_same_through_export_and_the_docs_api(
    api: httpx.AsyncClient, docs: httpx.AsyncClient
) -> None:
    """OBSERVED: text converted into a Doc is one document; its plain-text export and its Docs body hold the same
    characters, the export differing only by a leading byte-order mark and CRLF line ends."""
    made = await create_doc(api, "Harbour Notes", "Tides\n\nThe east pier floods at spring tide.")

    exported = (
        await api.get(f"/drive/v3/files/{made['id']}/export", params={"mimeType": "text/plain"}, headers=AUTH)
    ).text
    body = "".join(text_of(e) for e in content(await get(docs, str(made["id"]))) if "paragraph" in e)

    assert exported.removeprefix("﻿").replace("\r\n", "\n") == body


async def test_a_doc_created_with_no_text_is_its_final_newline_and_nothing_else(
    api: httpx.AsyncClient, docs: httpx.AsyncClient
) -> None:
    """OBSERVED: an empty Doc's body is a section break and one paragraph holding only its newline, ending at 2; it
    does not carry its own name as text."""
    made = answer(await api.post("/drive/v3/files", json={"name": "Empty Berth Log", "mimeType": DOC}, headers=AUTH))

    elements = content(await get(docs, str(made["id"])))
    exported = await api.get(f"/drive/v3/files/{made['id']}/export", params={"mimeType": "text/plain"}, headers=AUTH)

    assert [text_of(e) for e in elements if "paragraph" in e] == ["\n"]
    assert elements[-1]["endIndex"] == 2
    assert b"Empty Berth Log" not in exported.content


async def test_a_csv_uploaded_as_a_sheet_exports_its_rows_as_csv(api: httpx.AsyncClient) -> None:
    """DOCUMENTED: a CSV uploaded with the Sheets type is converted into a Sheet, and a Sheet exports as text/csv.
    https://developers.google.com/workspace/drive/api/guides/manage-uploads"""
    made = answer(
        await api.post(
            "/upload/drive/v3/files",
            params={"uploadType": "multipart"},
            content=multipart(
                json.dumps({"name": "Berths", "mimeType": SHEET}), b"berth,vessel\n4,Kestrel\n", "text/csv"
            ),
            headers={**AUTH, "Content-Type": 'multipart/related; boundary="===b0undary=="'},
        )
    )

    exported = await api.get(f"/drive/v3/files/{made['id']}/export", params={"mimeType": "text/csv"}, headers=AUTH)

    assert exported.status_code == 200, exported.text
    assert [line.split(",") for line in exported.text.splitlines()] == [["berth", "vessel"], ["4", "Kestrel"]]
