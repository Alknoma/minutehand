"""Who sees what, what people do without the agent, and the refusals that follow from both."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.google_drive.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import (
    Access,
    AccessRole,
    DocumentChange,
    Edited,
    Fault,
    FaultKind,
    Moved,
    SeededDocument,
    Shared,
    SharedSpace,
    SignIn,
)
from minutehand.domain.world import Actor, Operation
from tests.providers.google_drive.drive_world import (
    AUTH,
    SCENARIO,
    START,
    Drive,
    answer,
    client_for,
    create_doc,
    issue,
    listed,
    names_of,
    reason_of,
)

DOV = {"Authorization": "Bearer ya29.dov"}


@pytest.fixture
def dov(drive: Drive) -> dict[str, str]:
    issue(drive.store, "ya29.dov", "dov@example.com")
    return DOV


async def test_a_file_is_seen_only_by_its_owner_and_whoever_is_granted_it(
    drive: Drive, api: httpx.AsyncClient, dov: dict[str, str]
) -> None:
    made = await create_doc(api, "Owner Only", "private")
    hidden = await api.get(f"/drive/v3/files/{made['id']}", headers=dov)
    before = names_of(await listed(api, "name = 'Owner Only'"))
    dov_before = answer(await api.get("/drive/v3/files", params={"q": "name = 'Owner Only'"}, headers=dov))
    await api.post(
        f"/drive/v3/files/{made['id']}/permissions",
        json={"type": "user", "role": "reader", "emailAddress": "dov@example.com"},
        headers=AUTH,
    )
    seen = await api.get(f"/drive/v3/files/{made['id']}", headers=dov)
    write = await api.patch(f"/drive/v3/files/{made['id']}", json={"name": "Dov's"}, headers=dov)

    assert reason_of(hidden, 404) == "notFound", "a file you may not see is not found, as in Drive"
    assert before == ["Owner Only"] and dov_before["files"] == []
    assert seen.status_code == 200
    assert reason_of(write, 403) == "insufficientFilePermissions"


async def test_a_link_shared_with_anyone_opens_but_is_not_found_by_search(
    api: httpx.AsyncClient, dov: dict[str, str]
) -> None:
    made = await create_doc(api, "Linked", "by link")
    await api.post(f"/drive/v3/files/{made['id']}/permissions", json={"type": "anyone", "role": "reader"}, headers=AUTH)
    opened = await api.get(f"/drive/v3/files/{made['id']}", headers=dov)
    searched = answer(await api.get("/drive/v3/files", params={"q": "name = 'Linked'"}, headers=dov))
    assert opened.status_code == 200 and searched["files"] == []


async def test_a_writer_may_not_trash_or_delete_a_file_in_someone_elses_my_drive_is_refused(
    api: httpx.AsyncClient, dov: dict[str, str]
) -> None:
    made = await create_doc(api, "Mara's", "mine")
    await api.post(
        f"/drive/v3/files/{made['id']}/permissions",
        json={"type": "user", "role": "writer", "emailAddress": "dov@example.com"},
        headers=AUTH,
    )
    trash = await api.patch(f"/drive/v3/files/{made['id']}", json={"trashed": True}, headers=dov)
    delete = await api.delete(f"/drive/v3/files/{made['id']}", headers=dov)
    rename = await api.patch(f"/drive/v3/files/{made['id']}", json={"name": "Renamed by Dov"}, headers=dov)
    assert reason_of(trash, 403) == "insufficientFilePermissions"
    assert reason_of(delete, 403) == "insufficientFilePermissions"
    assert rename.status_code == 200


async def test_an_unknown_expired_or_revoked_token_is_refused_401(
    drive: Drive, api: httpx.AsyncClient, oauth: httpx.AsyncClient
) -> None:
    unknown = await api.get("/drive/v3/files", headers={"Authorization": "Bearer ya29.never-issued"})
    issue(drive.store, "ya29.short", "mara@example.com", lasts=timedelta(hours=1))
    fresh = await api.get("/drive/v3/files", headers={"Authorization": "Bearer ya29.short"})
    drive.clock.jump(START + timedelta(hours=1))
    expired = await api.get("/drive/v3/files", headers={"Authorization": "Bearer ya29.short"})
    revoked = await oauth.post("/revoke", params={"token": "ya29.a token the run issued to the owner"})
    after = await api.get("/drive/v3/files", headers=AUTH)
    again = await oauth.post("/revoke", params={"token": "ya29.a token the run issued to the owner"})

    assert reason_of(unknown, 401) == "authError" and fresh.status_code == 200
    assert reason_of(expired, 401) == "authError"
    assert revoked.status_code == 200 and reason_of(after, 401) == "authError"
    assert answer(again, 400) == {"error": "invalid_token", "error_description": "Token expired or revoked"}


async def test_a_credential_the_scenario_does_not_name_is_refused_invalid_grant(tmp_path: Path) -> None:
    named = SCENARIO.model_copy(
        update={"sign_ins": [SignIn(provider="google_drive", credential="1//known", person="mara")]}
    )
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "root", clock)
    build().seed(named, store)
    async with client_for(build(), store, clock, "oauth2.googleapis.com") as oauth:
        known = await oauth.post("/token", data={"grant_type": "refresh_token", "refresh_token": "1//known"})
        stranger = await oauth.post("/token", data={"grant_type": "refresh_token", "refresh_token": "1//stranger"})
    assert known.status_code == 200
    assert answer(stranger, 400) == {"error": "invalid_grant", "error_description": "Bad Request"}


def test_a_fault_naming_a_call_this_fake_does_not_answer_is_refused_at_seeding(tmp_path: Path) -> None:
    faulty = SCENARIO.model_copy(
        update={"faults": [Fault(provider="google_drive", operation="files.watch", kind=FaultKind.NOT_FOUND)]}
    )
    with pytest.raises(ValueError, match=r"'files\.watch', which is not a Google Drive, Docs or Slides call"):
        build().seed(faulty, SqliteStore(tmp_path / "w.db", "root", RunClock(START)))


async def test_a_fault_is_played_once_per_time_and_its_count_is_in_the_world(tmp_path: Path) -> None:
    faulty = SCENARIO.model_copy(
        update={
            "faults": [
                Fault(
                    provider="google_drive",
                    operation="files.list",
                    kind=FaultKind.UNAVAILABLE,
                    times=2,
                    after=timedelta(hours=1),
                )
            ]
        }
    )
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "root", clock)
    build().seed(faulty, store)
    issue(store, "ya29.a token the run issued to the owner", "mara@example.com")
    async with client_for(build(), store, clock) as api:
        early = await api.get("/drive/v3/files", headers=AUTH)
        clock.jump(START + timedelta(hours=1))
        statuses = [(await api.get("/drive/v3/files", headers=AUTH)).status_code for _ in range(3)]
        fork = store.fork("before", at_seq=store.head() - 3, clock=RunClock(START + timedelta(hours=1)))
    async with client_for(build(), fork, RunClock(START + timedelta(hours=1))) as forked:
        replayed = (await forked.get("/drive/v3/files", headers=AUTH)).status_code
    assert early.status_code == 200
    assert statuses == [503, 503, 200]
    assert replayed == 503, "a fork before the second refusal plays it again"


async def test_people_edit_move_and_share_a_seeded_document_as_themselves(
    drive: Drive, api: httpx.AsyncClient, dov: dict[str, str]
) -> None:
    drive.clock.jump(START + timedelta(hours=2))
    for action in (
        Edited(append="Initech joined."),
        Moved(folder="Procurement/Archive"),
        Shared(access=Access(person="dov", role=AccessRole.COMMENTER)),
    ):
        drive.provider.change(
            DocumentChange(
                provider="google_drive",
                document="Supplier Shortlist",
                by="mara",
                after=timedelta(hours=2),
                action=action,
            ),
            drive.store,
            drive.clock,
        )
    shortlist = answer(
        await api.get(
            "/drive/v3/files",
            params={
                "q": "name = 'Supplier Shortlist'",
                "fields": "files(id,parents,modifiedTime,lastModifyingUser,shared)",
            },
            headers=AUTH,
        )
    )["files"][0]
    exported = await api.get(
        f"/drive/v3/files/{shortlist['id']}/export", params={"mimeType": "text/plain"}, headers=AUTH
    )
    archive = answer(
        await api.get(f"/drive/v3/files/{shortlist['parents'][0]}", params={"fields": "name"}, headers=AUTH)
    )
    by_dov = await api.get(f"/drive/v3/files/{shortlist['id']}", headers=dov)
    people = [(e.actor, e.operation) for e in drive.store.events() if e.actor is Actor.PERSON]

    assert exported.text.endswith("Initech joined.\r\n")
    assert archive == {"name": "Archive"}
    assert shortlist["modifiedTime"] == "2026-09-14T10:30:00.000Z" and shortlist["shared"] is True
    assert shortlist["lastModifyingUser"]["emailAddress"] == "mara@example.com"
    assert by_dov.status_code == 200
    assert people and all(op in (Operation.CREATE, Operation.UPDATE) for _, op in people)


async def test_a_change_listing_says_removed_for_a_file_deleted_and_the_token_is_checked(
    api: httpx.AsyncClient,
) -> None:
    start = answer(await api.get("/drive/v3/changes/startPageToken", headers=AUTH))["startPageToken"]
    made = await create_doc(api, "Short Lived", "x")
    await api.delete(f"/drive/v3/files/{made['id']}", headers=AUTH)
    changes = answer(await api.get("/drive/v3/changes", params={"pageToken": start}, headers=AUTH))
    missing = await api.get("/drive/v3/changes", headers=AUTH)
    future = await api.get("/drive/v3/changes", params={"pageToken": "999999"}, headers=AUTH)

    assert [(c["fileId"], c["removed"]) for c in changes["changes"]] == [(made["id"], True)]
    assert "file" not in changes["changes"][0] and "newStartPageToken" in changes
    assert reason_of(missing, 400) == "required"
    assert reason_of(future, 400) == "invalid"


async def test_a_watch_without_an_address_type_or_unique_id_is_refused(api: httpx.AsyncClient) -> None:
    start = answer(await api.get("/drive/v3/changes/startPageToken", headers=AUTH))["startPageToken"]
    watch = "/drive/v3/changes/watch"
    kind = await api.post(
        watch, params={"pageToken": start}, json={"id": "c1", "type": "email", "address": "https://a"}, headers=AUTH
    )
    no_id = await api.post(
        watch, params={"pageToken": start}, json={"type": "web_hook", "address": "https://a"}, headers=AUTH
    )
    first = await api.post(
        watch,
        params={"pageToken": start},
        json={"id": "c1", "type": "web_hook", "address": "https://127.0.0.1:1/h"},
        headers=AUTH,
    )
    twice = await api.post(
        watch,
        params={"pageToken": start},
        json={"id": "c1", "type": "web_hook", "address": "https://127.0.0.1:1/h"},
        headers=AUTH,
    )
    assert reason_of(kind, 400) == "push.channelTypeNotSupported"
    assert reason_of(no_id, 400) == "required"
    assert first.status_code == 200 and answer(first)["kind"] == "api#channel"
    assert reason_of(twice, 400) == "channelIdNotUnique"


async def test_a_shared_drive_member_reads_its_files_and_a_stranger_does_not(tmp_path: Path) -> None:
    spaced = SCENARIO.model_copy(
        update={
            "spaces": [
                SharedSpace(
                    provider="google_drive", name="Team", members=[Access(person="dov", role=AccessRole.READER)]
                )
            ],
            "documents": [SeededDocument(provider="google_drive", title="Team Plan", text="hi", space="Team")],
        }
    )
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "root", clock)
    build().seed(spaced, store)
    issue(store, "ya29.dov", "dov@example.com")
    issue(store, "ya29.mara", "mara@example.com")
    params = {
        "q": "trashed = false",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
        "fields": "files(name)",
    }
    async with client_for(build(), store, clock) as api:
        dov_sees = answer(await api.get("/drive/v3/files", params=params, headers={"Authorization": "Bearer ya29.dov"}))
        mara_sees = answer(
            await api.get("/drive/v3/files", params=params, headers={"Authorization": "Bearer ya29.mara"})
        )
        drives = answer(await api.get("/drive/v3/drives", headers={"Authorization": "Bearer ya29.mara"}))
    assert dov_sees["files"] == [{"name": "Team Plan"}]
    assert {"name": "Team Plan"} not in mara_sees["files"] and drives["drives"] == []
