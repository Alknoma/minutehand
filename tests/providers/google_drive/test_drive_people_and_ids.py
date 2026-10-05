"""People with no email or a hidden one, a person's declared Drive account, and the ids a seed declares, read back
through Google's own client.

Drive omits `emailAddress` on a `User` "if the user has not made their email address visible to the requester"
(https://developers.google.com/workspace/drive/api/reference/rest/v3/User): that is how a person with no email, or
whose `google_drive` account hides it, is shown. What Drive cannot do for an account with no address (share with
it, sign in as it) is refused at load.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.adapters.providers.google_drive import state
from minutehand.adapters.providers.google_drive.provider import build
from minutehand.adapters.providers.google_drive.state import DriveWorld
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import (
    Access,
    AccessRole,
    DocumentHappening,
    Edited,
    Person,
    PersonAccount,
    Scenario,
    SeededDocument,
    Shared,
    SharedSpace,
    SignIn,
)
from minutehand.domain.world import DocumentSnapshot, GrantSnapshot
from tests.providers.google_drive.drive_world import START, Drive
from tests.providers.google_drive.test_drive_google_client import Google, google, off_loop

__all__ = ["google"]

DECLARED_DOC = "1DeclaredDocId_abc-XYZ0123456789"
DECLARED_DRIVE = "0ADeclaredDrive01Uk9"
INES_PERMISSION = "123456789012345678901"

PEOPLE_AND_IDS = Scenario(
    name="people_and_ids",
    goal="g",
    owner="mara",
    starts_at=START,
    people=[
        Person(key="mara", name="Mara Lindqvist", email="mara@example.com"),
        Person(
            key="ines",
            name="Ines Rocha",
            email="ines@example.com",
            accounts=[PersonAccount(provider="google_drive", email_visible=False, name="Inês R.", id=INES_PERMISSION)],
        ),
        Person(key="svc", name="Reporting Robot"),
    ],
    documents=[
        SeededDocument(
            provider="google_drive",
            title="Ines Draft",
            text="A draft.",
            owner="ines",
            modified_by="ines",
            shared_with=[Access(person="mara", role=AccessRole.WRITER)],
            id=DECLARED_DOC,
        ),
        SeededDocument(
            provider="google_drive",
            title="Robot Report",
            text="Numbers.",
            owner="svc",
            shared_with=[Access(person="mara", role=AccessRole.READER)],
        ),
        SeededDocument(provider="google_drive", title="Team Plan", text="Plan.", space="Team"),
    ],
    spaces=[
        SharedSpace(
            provider="google_drive",
            name="Team",
            id=DECLARED_DRIVE,
            members=[Access(person="mara", role=AccessRole.ORGANIZER), Access(person="ines")],
        )
    ],
)


@pytest.fixture
def drive(tmp_path: Path) -> Drive:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(PEOPLE_AND_IDS, store)
    return Drive(provider=provider, store=store, clock=clock, path=tmp_path / "world.db")


def _seeded(tmp_path: Path, scenario: Scenario) -> SqliteStore:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "refused.db", "root", clock)
    build().seed(scenario, store)
    return store


async def test_a_person_without_email_and_one_whose_email_is_hidden_are_shown_without_an_address(
    google: Google,
) -> None:
    files = google.drive(google.service_account()).files()
    draft = await off_loop(lambda: files.get(fileId=DECLARED_DOC, fields="id,owners,lastModifyingUser").execute())
    listed = await off_loop(lambda: files.list(q="name = 'Robot Report'", fields="files(id,owners)").execute())

    assert draft["id"] == DECLARED_DOC, "the id the seed declared is the id the client reads"
    assert draft["owners"] == [{"kind": "drive#user", "displayName": "Inês R.", "permissionId": INES_PERMISSION}]
    assert draft["lastModifyingUser"] == draft["owners"][0]
    robot = listed["files"][0]["owners"][0]
    assert "emailAddress" not in robot and robot["displayName"] == "Reporting Robot"
    assert robot["permissionId"] == state.person_permission_id("svc")


async def test_a_declared_shared_drive_id_is_the_drive_the_client_reads(google: Google) -> None:
    service = google.drive(google.service_account())
    got = await off_loop(lambda: service.drives().get(driveId=DECLARED_DRIVE).execute())
    listed = await off_loop(
        lambda: (
            service.files()
            .list(
                corpora="drive",
                driveId=DECLARED_DRIVE,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields="files(name,driveId)",
            )
            .execute()
        )
    )
    assert got["id"] == DECLARED_DRIVE and got["name"] == "Team"
    assert listed["files"] == [{"name": "Team Plan", "driveId": DECLARED_DRIVE}]


def test_records_name_each_person_by_key_and_by_address_only_when_they_have_one(drive: Drive) -> None:
    world = DriveWorld(drive.store)
    owners = {
        e.after.title: (e.after.owner, e.after.owned_by, e.after.last_edited_by)
        for e in drive.store.events()
        if isinstance(e.after, DocumentSnapshot) and e.after.title in ("Ines Draft", "Robot Report")
    }
    grants = [
        (e.after.document, e.after.to, e.after.person)
        for e in drive.store.events()
        if isinstance(e.after, GrantSnapshot)
    ]

    assert owners["Ines Draft"] == ("ines@example.com", "ines", "ines@example.com")
    assert owners["Robot Report"] == (None, "svc", "Reporting Robot")
    assert ("Ines Draft", "mara@example.com", "mara") in grants and ("Team", "ines@example.com", "ines") in grants
    assert world.person_key(INES_PERMISSION) == "ines" and world.person_key("0") is None


def test_a_change_by_a_person_without_email_names_them_without_an_address(drive: Drive) -> None:
    edited = DocumentHappening(person="svc", document="Ines Draft", after=timedelta(hours=1), action=Edited(append="x"))
    drive.provider.change(edited, PEOPLE_AND_IDS, drive.store, drive.clock)
    stored = DriveWorld(drive.store).file(DECLARED_DOC)
    assert stored is not None and stored.file.lastModifyingUser is not None
    assert stored.file.lastModifyingUser.emailAddress is None
    assert stored.file.lastModifyingUser.permissionId == state.person_permission_id("svc")


def _with(**update: object) -> Scenario:
    return Scenario.model_validate({**PEOPLE_AND_IDS.model_dump(), **update})


def test_sharing_a_document_with_a_person_without_email_is_refused(tmp_path: Path) -> None:
    documents = [
        SeededDocument(provider="google_drive", title="Shared", shared_with=[Access(person="svc")]).model_dump()
    ]
    with pytest.raises(ValueError, match=r"'Shared' is shared with svc, who has no email"):
        _seeded(tmp_path, _with(documents=documents, spaces=[]))


def test_a_shared_drive_member_without_email_is_refused(tmp_path: Path) -> None:
    spaces = [SharedSpace(provider="google_drive", name="Ops", members=[Access(person="svc")]).model_dump()]
    with pytest.raises(ValueError, match=r"svc is a member of the shared drive 'Ops' and has no email"):
        _seeded(tmp_path, _with(documents=[], spaces=spaces))


def test_a_shared_happening_to_a_person_without_email_is_refused(tmp_path: Path) -> None:
    shared = DocumentHappening(
        person="mara", document="Robot Report", after=timedelta(hours=1), action=Shared(access=Access(person="svc"))
    )
    with pytest.raises(ValueError, match=r"mara shares 'Robot Report' with svc, who has no email"):
        _seeded(tmp_path, _with(happenings=[shared.model_dump()]))


def test_signing_in_as_a_person_without_email_is_refused(tmp_path: Path) -> None:
    sign_ins = [SignIn(provider="google_drive", credential="refresh-svc", person="svc").model_dump()]
    with pytest.raises(ValueError, match=r"a google_drive sign-in is svc, who has no email"):
        _seeded(tmp_path, _with(sign_ins=sign_ins))
    with pytest.raises(ValueError, match=r"every credential signs in as the scenario's owner, svc"):
        _seeded(tmp_path, _with(owner="svc"))


@pytest.mark.parametrize("declared", ["short", "has a space in it", "slash/in/the/middle/oops"])
def test_a_document_id_not_in_drives_shape_is_refused(tmp_path: Path, declared: str) -> None:
    documents = [SeededDocument(provider="google_drive", title="Odd", id=declared).model_dump()]
    with pytest.raises(ValueError, match=r"declares the Drive id .* which is not one"):
        _seeded(tmp_path, _with(documents=documents, spaces=[]))


def test_a_permission_id_that_is_not_digits_is_refused(tmp_path: Path) -> None:
    people = [p.model_dump() for p in PEOPLE_AND_IDS.people[:1]]
    people.append(
        Person(
            key="kai", name="Kai", email="kai@example.com", accounts=[PersonAccount(provider="google_drive", id="u-1")]
        ).model_dump()
    )
    with pytest.raises(ValueError, match=r"kai's google_drive account declares the id 'u-1'"):
        _seeded(tmp_path, _with(people=people, documents=[], spaces=[]))


def test_a_declared_document_id_another_seeded_document_already_has_is_refused(tmp_path: Path) -> None:
    root = state.root_id("mara@example.com")
    taken = state.seeded_file_id(state.SEEDED_FOLDER_DEPTH, root, "First")
    documents = [
        SeededDocument(provider="google_drive", title="First").model_dump(),
        SeededDocument(provider="google_drive", title="Second", id=taken).model_dump(),
    ]
    with pytest.raises(ValueError, match=r"'Second' would take the Drive id .*, which is already taken"):
        _seeded(tmp_path, _with(documents=documents, spaces=[]))


def test_undeclared_ids_are_the_ones_derived_before(tmp_path: Path) -> None:
    documents = [SeededDocument(provider="google_drive", title="First").model_dump()]
    store = _seeded(tmp_path, _with(documents=documents, spaces=[]))
    root = state.root_id("mara@example.com")
    assert DriveWorld(store).seeded("First") == state.seeded_file_id(state.SEEDED_FOLDER_DEPTH, root, "First")
    assert DriveWorld(store).user("mara@example.com") is not None
