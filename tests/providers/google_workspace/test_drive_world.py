"""The Drive lives in the store and nowhere else; seeding, sign-in, and what the manifest claims."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx

from minutehand.adapters.providers.google_workspace import state
from minutehand.adapters.providers.google_workspace.manifest import MANIFEST
from minutehand.adapters.providers.google_workspace.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.provider import Tier
from minutehand.domain.world import Actor, DocumentSnapshot, EntityKind, Operation
from tests.providers.google_workspace.drive_world import (
    AUTH,
    DOC,
    FOLDER,
    OWNER,
    ROOT_ID,
    SCENARIO,
    START,
    TOKEN,
    Drive,
    answer,
    client_for,
    create_doc,
    listed,
    names_of,
    unsigned_assertion,
)


async def test_the_drive_survives_a_new_app_over_a_new_connection(drive: Drive) -> None:
    async with client_for(drive.provider, drive.store, drive.clock) as first:
        made = await create_doc(first, "Written First", "by the first app")

    reopened = SqliteStore(drive.path, "root", RunClock(START))
    async with client_for(build(), reopened, RunClock(START)) as second:
        found = names_of(await listed(second, "fullText contains 'first'"))
        exported = await second.get(
            f"/drive/v3/files/{made['id']}/export", params={"mimeType": "text/markdown"}, headers=AUTH
        )

    assert found == ["Written First"]
    assert exported.content == b"by the first app"


async def test_a_fork_sees_files_only_up_to_the_fork(drive: Drive, api: httpx.AsyncClient) -> None:
    before = await create_doc(api, "Before Fork", "kept")
    at = drive.store.head()
    after = await create_doc(api, "After Fork", "parent only")
    await api.patch(f"/drive/v3/files/{before['id']}", json={"name": "Renamed In Parent"}, headers=AUTH)

    fork = drive.store.fork("what-if", at_seq=at, clock=RunClock(START))
    async with client_for(drive.provider, fork, RunClock(START)) as forked:
        assert {"Before Fork"} <= set(names_of(await listed(forked, "")))
        assert "After Fork" not in names_of(await listed(forked, ""))
        assert (await forked.get(f"/drive/v3/files/{after['id']}", headers=AUTH)).status_code == 404
        await create_doc(forked, "Only In Fork", "fork only")
        forked_names = names_of(await listed(forked, f"mimeType = '{DOC}'"))

    parent_names = names_of(await listed(api, f"mimeType = '{DOC}'"))
    assert "Only In Fork" in forked_names and "Renamed In Parent" not in forked_names
    assert "Only In Fork" not in parent_names and {"After Fork", "Renamed In Parent"} <= set(parent_names)


def test_seeding_writes_my_drive_people_access_and_documents_as_the_scenario(drive: Drive) -> None:
    events = [e for e in drive.store.events() if e.entity.external_id != state.secret_digest(TOKEN)]
    assert events and {e.actor for e in events} == {Actor.SCENARIO}

    world = drive.world
    root = world.file(ROOT_ID)
    assert root is not None and [o.emailAddress for o in root.file.owners or []] == ["mara@example.com"]
    dov_root = world.file(state.root_id("dov@example.com"))
    assert dov_root is not None and dov_root.file.name == "My Drive", "everyone has a My Drive of their own"
    files = {stored.file.name: stored for stored, _ in world.walk(ROOT_ID)}
    assert sorted(files) == ["Kickoff Notes", "Procurement", "Supplier Shortlist"], "the slack document is not Drive's"
    assert files["Procurement"].file.mimeType == FOLDER and files["Procurement"].file.parents == [ROOT_ID]
    shortlist = files["Supplier Shortlist"]
    assert shortlist.file.parents == [files["Procurement"].file.id] and shortlist.file.mimeType == DOC
    assert state.readable_text(shortlist) == "Three suppliers remain.\nPrices due Friday."
    assert shortlist.file.createdTime == "2026-09-14T08:30:00.000Z"
    assert [o.emailAddress for o in shortlist.file.owners or []] == ["mara@example.com"]
    assert world.grants(ROOT_ID) == [], "nobody is granted the owner's My Drive unless the scenario says so"
    assert world.role("dov@example.com", shortlist) is None and world.role(OWNER, shortlist) == "owner"
    assert world.user("dov@example.com") is not None and world.user("mara@example.com") is not None

    created = [e for e in events if e.entity == state.file_ref(shortlist.file.id)]
    assert [(e.operation, e.after) for e in created] == [
        (
            Operation.CREATE,
            DocumentSnapshot(
                title="Supplier Shortlist",
                mime_type=DOC,
                text="Three suppliers remain.\nPrices due Friday.",
                last_edited_by=OWNER,
                last_edited_at=START,
                owner=OWNER,
            ),
        ),
    ]


def test_seeded_ids_are_the_same_in_every_run(tmp_path: Path) -> None:
    ids: list[list[str]] = []
    for name in ("one", "two"):
        directory = tmp_path / name
        directory.mkdir()
        store = SqliteStore(directory / "world.db", "root", RunClock(START))
        build().seed(SCENARIO, store)
        ids.append(sorted(e.entity.external_id for e in store.events() if e.entity.kind is EntityKind.DOCUMENT))
    assert ids[0] == ids[1]


async def test_the_token_endpoint_answers_a_service_account_assertion(oauth: httpx.AsyncClient) -> None:
    signed_in = answer(
        await oauth.post(
            "/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": unsigned_assertion("reader@sim-project.iam.example.com"),
            },
        )
    )
    assert set(signed_in) == {"access_token", "expires_in", "token_type"}
    assert signed_in["token_type"] == "Bearer" and signed_in["expires_in"] == 3599
    assert str(signed_in["access_token"]).startswith("ya29.")


async def test_the_token_endpoint_answers_a_refresh_token(oauth: httpx.AsyncClient, api: httpx.AsyncClient) -> None:
    refreshed = answer(
        await oauth.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "1//refresh",
                "client_id": "client.example",
                "client_secret": "s",
                "scope": "https://www.googleapis.com/auth/drive",
            },
        )
    )
    assert refreshed["scope"] == "https://www.googleapis.com/auth/drive"
    listing = await api.get("/drive/v3/files", headers={"Authorization": f"Bearer {refreshed['access_token']}"})
    assert listing.status_code == 200


async def test_the_token_endpoint_rejects_an_unknown_grant_or_a_missing_assertion(oauth: httpx.AsyncClient) -> None:
    unknown = answer(await oauth.post("/token", data={"grant_type": "password"}), 400)
    missing = answer(
        await oauth.post("/token", data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer"}), 400
    )
    assert unknown["error"] == "unsupported_grant_type"
    assert missing["error"] == "invalid_request"


def test_the_provider_is_registered_and_its_manifest_imports_nothing_else() -> None:
    assert (MANIFEST.key, MANIFEST.tier) == ("google_workspace", Tier.FINISHED)
    assert MANIFEST.hosts == [
        "www.googleapis.com",
        "oauth2.googleapis.com",
        "docs.googleapis.com",
        "slides.googleapis.com",
        "iamcredentials.googleapis.com",
    ]
    assert MANIFEST.kinds == [EntityKind.DOCUMENT, EntityKind.COMMENT] and not MANIFEST.pushes_events
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from minutehand.adapters.proxy.registry import Registry; r = Registry.installed();"
            "print(r.claimant('docs.googleapis.com').key,"
            " sorted(m for m in sys.modules if m.startswith('minutehand.adapters.providers.google_workspace.')))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.strip() == "google_workspace ['minutehand.adapters.providers.google_workspace.manifest']"
