"""The Drive as entities in the run's store.

| Drive thing          | `EntityKind` | external id                | parent                 |
|----------------------|--------------|----------------------------|------------------------|
| My Drive (the root)  | DOCUMENT     | `ROOT_ID`                  | None                   |
| file or folder       | DOCUMENT     | minted from its event seq  | its one parent folder  |
| comment              | COMMENT      | minted from its event seq  | the file               |
| permission granted   | RECORD       | `<file id>.<permission id>`| the file               |
| user                 | RECORD       | the user's permission id   | `USERS`                |

A file's content is stored inside its own entity (`wire.StoredFile.content`), so a
version of a file is its metadata and its content together, read as of any sequence.
Nothing here is held between calls: a new app over the same store sees the same
Drive, and a fork sees it as of the fork.

There is one Drive. Every caller sees every file: permissions are recorded and
listed but never enforced.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

from minutehand.adapters.providers.google_drive import wire
from minutehand.adapters.providers.google_drive.manifest import MANIFEST
from minutehand.domain.world import (
    Actor, Change, DocumentSnapshot, EntityKind, EntityRef, Operation, RecordSnapshot, Stored, WorldEvent,
)
from minutehand.ports.store import Store

ROOT_ID = "0AMyDriveRootFolder00000"
ROOT_ALIAS = "root"
"""Drive's word for the caller's My Drive in a file id or an `in parents` term."""
ROOT_NAME = "My Drive"
USERS = "users"

AGENT_NAME = "Agent"
AGENT_EMAIL = "agent@drive.example.com"
"""Who every call is made as: tokens are not verified, so no caller is told apart from another."""

_SCAN = 1000
_MAX_SEQ = 99_999_999


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _minted(prefix: str, seq: int) -> str:
    if seq > _MAX_SEQ:
        raise OverflowError(f"event {seq} no longer fits the eight digits of a minted id")
    return f"{prefix}{seq:08d}{_digest(prefix, str(seq))[:20]}"


def file_id(seq: int) -> str:
    """The id of the file written by event `seq`: ordered by creation, the same in every run that reaches it."""
    return _minted("1F", seq)


def comment_id(seq: int) -> str:
    return _minted("AAAB", seq)


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


def agent() -> wire.DriveUser:
    return wire.DriveUser(displayName=AGENT_NAME, emailAddress=AGENT_EMAIL, permissionId=permission_id(AGENT_EMAIL))


def snapshot(stored: wire.StoredFile) -> DocumentSnapshot:
    return DocumentSnapshot(title=stored.file.name, mime_type=stored.file.mimeType)


def readable_text(stored: wire.StoredFile) -> str:
    """The text a search or an export reads: a Doc's text, or a text file's bytes as UTF-8."""
    content = stored.content
    if isinstance(content, wire.DocText):
        return content.text
    if isinstance(content, wire.Blob) and stored.file.mimeType.startswith("text/"):
        return wire.blob_bytes(content).decode("utf-8", errors="replace")
    return ""


class DriveWorld:
    """Typed reads and writes of one run's Drive entities."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------ ids

    def next_seq(self) -> int:
        """The sequence number of the event about to be written; what a new file's id and version come from."""
        return self._store.head() + 1

    # ------------------------------------------------------------------ files

    @staticmethod
    def resolve(file: str) -> str:
        return ROOT_ID if file == ROOT_ALIAS else file

    def file(self, file: str) -> wire.StoredFile | None:
        stored = self._store.get(file_ref(self.resolve(file)))
        return None if stored is None else wire.parse(wire.StoredFile, stored.body)

    def root(self) -> wire.StoredFile | None:
        return self.file(ROOT_ID)

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

    def walk(self) -> Iterator[tuple[wire.StoredFile, bool]]:
        """Every file under My Drive, depth first, with whether a folder above it is in the trash."""
        pending: list[tuple[str, bool]] = [(ROOT_ID, False)]
        while pending:
            folder, trashed_above = pending.pop()
            for child in self.children(folder):
                yield child, trashed_above
                if child.file.mimeType == wire.FOLDER:
                    pending.append((child.file.id, trashed_above or child.file.trashed))

    def ancestors(self, stored: wire.StoredFile) -> list[wire.StoredFile]:
        """The folders above a file, nearest first, up to and including My Drive."""
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
        return self._store.apply(Change(
            entity=file_ref(stored.file.id), operation=operation, actor=actor, body=wire.dump(stored),
            parent=parents[0] if parents else None, after=snapshot(stored),
        ))

    def delete_file(self, stored: wire.StoredFile, *, actor: Actor) -> list[WorldEvent]:
        """Delete a file and, for a folder, everything under it, deepest first."""
        events: list[WorldEvent] = []
        if stored.file.mimeType == wire.FOLDER:
            for child in list(self.children(stored.file.id)):
                events += self.delete_file(child, actor=actor)
        parents = stored.file.parents
        events.append(self._store.apply(Change(
            entity=file_ref(stored.file.id), operation=Operation.DELETE, actor=actor,
            parent=parents[0] if parents else None,
        )))
        return events

    # ------------------------------------------------------------------ people and permissions

    def user(self, email: str) -> wire.DriveUser | None:
        stored = self._store.get(user_ref(permission_id(email)))
        if stored is None or stored.parent != USERS:
            return None
        return wire.parse(wire.DriveUser, stored.body)

    def write_user(self, user: wire.DriveUser, *, actor: Actor) -> WorldEvent:
        return self._store.apply(Change(
            entity=user_ref(user.permissionId), operation=Operation.CREATE, actor=actor, body=wire.dump(user),
            parent=USERS,
        ))

    def grants(self, file: str) -> list[wire.Permission]:
        return [wire.parse(wire.Permission, s.body) for s in self._pages(EntityKind.RECORD, file)]

    def write_grant(self, file: str, permission: wire.Permission, *, operation: Operation, actor: Actor) -> WorldEvent:
        who = permission.emailAddress or permission.domain or permission.type
        return self._store.apply(Change(
            entity=grant_ref(file, permission.id), operation=operation, actor=actor, body=wire.dump(permission),
            parent=file, after=RecordSnapshot(resource="permission", text=f"{permission.role} {who}"),
        ))

    # ------------------------------------------------------------------ comments

    def comments(self, file: str) -> list[wire.Comment]:
        """Every comment on a file, oldest first: comment ids are minted in event order."""
        return [wire.parse(wire.Comment, s.body) for s in self._pages(EntityKind.COMMENT, file)]

    def write_comment(self, file: str, comment: wire.Comment, *, actor: Actor) -> WorldEvent:
        return self._store.apply(Change(
            entity=comment_ref(comment.id), operation=Operation.CREATE, actor=actor, body=wire.dump(comment),
            parent=file, after=RecordSnapshot(resource="comment", text=comment.content),
        ))

    # ------------------------------------------------------------------ reads

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))
