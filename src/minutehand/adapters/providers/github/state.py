"""GitHub as records in the run's store.

| GitHub thing | `EntityKind` | external id | parent |
|---|---|---|---|
| user or organization | RECORD | `account/<login, lower case>` | `accounts` |
| personal access token | RECORD | `token/<sha-256 of the token>` | `tokens` |
| repository | RECORD | `repo/<owner>/<name>`, lower case | `repositories` |
| file | RECORD | `file/<owner>/<name>/<path>` | the repository's external id |
| armed fault | RECORD | `fault/<n>` | `faults` |

GitHub matches logins and repository names in any case, so their ids are lower-cased; a path is not. A token is
kept under a digest of itself, so the log never holds the credential. Nothing here is held between calls: every
read is a query of the store, so a new app over the same store sees the same GitHub, and a fork sees it as of the
fork.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.manifest import MANIFEST
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, Stored, WorldEvent
from minutehand.ports.store import Store

ACCOUNTS = "accounts"
TOKENS = "tokens"
REPOSITORIES = "repositories"
FAULTS = "faults"

_SCAN = 1000


def _ref(external_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=external_id)


def account_ref(login: str) -> EntityRef:
    return _ref(f"account/{login.lower()}")


def token_ref(token: str) -> EntityRef:
    return _ref(f"token/{hashlib.sha256(token.encode()).hexdigest()}")


def repository_id(owner: str, name: str) -> str:
    return f"repo/{owner.lower()}/{name.lower()}"


def repository_ref(owner: str, name: str) -> EntityRef:
    return _ref(repository_id(owner, name))


def file_ref(repository: wire.StoredRepository, path: str) -> EntityRef:
    return _ref(f"file/{repository.owner.lower()}/{repository.name.lower()}/{path}")


def fault_ref(number: int) -> EntityRef:
    return _ref(f"fault/{number:06d}")


class GitHubWorld:
    """Typed reads and writes of one run's GitHub records."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------ reads

    def _all(self, parent: str) -> Iterator[Stored]:
        after: str | None = None
        while True:
            page = self._store.children(MANIFEST.key, EntityKind.RECORD, parent, after=after, limit=_SCAN)
            yield from page
            if len(page) < _SCAN:
                return
            after = page[-1].entity.external_id

    def account(self, login: str) -> wire.StoredAccount | None:
        stored = self._store.get(account_ref(login))
        return None if stored is None else wire.parse(wire.StoredAccount, stored.body)

    def accounts(self) -> list[wire.StoredAccount]:
        return [wire.parse(wire.StoredAccount, s.body) for s in self._all(ACCOUNTS)]

    def token(self, presented: str) -> wire.StoredToken | None:
        stored = self._store.get(token_ref(presented))
        return None if stored is None else wire.parse(wire.StoredToken, stored.body)

    def repository(self, owner: str, name: str) -> wire.StoredRepository | None:
        stored = self._store.get(repository_ref(owner, name))
        return None if stored is None else wire.parse(wire.StoredRepository, stored.body)

    def repositories(self) -> list[wire.StoredRepository]:
        return [wire.parse(wire.StoredRepository, s.body) for s in self._all(REPOSITORIES)]

    def files(self, repository: wire.StoredRepository) -> list[wire.StoredFile]:
        """Every file of the repository, ordered by path."""
        found = [
            wire.parse(wire.StoredFile, s.body) for s in self._all(repository_id(repository.owner, repository.name))
        ]
        return sorted(found, key=lambda f: f.path)

    def file(self, repository: wire.StoredRepository, path: str) -> wire.StoredFile | None:
        stored = self._store.get(file_ref(repository, path))
        return None if stored is None else wire.parse(wire.StoredFile, stored.body)

    def faults(self) -> list[tuple[EntityRef, wire.StoredFault]]:
        """Every armed fault, in the order it was armed."""
        return [(s.entity, wire.parse(wire.StoredFault, s.body)) for s in self._all(FAULTS)]

    # ------------------------------------------------------------------ writes

    def _write(self, ref: EntityRef, body: wire.Wire, parent: str, operation: Operation, actor: Actor) -> WorldEvent:
        return self._store.apply(
            Change(entity=ref, operation=operation, actor=actor, body=wire.dump(body), parent=parent)
        )

    def write_account(self, account: wire.StoredAccount) -> WorldEvent:
        return self._write(account_ref(account.login), account, ACCOUNTS, Operation.CREATE, Actor.SCENARIO)

    def write_token(self, token: str, stored: wire.StoredToken) -> WorldEvent:
        return self._write(token_ref(token), stored, TOKENS, Operation.CREATE, Actor.SCENARIO)

    def write_repository(self, repository: wire.StoredRepository) -> WorldEvent:
        ref = repository_ref(repository.owner, repository.name)
        return self._write(ref, repository, REPOSITORIES, Operation.CREATE, Actor.SCENARIO)

    def write_file(self, repository: wire.StoredRepository, file: wire.StoredFile) -> WorldEvent:
        parent = repository_id(repository.owner, repository.name)
        return self._write(file_ref(repository, file.path), file, parent, Operation.CREATE, Actor.SCENARIO)

    def arm(self, number: int, fault: wire.StoredFault) -> WorldEvent:
        return self._write(fault_ref(number), fault, FAULTS, Operation.CREATE, Actor.SCENARIO)

    def spend(self, ref: EntityRef, fault: wire.StoredFault) -> WorldEvent:
        """The fault answered one more call. The world, not the agent, did it."""
        spent = fault.model_copy(update={"answered": fault.answered + 1})
        return self._write(ref, spent, FAULTS, Operation.UPDATE, Actor.SCENARIO)

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))
