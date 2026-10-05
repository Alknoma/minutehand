"""Drive v3 and the Docs read, through the ASGI app, over a real store and clock."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.google_drive import state
from minutehand.adapters.providers.google_drive.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.world import Actor, DocumentSnapshot, EntityKind, Operation, RecordSnapshot
from tests.providers.google_drive.drive_world import (
    AUTH,
    DOC,
    FOLDER,
    LATER,
    OWNER,
    ROOT_ID,
    SCENARIO,
    START,
    TOKEN,
    Drive,
    answer,
    client_for,
    create_doc,
    files_of,
    issue,
    listed,
    multipart,
    names_of,
)


async def make_folder(api: httpx.AsyncClient, name: str, parent: str | None = None) -> str:
    body: dict[str, object] = {"name": name, "mimeType": FOLDER}
    if parent is not None:
        body["parents"] = [parent]
    made = answer(await api.post("/drive/v3/files", json=body, headers=AUTH))
    return str(made["id"])


@pytest.mark.parametrize(
    "q",
    [
        "name = 'Quarterly Budget Review'",
        "name contains 'Budget'",
        "name contains 'quarterly'",
        "fullText contains 'freight'",
        "fullText contains '\"freight costs\"'",
        f"mimeType = '{DOC}'",
        "trashed = false",
        "name contains 'Budget' and trashed = false and mimeType = 'application/vnd.google-apps.document'",
    ],
)
async def test_a_created_doc_is_found_by_each_supported_q_form(api: httpx.AsyncClient, q: str) -> None:
    await create_doc(api, "Quarterly Budget Review", "Freight costs rose sharply this quarter.")
    assert "Quarterly Budget Review" in names_of(await listed(api, q))


@pytest.mark.parametrize(
    "q",
    [
        "name = 'Quarterly Budget'",
        "name contains 'udget'",
        "fullText contains 'freigh'",
        "fullText contains '\"costs freight\"'",
        f"mimeType = '{FOLDER}'",
        "trashed = true",
        "not name contains 'Budget'",
    ],
)
async def test_a_q_that_does_not_describe_the_doc_does_not_find_it(api: httpx.AsyncClient, q: str) -> None:
    await create_doc(api, "Quarterly Budget Review", "Freight costs rose sharply this quarter.")
    assert "Quarterly Budget Review" not in names_of(await listed(api, q))


async def test_in_parents_finds_a_doc_in_its_folder_and_nowhere_else(api: httpx.AsyncClient) -> None:
    folder = await make_folder(api, "Contracts")
    await create_doc(api, "Freight Contract", "Signed in August.", parent=folder)

    assert names_of(await listed(api, f"'{folder}' in parents")) == ["Freight Contract"]
    assert "Freight Contract" not in names_of(await listed(api, "'root' in parents"))
    assert "Contracts" in names_of(await listed(api, "'root' in parents"))


async def test_a_listing_with_no_q_lists_every_file_including_the_seeded_ones(api: httpx.AsyncClient) -> None:
    await create_doc(api, "Freight Contract", "Signed in August.")
    assert names_of(await listed(api, "")) == ["Freight Contract", "Kickoff Notes", "Procurement", "Supplier Shortlist"]


async def test_a_created_doc_exports_as_text_with_a_bom_and_crlf(api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "First line\nSecond line")

    plain = await api.get(f"/drive/v3/files/{made['id']}/export", params={"mimeType": "text/plain"}, headers=AUTH)
    markdown = await api.get(f"/drive/v3/files/{made['id']}/export", params={"mimeType": "text/markdown"}, headers=AUTH)

    assert plain.status_code == 200 and plain.headers["content-type"].startswith("text/plain")
    assert plain.content == "﻿First line\r\nSecond line\r\n".encode()
    assert markdown.content == b"First line\nSecond line"


async def test_a_created_doc_reads_through_the_docs_api_as_paragraphs_of_text_runs(
    api: httpx.AsyncClient,
    docs: httpx.AsyncClient,
) -> None:
    made = await create_doc(api, "Minutes", "Agenda \U0001f4c5\nActions")

    document = answer(await docs.get(f"/v1/documents/{made['id']}", headers=AUTH))

    assert document["documentId"] == made["id"] and document["title"] == "Minutes"
    body = document["body"]
    assert isinstance(body, dict)
    content = body["content"]
    assert isinstance(content, list)
    assert content[0] == {
        "endIndex": 1,
        "sectionBreak": {
            "sectionStyle": {
                "columnSeparatorStyle": "NONE",
                "contentDirection": "LEFT_TO_RIGHT",
                "sectionType": "CONTINUOUS",
            }
        },
    }
    runs = [(e["startIndex"], e["endIndex"], e["paragraph"]["elements"][0]["textRun"]["content"]) for e in content[1:]]
    assert runs == [(1, 11, "Agenda \U0001f4c5\n"), (11, 19, "Actions\n")], "indexes count UTF-16 units"


async def test_every_timestamp_is_the_runs_clock_not_the_machines(drive: Drive, api: httpx.AsyncClient) -> None:
    drive.clock.jump(LATER)
    made = await create_doc(api, "Minutes", "text")
    assert made["createdTime"] == made["modifiedTime"] == "2026-09-14T11:37:11.250Z"

    drive.clock.jump(LATER.replace(hour=15))
    renamed = answer(
        await api.patch(
            f"/drive/v3/files/{made['id']}",
            json={"name": "Final Minutes"},
            params={"fields": "name,createdTime,modifiedTime"},
            headers=AUTH,
        )
    )
    assert renamed == {
        "name": "Final Minutes",
        "createdTime": "2026-09-14T11:37:11.250Z",
        "modifiedTime": "2026-09-14T15:37:11.250Z",
    }


async def test_a_write_is_an_agent_event_on_a_document_with_title_mime_type_and_parent(
    drive: Drive,
    api: httpx.AsyncClient,
) -> None:
    folder = await make_folder(api, "Contracts")
    made = await create_doc(api, "Freight Contract", "Signed.", parent=folder)

    event = drive.store.events()[-1]
    assert (event.actor, event.operation, event.entity.kind, event.entity.external_id) == (
        Actor.AGENT,
        Operation.CREATE,
        EntityKind.DOCUMENT,
        made["id"],
    )
    assert isinstance(event.after, DocumentSnapshot)
    assert (event.after.title, event.after.mime_type, event.after.text) == ("Freight Contract", DOC, "Signed.")
    stored = drive.store.get(state.file_ref(str(made["id"])))
    assert stored is not None and stored.parent == folder

    await api.patch(f"/drive/v3/files/{made['id']}", json={"name": "Freight Contract v2"}, headers=AUTH)
    renamed = drive.store.events()[-1]
    assert renamed.operation is Operation.UPDATE and isinstance(renamed.after, DocumentSnapshot)
    assert renamed.after.title == "Freight Contract v2"


async def test_file_ids_come_from_the_event_sequence(drive: Drive, api: httpx.AsyncClient, tmp_path: Path) -> None:
    next_seq = drive.store.head() + 1
    made = await create_doc(api, "Minutes", "text")

    again = SqliteStore(tmp_path / "again.db", "root", RunClock(START))
    build().seed(SCENARIO, again)
    issue(again, TOKEN, OWNER)
    async with client_for(build(), again, RunClock(START)) as replay:
        remade = await create_doc(replay, "Minutes", "text")

    assert str(made["id"]).startswith(f"1F{next_seq:08d}") and len(str(made["id"])) == 30
    assert remade["id"] == made["id"], "the same call at the same point of the log mints the same id"


async def test_a_listing_is_a_search_and_a_get_is_a_read(drive: Drive, api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "text")
    await api.get("/drive/v3/files", headers=AUTH)
    searched = drive.store.events()[-1]
    await api.get(f"/drive/v3/files/{made['id']}", headers=AUTH)
    read = drive.store.events()[-1]

    assert (searched.actor, searched.operation, searched.entity) == (
        Actor.AGENT,
        Operation.SEARCH,
        state.file_ref(ROOT_ID),
    )
    assert (read.operation, read.entity.external_id) == (Operation.READ, made["id"])


async def test_a_listing_takes_pages_until_the_token_runs_out(api: httpx.AsyncClient) -> None:
    for n in range(5):
        await create_doc(api, f"Page Doc {n}", "text")
    seen: list[str] = []
    token: str | None = None
    pages = 0
    while True:
        params = {"q": "name contains 'Page'", "pageSize": "2", "fields": "nextPageToken,files(id,name)"}
        if token is not None:
            params["pageToken"] = token
        page = answer(await api.get("/drive/v3/files", params=params, headers=AUTH))
        pages += 1
        seen += [str(f["name"]) for f in files_of(page)]
        if "nextPageToken" not in page:
            break
        token = str(page["nextPageToken"])
    assert pages == 3
    assert seen == [f"Page Doc {n}" for n in range(5)]


async def test_a_listing_orders_by_name_descending(api: httpx.AsyncClient) -> None:
    for name in ["Beta", "Alpha", "Gamma"]:
        await create_doc(api, name, "text")
    page = await listed(
        api, f"mimeType = '{DOC}' and not name contains 'Supplier' and not name contains 'Kickoff'", orderBy="name desc"
    )
    assert [f["name"] for f in files_of(page)] == ["Gamma", "Beta", "Alpha"]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
async def test_a_multipart_upload_keeps_binary_media_exactly(api: httpx.AsyncClient, newline: bytes) -> None:
    media = bytes(range(256)) + b"\r\n\n--not-a-boundary\r\n"
    made = answer(
        await api.post(
            "/upload/drive/v3/files",
            params={"uploadType": "multipart", "fields": "id,name,mimeType,size,md5Checksum"},
            content=multipart(
                json.dumps({"name": "scan.pdf", "mimeType": "application/pdf"}),
                media,
                "application/pdf",
                newline=newline,
            ),
            headers={**AUTH, "Content-Type": 'multipart/related; boundary="===b0undary=="'},
        )
    )
    downloaded = await api.get(f"/drive/v3/files/{made['id']}", params={"alt": "media"}, headers=AUTH)

    assert made["mimeType"] == "application/pdf" and made["size"] == str(len(media))
    assert made["md5Checksum"] == hashlib.md5(media).hexdigest()
    assert downloaded.content == media and downloaded.headers["content-type"] == "application/pdf"


async def test_a_media_upload_is_an_untitled_file_of_the_bodys_type(api: httpx.AsyncClient) -> None:
    made = answer(
        await api.post(
            "/upload/drive/v3/files",
            params={"uploadType": "media", "fields": "name,mimeType"},
            content=b"a,b\n1,2\n",
            headers={**AUTH, "Content-Type": "text/csv"},
        )
    )
    assert made == {"name": "Untitled", "mimeType": "text/csv"}


async def test_new_content_replaces_a_docs_text(api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "draft")
    await api.patch(
        f"/upload/drive/v3/files/{made['id']}",
        params={"uploadType": "media"},
        content=b"final",
        headers={**AUTH, "Content-Type": "text/plain"},
    )
    exported = await api.get(f"/drive/v3/files/{made['id']}/export", params={"mimeType": "text/markdown"}, headers=AUTH)
    assert exported.content == b"final"
    assert "Minutes" in names_of(await listed(api, "fullText contains 'final'"))
    assert "Minutes" not in names_of(await listed(api, "fullText contains 'draft'"))


async def test_a_get_answers_only_the_fields_asked_for(api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "text")
    default = answer(await api.get(f"/drive/v3/files/{made['id']}", headers=AUTH))
    narrow = answer(
        await api.get(f"/drive/v3/files/{made['id']}", params={"fields": "id,owners(emailAddress)"}, headers=AUTH)
    )
    every = answer(await api.get(f"/drive/v3/files/{made['id']}", params={"fields": "*"}, headers=AUTH))

    assert set(default) == {"kind", "id", "name", "mimeType"}
    assert narrow == {"id": made["id"], "owners": [{"emailAddress": OWNER}]}
    assert {"parents", "createdTime", "modifiedTime", "version", "webViewLink", "trashed"} <= set(every)


async def test_a_listing_answers_without_a_page_token_unless_it_was_asked_for(api: httpx.AsyncClient) -> None:
    for n in range(3):
        await create_doc(api, f"Page Doc {n}", "text")
    page = answer(await api.get("/drive/v3/files", params={"pageSize": "1", "fields": "files(id)"}, headers=AUTH))
    assert set(page) == {"files"}


async def test_a_move_changes_the_one_parent(api: httpx.AsyncClient) -> None:
    source, target = await make_folder(api, "Inbox"), await make_folder(api, "Archive")
    made = await create_doc(api, "Minutes", "text", parent=source)
    moved = answer(
        await api.patch(
            f"/drive/v3/files/{made['id']}",
            json={},
            params={"addParents": target, "removeParents": source, "fields": "parents"},
            headers=AUTH,
        )
    )
    assert moved == {"parents": [target]}
    assert names_of(await listed(api, f"'{target}' in parents")) == ["Minutes"]
    assert names_of(await listed(api, f"'{source}' in parents")) == []


async def test_trash_hides_a_file_and_everything_in_a_trashed_folder_from_trashed_false(api: httpx.AsyncClient) -> None:
    folder = await make_folder(api, "Old")
    inside = await create_doc(api, "Inside", "text", parent=folder)
    loose = await create_doc(api, "Loose", "text")
    await api.patch(f"/drive/v3/files/{loose['id']}", json={"trashed": True}, headers=AUTH)
    await api.patch(f"/drive/v3/files/{folder}", json={"trashed": True}, headers=AUTH)

    live = names_of(await listed(api, "trashed = false"))
    binned = names_of(await listed(api, "trashed = true"))
    child = answer(
        await api.get(f"/drive/v3/files/{inside['id']}", params={"fields": "trashed,explicitlyTrashed"}, headers=AUTH)
    )

    assert not {"Old", "Inside", "Loose"} & set(live)
    assert {"Old", "Inside", "Loose"} <= set(binned), (
        "a listing with no trashed term includes the trash, as Drive's does"
    )
    assert child == {"trashed": True, "explicitlyTrashed": False}


async def test_delete_removes_a_folder_and_what_is_in_it(drive: Drive, api: httpx.AsyncClient) -> None:
    folder = await make_folder(api, "Old")
    inside = await create_doc(api, "Inside", "text", parent=folder)
    deleted = await api.delete(f"/drive/v3/files/{folder}", headers=AUTH)

    assert deleted.status_code == 204 and deleted.content == b""
    assert (await api.get(f"/drive/v3/files/{inside['id']}", headers=AUTH)).status_code == 404
    assert [e.operation for e in drive.store.events()[-2:]] == [Operation.DELETE, Operation.DELETE]


async def test_a_copy_is_a_new_file_with_the_same_text(api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "the text")
    copy = answer(
        await api.post(f"/drive/v3/files/{made['id']}/copy", json={}, params={"fields": "id,name"}, headers=AUTH)
    )
    exported = await api.get(f"/drive/v3/files/{copy['id']}/export", params={"mimeType": "text/markdown"}, headers=AUTH)

    assert copy["id"] != made["id"] and copy["name"] == "Copy of Minutes"
    assert exported.content == b"the text"


def roles(permissions: dict[str, object]) -> list[tuple[str, str]]:
    found = permissions["permissions"]
    assert isinstance(found, list)
    return sorted((str(p["role"]), str(p["emailAddress"])) for p in found)


async def test_permissions_list_the_owner_and_take_a_new_grant(
    drive: Drive,
    api: httpx.AsyncClient,
) -> None:
    shortlist = next(f for f in files_of(await listed(api, "name = 'Supplier Shortlist'")))
    before = answer(
        await api.get(
            f"/drive/v3/files/{shortlist['id']}/permissions",
            params={"fields": "permissions(role,emailAddress)"},
            headers=AUTH,
        )
    )
    granted = answer(
        await api.post(
            f"/drive/v3/files/{shortlist['id']}/permissions",
            json={"type": "user", "role": "reader", "emailAddress": "auditor@example.org"},
            headers=AUTH,
        )
    )
    after = answer(
        await api.get(
            f"/drive/v3/files/{shortlist['id']}/permissions",
            params={"fields": "permissions(role,emailAddress)"},
            headers=AUTH,
        )
    )

    assert roles(before) == [("owner", "mara@example.com")], "nobody else was granted it, nor its folders"
    assert set(granted) == {"kind", "id", "type", "role"} and granted["role"] == "reader"
    assert ("reader", "auditor@example.org") in roles(after)
    event = drive.store.events()[-3]
    assert (event.actor, event.operation, event.after) == (
        Actor.AGENT,
        Operation.CREATE,
        RecordSnapshot(resource="permission", text="reader auditor@example.org"),
    )


async def test_a_comment_is_recorded_as_a_comment_and_listed(drive: Drive, api: httpx.AsyncClient) -> None:
    made = await create_doc(api, "Minutes", "text")
    drive.clock.jump(LATER)
    posted = answer(
        await api.post(
            f"/drive/v3/files/{made['id']}/comments",
            json={"content": "Is this final?"},
            params={"fields": "id,content,createdTime,author(displayName,me)"},
            headers=AUTH,
        )
    )
    event = drive.store.events()[-1]
    listed_comments = answer(
        await api.get(f"/drive/v3/files/{made['id']}/comments", params={"fields": "comments(id,content)"}, headers=AUTH)
    )

    assert posted["content"] == "Is this final?" and posted["createdTime"] == "2026-09-14T11:37:11.250Z"
    assert posted["author"] == {"displayName": "Mara Lindqvist", "me": True}
    assert (event.actor, event.operation, event.entity.kind) == (Actor.AGENT, Operation.CREATE, EntityKind.COMMENT)
    stored = drive.store.get(state.comment_ref(str(posted["id"])))
    assert stored is not None and stored.parent == made["id"]
    assert listed_comments == {"comments": [{"id": posted["id"], "content": "Is this final?"}]}


async def test_about_answers_the_caller(api: httpx.AsyncClient) -> None:
    about = answer(await api.get("/drive/v3/about", params={"fields": "user"}, headers=AUTH))
    assert about == {
        "user": {
            "kind": "drive#user",
            "displayName": "Mara Lindqvist",
            "emailAddress": OWNER,
            "permissionId": state.permission_id(OWNER),
            "me": True,
        }
    }


async def test_root_answers_with_my_drives_own_id(api: httpx.AsyncClient) -> None:
    root = answer(await api.get("/drive/v3/files/root", params={"fields": "id,name,mimeType"}, headers=AUTH))
    assert root == {"id": ROOT_ID, "name": "My Drive", "mimeType": FOLDER}


async def test_each_host_answers_only_its_own_paths(drive: Drive) -> None:
    async with (
        client_for(drive.provider, drive.store, drive.clock, "www.googleapis.com") as www,
        client_for(drive.provider, drive.store, drive.clock, "docs.googleapis.com") as docs_host,
        client_for(drive.provider, drive.store, drive.clock, "127.0.0.1:8081") as direct,
    ):
        assert (await www.post("/token", data={"grant_type": "refresh_token", "refresh_token": "r"})).status_code == 404
        assert (await docs_host.get("/drive/v3/files", headers=AUTH)).status_code == 404
        assert (await direct.get("/drive/v3/files", headers=AUTH)).status_code == 200
        assert (
            await direct.post("/token", data={"grant_type": "refresh_token", "refresh_token": "r"})
        ).status_code == 200
