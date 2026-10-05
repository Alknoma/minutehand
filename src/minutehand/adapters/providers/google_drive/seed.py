"""The Drive a scenario starts with: everyone's My Drive, the shared drives, the documents, who may see them,
how the agent signs in, and the faults the scenario declared.

- Every person is a user with a My Drive of their own; so is every sign-in that is not a person's (a service
  account), named by its credential.
- A `SharedSpace` is a shared drive whose members are granted on its root.
- Each `SeededDocument` is a file owned by its owner (the scenario's owner when it names none), in its owner's
  My Drive or its shared drive, under its folder path, made the first time a path names it. A DOCUMENT is a
  Google Doc whose text is read as Markdown; a SPREADSHEET a Google Sheet of its rows; a PRESENTATION a Google
  Slides deck; a FILE an uploaded file of its `mime_type`. It was last changed `modified_before_start`
  before the scenario starts, by `modified_by`.
- With no `SignIn` for this provider, any credential signs in as the scenario's owner.
- Its own seed (`DriveSeed`, the scenario's `ProviderSeed` for `google_drive`) declares the faults: the next
  `times` calls of a Google operation, from `after` on, refused with a `wire.FaultKind`.

Everything is written as actor SCENARIO.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field

from minutehand.adapters.providers.google_drive import docs, slides, state, wire
from minutehand.adapters.providers.google_drive.app import OPERATIONS
from minutehand.adapters.providers.google_drive.manifest import MANIFEST
from minutehand.adapters.providers.google_drive.state import (
    ROLES,
    ROOT_NAME,
    DriveWorld,
    ensure_folder,
    folder_file,
    grant,
)
from minutehand.domain.scenario import DocumentKind, Model, Person, Scenario, SeededDocument
from minutehand.domain.world import Actor, Operation
from minutehand.ports.store import Store


class FaultSeed(Model):
    """The next `times` calls of `operation`, from `after` on, are refused with `kind`."""

    operation: str = Field(min_length=1, description="Google's own name for the call, e.g. 'documents.get'")
    kind: wire.FaultKind
    times: int = Field(default=1, ge=1)
    after: timedelta = Field(default=timedelta(0), ge=timedelta(0), description="Offset from the scenario's start")


class DriveSeed(Model):
    """What only Drive seeds, as the body of the scenario's `ProviderSeed` for `google_drive`."""

    faults: list[FaultSeed] = []


def drive_seed(scenario: Scenario) -> DriveSeed:
    found = scenario.provider_seed(MANIFEST.key)
    return DriveSeed() if found is None else DriveSeed.model_validate_json(found.body)


def user_of(person: Person) -> wire.DriveUser:
    return wire.DriveUser(
        displayName=person.name, emailAddress=person.email, permissionId=state.permission_id(person.email)
    )


def _robot(credential: str) -> wire.DriveUser:
    return wire.DriveUser(
        displayName=credential.split("@", 1)[0], emailAddress=credential, permissionId=state.permission_id(credential)
    )


def _seed_document(
    drive: DriveWorld,
    document: SeededDocument,
    *,
    root: wire.StoredFile,
    owner: wire.DriveUser,
    modifier: wire.DriveUser,
    stamp: str,
    changed: str,
    position: int,
) -> wire.StoredFile:
    ordinal = position * (state.SEEDED_FOLDER_DEPTH + 1)
    parent = ensure_folder(drive, root, document.folder, owner, stamp, Actor.SCENARIO, seeded=ordinal)
    blob = (
        drive.keep_blob(document.text.encode("utf-8"), actor=Actor.SCENARIO)
        if document.kind is DocumentKind.FILE
        else None
    )
    file_id = state.seeded_file_id(ordinal + state.SEEDED_FOLDER_DEPTH, parent.file.id, document.title)
    content: wire.Content | None
    if document.kind is DocumentKind.DOCUMENT:
        mime, content = wire.DOCUMENT, docs.from_markdown(file_id, document.text)
    elif document.kind is DocumentKind.SPREADSHEET:
        mime, content = wire.SPREADSHEET, wire.Sheet(rows=document.rows)
    elif document.kind is DocumentKind.PRESENTATION:
        mime, content = wire.PRESENTATION, slides.deck_from_text(file_id, document.text)
    else:
        mime, content = document.mime_type or "text/plain", blob
    made = wire.StoredFile(
        file=wire.DriveFile(
            id=file_id,
            name=document.title,
            mimeType=mime,
            parents=[parent.file.id],
            owners=[owner] if root.file.driveId is None else None,
            createdTime=changed,
            modifiedTime=changed,
            version=str(state.SEEDED_VERSION),
            webViewLink=wire.web_view_link(file_id, mime),
            size=str(blob.size) if blob is not None else None,
            md5Checksum=blob.md5 if blob is not None else None,
            driveId=root.file.driveId,
            lastModifyingUser=modifier,
            shared=True if document.shared_with else None,
        ),
        content=content,
    )
    drive.write_file(made, operation=Operation.CREATE, actor=Actor.SCENARIO)
    return made


def seed(scenario: Scenario, world: Store) -> None:
    drive = DriveWorld(world)
    start = scenario.starts_at
    stamp = wire.rfc3339(start)
    people = {p.key: p for p in scenario.people}
    users = {p.key: user_of(p) for p in scenario.people}
    sign_ins = [s for s in scenario.sign_ins if s.provider == MANIFEST.key]
    robots = [_robot(s.credential) for s in sign_ins if s.person is None]

    for key, user in users.items():
        drive.keep_person(key, user)
    for user in [*users.values(), *robots]:
        assert user.emailAddress is not None
        drive.write_user(user, actor=Actor.SCENARIO)
        root = folder_file(
            state.root_id(user.emailAddress),
            ROOT_NAME,
            parent=None,
            owner=user,
            drive_id=None,
            stamp=stamp,
            version=state.SEEDED_VERSION,
        )
        drive.write_file(root, operation=Operation.CREATE, actor=Actor.SCENARIO)

    if not sign_ins:
        owner = people[scenario.owner]
        drive.keep_credential(state.ANY_CREDENTIAL, wire.Credential(email=owner.email), operation=Operation.CREATE)
    for sign_in in sign_ins:
        email = people[sign_in.person].email if sign_in.person is not None else sign_in.credential
        credential = wire.Credential(email=email, service_account="@" in sign_in.credential)
        drive.keep_credential(state.secret_digest(sign_in.credential), credential, operation=Operation.CREATE)

    spaces: dict[str, wire.StoredFile] = {}
    for position, space in enumerate(scenario.spaces):
        if space.provider != MANIFEST.key:
            continue
        drive_id = state.seeded_drive_id(position, space.name)
        drive.write_drive(wire.SharedDrive(id=drive_id, name=space.name, createdTime=stamp), actor=Actor.SCENARIO)
        root = folder_file(
            drive_id, space.name, parent=None, owner=None, drive_id=drive_id, stamp=stamp, version=state.SEEDED_VERSION
        )
        drive.write_file(root, operation=Operation.CREATE, actor=Actor.SCENARIO)
        for member in space.members:
            grant(drive, root, users[member.person], ROLES[member.role], actor=Actor.SCENARIO)
        spaces[space.name] = root

    for position, document in enumerate(scenario.documents):
        if document.provider != MANIFEST.key:
            continue
        owner = users[document.owner or scenario.owner]
        assert owner.emailAddress is not None
        root = spaces[document.space] if document.space is not None else drive.file(state.root_id(owner.emailAddress))
        assert root is not None
        made = _seed_document(
            drive,
            document,
            root=root,
            owner=owner,
            modifier=users[document.modified_by] if document.modified_by else owner,
            stamp=stamp,
            changed=wire.rfc3339(start - document.modified_before_start),
            position=position,
        )
        drive.keep_seeded(document.title, made.file.id)
        for access in document.shared_with:
            grant(drive, made, users[access.person], ROLES[access.role], actor=Actor.SCENARIO)

    write_faults(drive, drive_seed(scenario).faults, start)


def write_faults(drive: DriveWorld, faults: list[FaultSeed], start: datetime) -> None:
    """Record each fault after those already recorded, from `start` plus its own offset."""
    first = len(drive.faults())
    for fault in faults:
        if fault.operation not in OPERATIONS:
            raise ValueError(
                f"a fault names {fault.operation!r}, which is not a Google Drive, Docs or Slides call this simulation "
                f"answers; it answers {', '.join(sorted(OPERATIONS))}"
            )
    for position, fault in enumerate(faults, start=first):
        stored = wire.StoredFault(
            operation=fault.operation,
            kind=fault.kind,
            after=wire.rfc3339(start + fault.after),
            remaining=fault.times,
        )
        drive.keep_fault(f"fault{position:04d}", stored, operation=Operation.CREATE)
