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
- A person with no email, or whose `google_drive` account hides it, is a user others are shown without an
  `emailAddress`, as Drive shows a user whose address is not visible to the requester. Their account may declare
  its display name and its permission id. What Drive cannot do for an account with no address is refused, naming
  it: sharing with them (a `user` permission needs an `emailAddress`), and signing in as them.
- A document or shared space may declare its Drive id (`SeededDocument.id`, `SharedSpace.id`); an id that is not
  Drive's shape, or that something else seeded already has, is refused.
- Its own seed (`DriveSeed`, the scenario's `ProviderSeed` for `google_drive`) declares the faults: the next
  `times` calls of a Google operation, from `after` on, refused with a `wire.FaultKind`.

Everything is written as actor SCENARIO.
"""

from __future__ import annotations

import re
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
from minutehand.domain.scenario import (
    DocumentHappening,
    DocumentKind,
    Model,
    Person,
    Scenario,
    SeededDocument,
    Shared,
)
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


DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
"""The shape of a Drive file or shared drive id: letters, digits, `-` and `_` (observed; Drive documents no grammar,
and its `files.generateIds` hands out ids of this alphabet)."""
PERMISSION_ID = re.compile(r"^[0-9]{1,21}$")
"""The shape of a user's permission id as Drive shows it on a `User` and a `Permission`: digits (observed)."""


def user_of(person: Person) -> wire.DriveUser:
    """The user a person is, with the address they sign in with when they have one."""
    account = person.account_in(MANIFEST.key)
    if account is not None and account.id is not None:
        if PERMISSION_ID.fullmatch(account.id) is None:
            raise ValueError(
                f"{person.key}'s google_drive account declares the id {account.id!r}, and a Drive permission id is "
                "digits"
            )
        permission = account.id
    elif person.email is not None:
        permission = state.permission_id(person.email)
    else:
        permission = state.person_permission_id(person.key)
    name = account.name if account is not None and account.name is not None else person.name
    return wire.DriveUser(displayName=name, emailAddress=person.email, permissionId=permission)


def shown(person: Person, user: wire.DriveUser) -> wire.DriveUser:
    """The user as others are shown them: without `emailAddress` when the person has none or it is hidden."""
    return user.model_copy(update={"emailAddress": person.shown_email(MANIFEST.key)})


def _declared(kind: str, title: str, declared: str) -> str:
    if DRIVE_ID.fullmatch(declared) is None:
        raise ValueError(
            f"the {kind} {title!r} declares the Drive id {declared!r}, which is not one: letters, digits, '-' and "
            "'_', 10 to 100 of them"
        )
    return declared


def _refuse_without_email(scenario: Scenario, people: dict[str, Person]) -> None:
    """What Drive cannot do for an account with no address, refused before anything is written."""

    def addressless(key: str) -> bool:
        return people[key].email is None

    for space in (s for s in scenario.spaces if s.provider == MANIFEST.key):
        for member in space.members:
            if addressless(member.person):
                raise ValueError(
                    f"{member.person} is a member of the shared drive {space.name!r} and has no email; Drive grants "
                    "a user access by their emailAddress"
                )
    for document in (d for d in scenario.documents if d.provider == MANIFEST.key):
        for access in document.shared_with:
            if addressless(access.person):
                raise ValueError(
                    f"{document.title!r} is shared with {access.person}, who has no email; Drive shares with a "
                    "user by their emailAddress"
                )
    ours = {d.title for d in scenario.documents if d.provider == MANIFEST.key}
    for happening in scenario.happenings:
        if (
            isinstance(happening, DocumentHappening)
            and happening.document in ours
            and isinstance(happening.action, Shared)
            and addressless(happening.action.access.person)
        ):
            raise ValueError(
                f"{happening.person} shares {happening.document!r} with {happening.action.access.person}, who has no "
                "email; Drive shares with a user by their emailAddress"
            )
    for sign_in in (s for s in scenario.sign_ins if s.provider == MANIFEST.key):
        if sign_in.person is not None and addressless(sign_in.person):
            raise ValueError(
                f"a google_drive sign-in is {sign_in.person}, who has no email; a Google account signs in by its "
                "address"
            )
    if not any(s.provider == MANIFEST.key for s in scenario.sign_ins) and addressless(scenario.owner):
        raise ValueError(
            f"with no google_drive sign-in every credential signs in as the scenario's owner, {scenario.owner}, who "
            "has no email; a Google account signs in by its address: declare a sign-in"
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
    file_id = (
        _declared("document", document.title, document.id)
        if document.id is not None
        else state.seeded_file_id(ordinal + state.SEEDED_FOLDER_DEPTH, parent.file.id, document.title)
    )
    if drive.file(file_id) is not None:
        raise ValueError(f"the document {document.title!r} would take the Drive id {file_id}, which is already taken")
    blob = (
        drive.keep_blob(document.text.encode("utf-8"), actor=Actor.SCENARIO)
        if document.kind is DocumentKind.FILE
        else None
    )
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
    _refuse_without_email(scenario, people)
    accounts = {p.key: user_of(p) for p in scenario.people}
    users = {p.key: shown(p, accounts[p.key]) for p in scenario.people}
    sign_ins = [s for s in scenario.sign_ins if s.provider == MANIFEST.key]
    robots = [_robot(s.credential) for s in sign_ins if s.person is None]
    permissions = [u.permissionId for u in [*accounts.values(), *robots]]
    twice = sorted({p for p in permissions if permissions.count(p) > 1})
    if twice:
        raise ValueError(f"two Drive users would share the permission id {', '.join(twice)}")

    for key, user in users.items():
        drive.keep_person(key, user)
    for account in [*accounts.values(), *robots]:
        drive.write_user(account, actor=Actor.SCENARIO)
        root = folder_file(
            state.user_root(account),
            ROOT_NAME,
            parent=None,
            owner=next((u for u in users.values() if u.permissionId == account.permissionId), account),
            drive_id=None,
            stamp=stamp,
            version=state.SEEDED_VERSION,
        )
        drive.write_file(root, operation=Operation.CREATE, actor=Actor.SCENARIO)

    def address(key: str) -> str:
        email = people[key].email
        assert email is not None, "refused above: a sign-in is never a person with no email"
        return email

    if not sign_ins:
        drive.keep_credential(
            state.ANY_CREDENTIAL, wire.Credential(email=address(scenario.owner)), operation=Operation.CREATE
        )
    for sign_in in sign_ins:
        email = address(sign_in.person) if sign_in.person is not None else sign_in.credential
        credential = wire.Credential(email=email, service_account="@" in sign_in.credential)
        drive.keep_credential(state.secret_digest(sign_in.credential), credential, operation=Operation.CREATE)

    spaces: dict[str, wire.StoredFile] = {}
    for position, space in enumerate(scenario.spaces):
        if space.provider != MANIFEST.key:
            continue
        drive_id = (
            _declared("shared drive", space.name, space.id)
            if space.id is not None
            else state.seeded_drive_id(position, space.name)
        )
        if drive.file(drive_id) is not None:
            raise ValueError(f"the shared drive {space.name!r} would take the Drive id {drive_id}, which is taken")
        drive.write_drive(wire.SharedDrive(id=drive_id, name=space.name, createdTime=stamp), actor=Actor.SCENARIO)
        root = folder_file(
            drive_id, space.name, parent=None, owner=None, drive_id=drive_id, stamp=stamp, version=state.SEEDED_VERSION
        )
        drive.write_file(root, operation=Operation.CREATE, actor=Actor.SCENARIO)
        for member in space.members:
            grant(drive, root, accounts[member.person], ROLES[member.role], actor=Actor.SCENARIO)
        spaces[space.name] = root

    for position, document in enumerate(scenario.documents):
        if document.provider != MANIFEST.key:
            continue
        owner_key = document.owner or scenario.owner
        owner = users[owner_key]
        root = (
            spaces[document.space] if document.space is not None else drive.file(state.user_root(accounts[owner_key]))
        )
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
            grant(drive, made, accounts[access.person], ROLES[access.role], actor=Actor.SCENARIO)

    write_faults(drive, drive_seed(scenario).faults, start)


DECLARED = 1_000_000
"""Where the numbers of faults declared on an open world (`provider-faults`) start: above every number a seed gives
its own faults, which count from 0 in the seed's order, so a fault a later seed fragment adds never takes the
number of one declared before it, and the seed's are armed ahead of the declared ones."""


def write_faults(drive: DriveWorld, faults: list[FaultSeed], start: datetime, *, declared: bool = False) -> None:
    """Record each fault after those already recorded, from `start` plus its own offset."""
    first = (DECLARED if declared else 0) + len(drive.faults())
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
