"""The Drive a scenario starts with: My Drive, its people, their access, and the scenario's documents.

- My Drive is owned by the scenario's owner.
- Every person is a user; the owner holds My Drive and everyone else, the agent
  included, is granted `writer` on it, which every file below inherits.
- Each `SeededDocument` for this provider is a Google Doc owned by the owner, in its
  `folder` (a folder under My Drive, made the first time a document names it) or in
  My Drive itself.

Everything is written as actor SCENARIO, stamped with the store's clock, which is at
the scenario's start while it is being set up.
"""

from __future__ import annotations

from minutehand.adapters.providers.google_drive import state, wire
from minutehand.adapters.providers.google_drive.manifest import MANIFEST
from minutehand.adapters.providers.google_drive.state import ROOT_ID, ROOT_NAME, DriveWorld
from minutehand.domain.scenario import Person, Scenario
from minutehand.domain.world import Actor, Operation
from minutehand.ports.store import Store


def _user(person: Person) -> wire.DriveUser:
    return wire.DriveUser(
        displayName=person.name, emailAddress=person.email, permissionId=state.permission_id(person.email),
    )


def _file(
    file_id: str, name: str, mime_type: str, *, parent: str | None, owner: wire.DriveUser, stamp: str, version: int,
    content: wire.DocText | None,
) -> wire.StoredFile:
    return wire.StoredFile(
        file=wire.DriveFile(
            id=file_id, name=name, mimeType=mime_type, parents=[parent] if parent is not None else None,
            owners=[owner], createdTime=stamp, modifiedTime=stamp, version=str(version),
            webViewLink=wire.web_view_link(file_id, mime_type),
        ),
        content=content,
    )


def seed(scenario: Scenario, world: Store) -> None:
    drive = DriveWorld(world)
    stamp = wire.rfc3339(scenario.starts_at)
    owner_person = next(p for p in scenario.people if p.key == scenario.owner)
    owner = _user(owner_person)

    for user in [*(_user(p) for p in scenario.people), state.agent()]:
        drive.write_user(user, actor=Actor.SCENARIO)

    root = _file(ROOT_ID, ROOT_NAME, wire.FOLDER, parent=None, owner=owner, stamp=stamp,
                 version=drive.next_seq(), content=None)
    drive.write_file(root, operation=Operation.CREATE, actor=Actor.SCENARIO)

    grantees = [_user(p) for p in scenario.people if p.key != scenario.owner] + [state.agent()]
    for grantee in grantees:
        assert grantee.emailAddress is not None
        drive.write_grant(ROOT_ID, wire.Permission(
            id=grantee.permissionId, type="user", role="writer", emailAddress=grantee.emailAddress,
            displayName=grantee.displayName,
        ), operation=Operation.CREATE, actor=Actor.SCENARIO)

    folders: dict[str, str] = {}
    for document in scenario.documents:
        if document.provider != MANIFEST.key:
            continue
        parent = ROOT_ID
        if document.folder is not None:
            if document.folder not in folders:
                seq = drive.next_seq()
                folder = _file(state.file_id(seq), document.folder, wire.FOLDER, parent=ROOT_ID, owner=owner,
                               stamp=stamp, version=seq, content=None)
                drive.write_file(folder, operation=Operation.CREATE, actor=Actor.SCENARIO)
                folders[document.folder] = folder.file.id
            parent = folders[document.folder]
        seq = drive.next_seq()
        made = _file(state.file_id(seq), document.title, wire.DOCUMENT, parent=parent, owner=owner, stamp=stamp,
                     version=seq, content=wire.DocText(text=document.text))
        drive.write_file(made, operation=Operation.CREATE, actor=Actor.SCENARIO)
