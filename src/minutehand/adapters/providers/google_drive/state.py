"""Google Drive as entities in the run's store.

| Drive thing                       | `EntityKind` | external id                  | parent                  |
|-----------------------------------|--------------|------------------------------|-------------------------|
| a My Drive or a shared drive root | DOCUMENT     | `root_id(email)`, the drive id | None                  |
| file or folder                    | DOCUMENT     | minted from its event seq    | its one parent folder   |
| comment                           | COMMENT      | minted from its event seq    | the file                |
| permission granted                | RECORD       | `<file id>.<permission id>`  | the file                |
| user                              | RECORD       | the user's permission id     | `USERS`                 |
| shared drive                      | RECORD       | the drive id                 | `DRIVES`                |
| a file's bytes                    | RECORD       | their SHA-256                | `BLOBS`                 |
| a credential the run signs in     | RECORD       | its SHA-256                  | `CREDENTIALS`           |
| an access token issued            | RECORD       | its SHA-256                  | `TOKENS`                |
| a `changes.watch` channel         | RECORD       | the channel id               | `CHANNELS`              |
| a fault the scenario declared     | RECORD       | its position                 | `FAULTS`                |
| a resumable upload in progress    | RECORD       | its upload id                | `UPLOADS`               |
| the file a seeded document became | RECORD       | SHA-256 of its title         | `SEEDED`                |
| a scenario person, by `Person.key`| RECORD       | `person.<key>`               | `PEOPLE`                |

A file's metadata and its structured content (a Doc, a deck, a sheet) are one entity, so a version of the
file is both, read as of any sequence. Binary bytes are their own entity, written once. Nothing is held
between calls: a new app over the same store sees the same Drive, and a fork sees it as of the fork.

**Who sees what.** Each user has a My Drive of their own. A file in a My Drive is seen by its owner and by
whoever is granted it, on the file or a folder above it; a file in a shared drive by the drive's members
and whoever is granted it. A grant to `anyone` lets anyone open the file but finds it in nobody's search
unless it allows file discovery, as in Drive.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime

from minutehand.adapters.providers.google_drive import docs, slides, wire
from minutehand.adapters.providers.google_drive.manifest import MANIFEST
from minutehand.domain.scenario import AccessRole, Model
from minutehand.domain.world import (
    Actor,
    Change,
    DocumentSnapshot,
    EntityKind,
    EntityRef,
    GrantSnapshot,
    Operation,
    RecordSnapshot,
    Stored,
    WorldEvent,
)
from minutehand.ports.store import Store

ROOT_ALIAS = "root"
"""Drive's word for the caller's own My Drive in a file id or an `in parents` term."""
ROOT_NAME = "My Drive"
USERS = "users"
DRIVES = "drives"
BLOBS = "blobs"
CREDENTIALS = "credentials"
TOKENS = "tokens"
CHANNELS = "channels"
FAULTS = "faults"
UPLOADS = "uploads"
SEEDED = "seeded"
PEOPLE = "people"
ANY_CREDENTIAL = "*"
"""The credential entity a scenario that names no sign-in has: any refresh token or service account signs in as
the scenario's owner."""

ROLE_RANK = {"reader": 1, "commenter": 2, "writer": 3, "fileOrganizer": 4, "organizer": 5, "owner": 6}

_SCAN = 1000
_MAX_SEQ = 99_999_999


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def secret_digest(secret: str) -> str:
    """What a token or credential is kept under: never the secret itself."""
    return _digest("secret", secret)


def _minted(prefix: str, seq: int) -> str:
    if seq > _MAX_SEQ:
        raise OverflowError(f"event {seq} no longer fits the eight digits of a minted id")
    return f"{prefix}{seq:08d}{_digest(prefix, str(seq))[:20]}"


def file_id(seq: int) -> str:
    """The id of the file written by event `seq`: ordered by creation, the same in every run that reaches it."""
    return _minted("1F", seq)


def comment_id(seq: int) -> str:
    return _minted("AAAB", seq)


def drive_id(seq: int) -> str:
    return _minted("0AD", seq)[:19]


def root_id(email: str) -> str:
    """A user's My Drive: one per address, the same in every run."""
    return "0A" + _digest("root", email.lower())[:17].upper()


def permission_id(email: str) -> str:
    """A user's permission id: one per address, the same on every file, as Drive's is."""
    return str(int(_digest("permission", email.lower())[:15], 16)).zfill(20)


def domain_permission_id(domain: str) -> str:
    return str(int(_digest("domain", domain.lower())[:15], 16)).zfill(20)


ANYONE_PERMISSION_ID = "anyoneWithLink"


def _ref(kind: EntityKind, external_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=kind, external_id=external_id)


def file_ref(file: str) -> EntityRef:
    return _ref(EntityKind.DOCUMENT, file)


def comment_ref(comment: str) -> EntityRef:
    return _ref(EntityKind.COMMENT, comment)


def grant_ref(file: str, permission: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"{file}.{permission}")


def user_ref(permission: str) -> EntityRef:
    return _ref(EntityKind.RECORD, permission)


def record_ref(external_id: str) -> EntityRef:
    return _ref(EntityKind.RECORD, external_id)


def snapshot(stored: wire.StoredFile, *, space: str | None = None) -> DocumentSnapshot:
    """The file as every document provider tells it: title, type, its text where Drive holds it as text (a Doc, a
    deck, a sheet), who last changed it and who owns it, by email, and `space`, the name of the shared drive it is
    in (None in its owner's My Drive, where a shared drive owns nothing and a file is owned by a person)."""
    editor = stored.file.lastModifyingUser
    owners = stored.file.owners or []
    return DocumentSnapshot(
        owner=owners[0].emailAddress if owners else None,
        space=space,
        title=stored.file.name,
        mime_type=stored.file.mimeType,
        text=readable_text(stored) or None,
        last_edited_by=(editor.emailAddress or editor.displayName) if editor is not None else None,
        last_edited_at=datetime.fromisoformat(stored.file.modifiedTime.replace("Z", "+00:00")),
    )


def readable_text(stored: wire.StoredFile, blob: bytes | None = None) -> str:
    """The text a search reads: a Doc's, a deck's or a sheet's text, or a text file's bytes as UTF-8."""
    content = stored.content
    if isinstance(content, docs.DocBody):
        return docs.text_of(content)
    if isinstance(content, slides.Deck):
        return slides.text_of(content)
    if isinstance(content, wire.Sheet):
        return "\n".join("\t".join(row) for row in content.rows)
    if blob is not None and stored.file.mimeType.startswith("text/"):
        return blob.decode("utf-8", errors="replace")
    return ""


class DriveWorld:
    """Typed reads and writes of one run's Drive entities."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @property
    def store(self) -> Store:
        return self._store

    def next_seq(self) -> int:
        """The sequence number of the event about to be written; what a new file's id and version come from."""
        return self._store.head() + 1

    # ------------------------------------------------------------------ records

    def _record[
        T: (
            wire.SharedDrive,
            wire.Credential,
            wire.AccessToken,
            wire.Channel,
            wire.StoredFault,
            wire.UploadSession,
            wire.Blob,
            wire.DriveUser,
            wire.SeededFile,
        )
    ](self, model: type[T], external_id: str, parent: str) -> T | None:
        stored = self._store.get(record_ref(external_id))
        if stored is None or stored.parent != parent:
            return None
        return wire.parse(model, stored.body)

    def _records(self, parent: str) -> Iterator[Stored]:
        yield from self._pages(EntityKind.RECORD, parent)

    def _keep(
        self,
        external_id: str,
        parent: str,
        body: Model,
        *,
        operation: Operation = Operation.UPDATE,
        actor: Actor = Actor.SCENARIO,
    ) -> WorldEvent:
        return self._store.apply(
            Change(
                entity=record_ref(external_id), operation=operation, actor=actor, body=wire.dump(body), parent=parent
            )
        )

    # ------------------------------------------------------------------ files

    def file(self, file: str) -> wire.StoredFile | None:
        stored = self._store.get(file_ref(file))
        return None if stored is None else wire.parse(wire.StoredFile, stored.body)

    def file_as_of(self, file: str, before_seq: int) -> wire.StoredFile | None:
        """The last version of a file written before `before_seq`: what a deleted file was."""
        versions = [v for v in self._store.versions(file_ref(file)) if v.seq < before_seq]
        return wire.parse(wire.StoredFile, versions[-1].body) if versions else None

    def _pages(self, kind: EntityKind, parent: str) -> Iterator[Stored]:
        after: str | None = None
        while True:
            page = self._store.children(MANIFEST.key, kind, parent, after=after, limit=_SCAN)
            yield from page
            if len(page) < _SCAN:
                return
            after = page[-1].entity.external_id

    def children(self, folder: str) -> Iterator[wire.StoredFile]:
        for stored in self._pages(EntityKind.DOCUMENT, folder):
            yield wire.parse(wire.StoredFile, stored.body)

    def roots(self) -> list[str]:
        """Every My Drive and every shared drive."""
        found = [root_id(user.emailAddress) for user in self.users() if user.emailAddress]
        found += [d.id for d in self.drives()]
        return [r for r in found if self.file(r) is not None]

    def walk(self, root: str) -> Iterator[tuple[wire.StoredFile, bool]]:
        """Every file under one root, depth first, with whether a folder above it is in the trash."""
        pending: list[tuple[str, bool]] = [(root, False)]
        while pending:
            folder, trashed_above = pending.pop()
            for child in self.children(folder):
                yield child, trashed_above
                if child.file.mimeType == wire.FOLDER:
                    pending.append((child.file.id, trashed_above or child.file.trashed))

    def ancestors(self, stored: wire.StoredFile) -> list[wire.StoredFile]:
        """The folders above a file, nearest first, up to and including its root."""
        chain: list[wire.StoredFile] = []
        seen = {stored.file.id}
        parent = stored.file.parents[0] if stored.file.parents else None
        while parent is not None and parent not in seen:
            seen.add(parent)
            above = self.file(parent)
            if above is None:
                break
            chain.append(above)
            parent = above.file.parents[0] if above.file.parents else None
        return chain

    def trashed(self, stored: wire.StoredFile) -> bool:
        """Drive's `trashed`: set on the file, or on any folder above it."""
        return stored.file.trashed or any(above.file.trashed for above in self.ancestors(stored))

    def write_file(self, stored: wire.StoredFile, *, operation: Operation, actor: Actor) -> WorldEvent:
        parents = stored.file.parents
        return self._store.apply(
            Change(
                entity=file_ref(stored.file.id),
                operation=operation,
                actor=actor,
                body=wire.dump(stored),
                parent=parents[0] if parents else None,
                after=snapshot(stored, space=self._space(stored)),
            )
        )

    def _space(self, stored: wire.StoredFile) -> str | None:
        found = self.drive(stored.file.driveId) if stored.file.driveId is not None else None
        return found.name if found is not None else None

    def delete_file(self, stored: wire.StoredFile, *, actor: Actor) -> list[WorldEvent]:
        """Delete a file and, for a folder, everything under it, deepest first."""
        events: list[WorldEvent] = []
        if stored.file.mimeType == wire.FOLDER:
            for child in list(self.children(stored.file.id)):
                events += self.delete_file(child, actor=actor)
        parents = stored.file.parents
        events.append(
            self._store.apply(
                Change(
                    entity=file_ref(stored.file.id),
                    operation=Operation.DELETE,
                    actor=actor,
                    parent=parents[0] if parents else None,
                )
            )
        )
        return events

    # ------------------------------------------------------------------ bytes

    def blob(self, ref: wire.BlobRef) -> bytes:
        found = self._record(wire.Blob, ref.digest, BLOBS)
        if found is None:
            raise LookupError(f"the bytes {ref.digest} of a file are not in the world")
        return wire.blob_bytes(found)

    def keep_blob(self, content: bytes, *, actor: Actor) -> wire.BlobRef:
        found = wire.digest(content)
        if self._store.get(record_ref(found)) is None:
            self._keep(found, BLOBS, wire.blob(content), operation=Operation.CREATE, actor=actor)
        return wire.BlobRef(digest=found, size=len(content), md5=wire.md5(content))

    # ------------------------------------------------------------------ people and access

    def user(self, email: str) -> wire.DriveUser | None:
        return self._record(wire.DriveUser, permission_id(email), USERS)

    def users(self) -> list[wire.DriveUser]:
        return [wire.parse(wire.DriveUser, s.body) for s in self._records(USERS)]

    def write_user(self, user: wire.DriveUser, *, actor: Actor) -> WorldEvent:
        return self._keep(user.permissionId, USERS, user, operation=Operation.CREATE, actor=actor)

    def grants(self, file: str) -> list[wire.Permission]:
        return [wire.parse(wire.Permission, s.body) for s in self._pages(EntityKind.RECORD, file)]

    def write_grant(self, file: str, permission: wire.Permission, *, operation: Operation, actor: Actor) -> WorldEvent:
        who = permission.emailAddress or permission.domain or permission.type
        held = self.file(file)
        return self._store.apply(
            Change(
                entity=grant_ref(file, permission.id),
                operation=operation,
                actor=actor,
                body=wire.dump(permission),
                parent=file,
                after=GrantSnapshot(
                    document=held.file.name if held is not None else file, to=who, role=GRANTED[permission.role]
                ),
            )
        )

    def delete_grant(self, file: str, permission: str, *, actor: Actor) -> WorldEvent:
        return self._store.apply(
            Change(entity=grant_ref(file, permission), operation=Operation.DELETE, actor=actor, parent=file)
        )

    def role(self, email: str, stored: wire.StoredFile, *, searching: bool = False) -> str | None:
        """The most the user may do with the file: its owner, a member of its shared drive, or a grantee on it
        or a folder above it. None: the file is not theirs to see. `searching` leaves out a grant to anyone
        that does not allow discovery, which opens a file but does not put it in a search."""
        best: str | None = None
        if any(owner.emailAddress == email for owner in stored.file.owners or []):
            best = "owner"
        domain = email.rsplit("@", 1)[-1].lower()
        for holder in [stored, *self.ancestors(stored)]:
            for grant in self.grants(holder.file.id):
                if grant.type == "anyone" and searching and not grant.allowFileDiscovery:
                    continue
                if (
                    (grant.type in ("user", "group") and grant.emailAddress == email)
                    or grant.type == "anyone"
                    or (grant.type == "domain" and grant.domain is not None and grant.domain.lower() == domain)
                ) and (best is None or ROLE_RANK[grant.role] > ROLE_RANK[best]):
                    best = grant.role
        return best

    # ------------------------------------------------------------------ shared drives

    def drives(self) -> list[wire.SharedDrive]:
        return [wire.parse(wire.SharedDrive, s.body) for s in self._records(DRIVES)]

    def drive(self, drive: str) -> wire.SharedDrive | None:
        return self._record(wire.SharedDrive, drive, DRIVES)

    def write_drive(self, drive: wire.SharedDrive, *, actor: Actor) -> WorldEvent:
        return self._keep(drive.id, DRIVES, drive, operation=Operation.CREATE, actor=actor)

    # ------------------------------------------------------------------ sign-in

    def credential(self, secret: str) -> wire.Credential | None:
        """Whom a refresh token or a service account signs in as: as declared, or, in a scenario that declares
        none, the owner."""
        found = self._record(wire.Credential, secret_digest(secret), CREDENTIALS)
        if found is not None:
            return found
        return self._record(wire.Credential, ANY_CREDENTIAL, CREDENTIALS)

    def credential_key(self, secret: str) -> str:
        """The digest a credential is kept under, or the open entity's when the scenario declares none."""
        key = secret_digest(secret)
        return key if self._store.get(record_ref(key)) is not None else ANY_CREDENTIAL

    def keep_credential(
        self, key: str, credential: wire.Credential, *, operation: Operation = Operation.UPDATE
    ) -> WorldEvent:
        return self._keep(key, CREDENTIALS, credential, operation=operation)

    def token(self, token: str) -> wire.AccessToken | None:
        return self._record(wire.AccessToken, secret_digest(token), TOKENS)

    def keep_token(
        self, token: str, issued: wire.AccessToken, *, operation: Operation = Operation.CREATE
    ) -> WorldEvent:
        return self._keep(secret_digest(token), TOKENS, issued, operation=operation)

    def tokens(self) -> list[tuple[str, wire.AccessToken]]:
        return [(s.entity.external_id, wire.parse(wire.AccessToken, s.body)) for s in self._records(TOKENS)]

    def revoke_token(self, key: str, issued: wire.AccessToken) -> WorldEvent:
        return self._keep(key, TOKENS, issued.model_copy(update={"revoked": True}))

    def credential_by_key(self, key: str) -> wire.Credential | None:
        return self._record(wire.Credential, key, CREDENTIALS)

    # ------------------------------------------------------------------ channels, faults, uploads

    def channels(self) -> list[wire.Channel]:
        return [wire.parse(wire.Channel, s.body) for s in self._records(CHANNELS)]

    def channel(self, channel: str) -> wire.Channel | None:
        return self._record(wire.Channel, channel, CHANNELS)

    def keep_channel(self, channel: wire.Channel, *, operation: Operation = Operation.UPDATE) -> WorldEvent:
        return self._keep(channel.id, CHANNELS, channel, operation=operation)

    def faults(self) -> list[tuple[str, wire.StoredFault]]:
        return [(s.entity.external_id, wire.parse(wire.StoredFault, s.body)) for s in self._records(FAULTS)]

    def keep_fault(self, key: str, fault: wire.StoredFault, *, operation: Operation = Operation.UPDATE) -> WorldEvent:
        return self._keep(key, FAULTS, fault, operation=operation)

    def upload(self, upload: str) -> wire.UploadSession | None:
        return self._record(wire.UploadSession, upload, UPLOADS)

    def keep_upload(
        self,
        upload: str,
        session: wire.UploadSession,
        *,
        operation: Operation = Operation.UPDATE,
        actor: Actor = Actor.AGENT,
    ) -> WorldEvent:
        return self._keep(upload, UPLOADS, session, operation=operation, actor=actor)

    def end_upload(self, upload: str) -> WorldEvent:
        return self._store.apply(
            Change(entity=record_ref(upload), operation=Operation.DELETE, actor=Actor.AGENT, parent=UPLOADS)
        )

    # ------------------------------------------------------------------ the scenario's own names

    def keep_seeded(self, title: str, file: str) -> WorldEvent:
        return self._keep(_digest("seeded", title), SEEDED, wire.SeededFile(file_id=file), operation=Operation.CREATE)

    def seeded(self, title: str) -> str | None:
        found = self._record(wire.SeededFile, _digest("seeded", title), SEEDED)
        return found.file_id if found is not None else None

    def keep_person(self, key: str, user: wire.DriveUser) -> WorldEvent:
        return self._keep(f"person.{key}", PEOPLE, user, operation=Operation.CREATE)

    def person(self, key: str) -> wire.DriveUser | None:
        return self._record(wire.DriveUser, f"person.{key}", PEOPLE)

    # ------------------------------------------------------------------ comments

    def comments(self, file: str) -> list[wire.Comment]:
        """Every comment on a file, oldest first: comment ids are minted in event order."""
        return [wire.parse(wire.Comment, s.body) for s in self._pages(EntityKind.COMMENT, file)]

    def write_comment(self, file: str, comment: wire.Comment, *, actor: Actor) -> WorldEvent:
        return self._store.apply(
            Change(
                entity=comment_ref(comment.id),
                operation=Operation.CREATE,
                actor=actor,
                body=wire.dump(comment),
                parent=file,
                after=RecordSnapshot(resource="comment", text=comment.content),
            )
        )

    # ------------------------------------------------------------------ reads

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))


GRANTED: dict[wire.Role, AccessRole] = {
    "reader": AccessRole.READER,
    "commenter": AccessRole.COMMENTER,
    "writer": AccessRole.WRITER,
    "fileOrganizer": AccessRole.ORGANIZER,
    "organizer": AccessRole.ORGANIZER,
    "owner": AccessRole.ORGANIZER,
}
"""What access each Drive role gives, as every document provider says it: an owner organizes, at the least."""

ROLES: dict[AccessRole, wire.Role] = {
    AccessRole.READER: "reader",
    AccessRole.COMMENTER: "commenter",
    AccessRole.WRITER: "writer",
    AccessRole.ORGANIZER: "organizer",
}


def folder_file(
    file_id: str,
    name: str,
    *,
    parent: str | None,
    owner: wire.DriveUser | None,
    drive_id: str | None,
    stamp: str,
    version: int,
) -> wire.StoredFile:
    return wire.StoredFile(
        file=wire.DriveFile(
            id=file_id,
            name=name,
            mimeType=wire.FOLDER,
            parents=[parent] if parent is not None else None,
            owners=[owner] if owner is not None and drive_id is None else None,
            createdTime=stamp,
            modifiedTime=stamp,
            version=str(version),
            webViewLink=wire.web_view_link(file_id, wire.FOLDER),
            driveId=drive_id,
            lastModifyingUser=owner,
        )
    )


def ensure_folder(
    drive: DriveWorld, root: wire.StoredFile, path: str | None, owner: wire.DriveUser, stamp: str, actor: Actor
) -> wire.StoredFile:
    """The folder at `path` under `root`, made, part by part, where it is not there yet."""
    here = root
    for name in [part.strip() for part in (path or "").split("/") if part.strip()]:
        found = next(
            (c for c in drive.children(here.file.id) if c.file.mimeType == wire.FOLDER and c.file.name == name), None
        )
        if found is None:
            seq = drive.next_seq()
            found = folder_file(
                file_id(seq),
                name,
                parent=here.file.id,
                owner=owner,
                drive_id=root.file.driveId,
                stamp=stamp,
                version=seq,
            )
            drive.write_file(found, operation=Operation.CREATE, actor=actor)
        here = found
    return here


def grant(drive: DriveWorld, stored: wire.StoredFile, user: wire.DriveUser, role: wire.Role, *, actor: Actor) -> None:
    assert user.emailAddress is not None
    drive.write_grant(
        stored.file.id,
        wire.Permission(
            id=user.permissionId, type="user", role=role, emailAddress=user.emailAddress, displayName=user.displayName
        ),
        operation=Operation.CREATE,
        actor=actor,
    )
