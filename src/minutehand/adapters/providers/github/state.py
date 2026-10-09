"""GitHub as records in the run's store.

| GitHub thing | `EntityKind` | external id | parent |
|---|---|---|---|
| user or organization | RECORD | `account/<login, lower case>` | `accounts` |
| personal access token | RECORD | `token/<sha-256 of the token>` | `tokens` |
| who a credential the world does not hold acts as | RECORD | `stand-in` | `tokens` |
| installation access token issued | RECORD | `installation/<installation id>/token/<n>` | `installation/<installation id>` |
| repository | RECORD | `repo/<owner>/<name>`, lower case | `repositories` |
| file | RECORD | `file/<owner>/<name>/<path>` | the repository's external id |
| issue or pull request | RECORD | `issue/<owner>/<name>/<number>`, lower case, the number to ten digits | `issues/<owner>/<name>` |
| issue comment | RECORD | `comment/<owner>/<name>/<id>`, the id to twenty digits | `comments/<owner>/<name>` |
| label | RECORD | `label/<owner>/<name>/<label name>` | `labels/<owner>/<name>` |
| pull request review | RECORD | `review/<owner>/<name>/<id>` | `reviews/<owner>/<name>` |
| pull request review comment | RECORD | `rcomment/<owner>/<name>/<id>` | `rcomments/<owner>/<name>` |
| git blob | RECORD | `blob/<owner>/<name>/<sha>` | `blobs/<owner>/<name>` |
| the next id of a kind | RECORD | `counter/<kind>` | `counters` |
| armed fault | RECORD | `fault/<n>` | `faults` |
| primary rate-limit budget | RECORD | `budget/<login, lower case, or - for the address>/<resource>` | `budgets` |

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
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    Operation,
    RecordSnapshot,
    Stored,
    WorldEvent,
)
from minutehand.ports.store import Store

ACCOUNTS = "accounts"
COUNTERS = "counters"
TOKENS = "tokens"
REPOSITORIES = "repositories"
FAULTS = "faults"
BUDGETS = "budgets"

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


def _scope(owner: str, name: str) -> str:
    return f"{owner.lower()}/{name.lower()}"


def issues_parent(owner: str, name: str) -> str:
    return f"issues/{_scope(owner, name)}"


def issue_ref(owner: str, name: str, number: int) -> EntityRef:
    return _ref(f"issue/{_scope(owner, name)}/{number:010d}")


def comments_parent(owner: str, name: str) -> str:
    return f"comments/{_scope(owner, name)}"


def comment_ref(owner: str, name: str, comment: int) -> EntityRef:
    return _ref(f"comment/{_scope(owner, name)}/{comment:020d}")


def labels_parent(owner: str, name: str) -> str:
    return f"labels/{_scope(owner, name)}"


def label_ref(owner: str, name: str, label: str) -> EntityRef:
    return _ref(f"label/{_scope(owner, name)}/{label}")


def reviews_parent(owner: str, name: str) -> str:
    return f"reviews/{_scope(owner, name)}"


def review_ref(owner: str, name: str, review: int) -> EntityRef:
    return _ref(f"review/{_scope(owner, name)}/{review:020d}")


def review_comments_parent(owner: str, name: str) -> str:
    return f"rcomments/{_scope(owner, name)}"


def review_comment_ref(owner: str, name: str, comment: int) -> EntityRef:
    return _ref(f"rcomment/{_scope(owner, name)}/{comment:020d}")


def blobs_parent(owner: str, name: str) -> str:
    return f"blobs/{_scope(owner, name)}"


def blob_ref(owner: str, name: str, sha: str) -> EntityRef:
    return _ref(f"blob/{_scope(owner, name)}/{sha}")


def counter_ref(kind: str) -> EntityRef:
    return _ref(f"counter/{kind}")


def snapshot(resource: str, text: str) -> RecordSnapshot:
    """What the log says of a record the provider does not map to a ticket, message or document."""
    return RecordSnapshot(resource=resource, text=text)


STAND_IN = "stand-in"


def stand_in_ref() -> EntityRef:
    return _ref(STAND_IN)


def installation_id(installation: int) -> str:
    return f"installation/{installation}"


def installation_token_ref(installation: int, number: int) -> EntityRef:
    return _ref(f"{installation_id(installation)}/token/{number:06d}")


def fault_ref(number: int) -> EntityRef:
    return _ref(f"fault/{number:06d}")


ANONYMOUS = "-"
"""Whose budget a call with no credential spends: the address's. No login is a lone hyphen."""


def budget_ref(login: str | None, resource: wire.Resource) -> EntityRef:
    return _ref(f"budget/{(login or ANONYMOUS).lower()}/{resource.value}")


class GitHubWorld:
    """Typed reads and writes of one run's GitHub records."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @property
    def store(self) -> Store:
        return self._store

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

    def stand_in(self) -> wire.StoredStandIn | None:
        stored = self._store.get(stand_in_ref())
        return None if stored is None else wire.parse(wire.StoredStandIn, stored.body)

    def installation_tokens(self, installation: int) -> list[wire.StoredInstallationToken]:
        return [wire.parse(wire.StoredInstallationToken, s.body) for s in self._all(installation_id(installation))]

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

    def issues(self, repository: wire.StoredRepository) -> list[wire.StoredIssue]:
        """Every issue and pull request of the repository, by number."""
        found = [
            wire.parse(wire.StoredIssue, s.body) for s in self._all(issues_parent(repository.owner, repository.name))
        ]
        return sorted(found, key=lambda i: i.number)

    def issue(self, repository: wire.StoredRepository, number: int) -> wire.StoredIssue | None:
        stored = self._store.get(issue_ref(repository.owner, repository.name, number))
        return None if stored is None else wire.parse(wire.StoredIssue, stored.body)

    def comments(self, repository: wire.StoredRepository) -> list[wire.StoredComment]:
        """Every issue comment of the repository, by id."""
        found = [
            wire.parse(wire.StoredComment, s.body)
            for s in self._all(comments_parent(repository.owner, repository.name))
        ]
        return sorted(found, key=lambda c: c.id)

    def comment(self, repository: wire.StoredRepository, comment: int) -> wire.StoredComment | None:
        stored = self._store.get(comment_ref(repository.owner, repository.name, comment))
        return None if stored is None else wire.parse(wire.StoredComment, stored.body)

    def labels(self, repository: wire.StoredRepository) -> list[wire.StoredLabel]:
        """Every label of the repository, alphabetically by name, case aside (recorded)."""
        found = [
            wire.parse(wire.StoredLabel, s.body) for s in self._all(labels_parent(repository.owner, repository.name))
        ]
        return sorted(found, key=lambda label: (label.name.casefold(), label.name))

    def label(self, repository: wire.StoredRepository, name: str) -> wire.StoredLabel | None:
        stored = self._store.get(label_ref(repository.owner, repository.name, name))
        return None if stored is None else wire.parse(wire.StoredLabel, stored.body)

    def reviews(self, repository: wire.StoredRepository) -> list[wire.StoredReview]:
        """Every review of the repository, oldest first."""
        found = [
            wire.parse(wire.StoredReview, s.body) for s in self._all(reviews_parent(repository.owner, repository.name))
        ]
        return sorted(found, key=lambda r: r.id)

    def review_comments(self, repository: wire.StoredRepository) -> list[wire.StoredReviewComment]:
        """Every review comment of the repository, by id."""
        found = [
            wire.parse(wire.StoredReviewComment, s.body)
            for s in self._all(review_comments_parent(repository.owner, repository.name))
        ]
        return sorted(found, key=lambda c: c.id)

    def blob(self, repository: wire.StoredRepository, sha: str) -> wire.StoredBlob | None:
        stored = self._store.get(blob_ref(repository.owner, repository.name, sha))
        return None if stored is None else wire.parse(wire.StoredBlob, stored.body)

    def next_id(self, kind: str, *, first: int, actor: Actor) -> int:
        """The next id of `kind` GitHub hands out, from `first`; the count is a record, so a fork and a replay hand out
        the same ones."""
        stored = self._store.get(counter_ref(kind))
        found = first if stored is None else wire.parse(wire.StoredCounter, stored.body).next
        operation = Operation.CREATE if stored is None else Operation.UPDATE
        self._write(counter_ref(kind), wire.StoredCounter(next=found + 1), COUNTERS, operation, actor)
        return found

    def role(self, account: wire.StoredAccount, repository: wire.StoredRepository) -> wire.Permission | None:
        """The role the account holds by owning, collaborating or belonging, apart from the repository being public."""
        login = account.login.lower()
        if repository.owner.lower() == login:
            return wire.Permission.ADMIN
        roles = [c.permission for c in repository.collaborators if c.login.lower() == login]
        owner = self.account(repository.owner)
        if owner is not None and login in {m.lower() for m in owner.members}:
            roles.append(wire.Permission.PULL)
        return max(roles, key=wire.PERMISSION_ORDER.index) if roles else None

    def faults(self) -> list[tuple[EntityRef, wire.StoredFault]]:
        """Every armed fault, in the order it was armed."""
        return [(s.entity, wire.parse(wire.StoredFault, s.body)) for s in self._all(FAULTS)]

    def budget(self, login: str | None, resource: wire.Resource) -> wire.StoredBudget | None:
        stored = self._store.get(budget_ref(login, resource))
        return None if stored is None else wire.parse(wire.StoredBudget, stored.body)

    # ------------------------------------------------------------------ writes

    def _write(
        self,
        ref: EntityRef,
        body: wire.Wire,
        parent: str,
        operation: Operation,
        actor: Actor,
        after: RecordSnapshot | None = None,
    ) -> WorldEvent:
        return self._store.apply(
            Change(entity=ref, operation=operation, actor=actor, body=wire.dump(body), parent=parent, after=after)
        )

    def write_account(self, account: wire.StoredAccount) -> WorldEvent:
        return self._write(account_ref(account.login), account, ACCOUNTS, Operation.CREATE, Actor.SCENARIO)

    def write_token(self, token: str, stored: wire.StoredToken) -> WorldEvent:
        return self._write(token_ref(token), stored, TOKENS, Operation.CREATE, Actor.SCENARIO)

    def write_stand_in(self, stand_in: wire.StoredStandIn) -> WorldEvent:
        operation = Operation.CREATE if self._store.get(stand_in_ref()) is None else Operation.UPDATE
        return self._write(stand_in_ref(), stand_in, TOKENS, operation, Actor.SCENARIO)

    def issue_installation_token(self, issued: wire.StoredInstallationToken) -> int:
        """Record a token the agent's exchange was issued, as the agent's act; its number among the installation's
        tokens, from 0."""
        number = len(self.installation_tokens(issued.installation_id))
        ref = installation_token_ref(issued.installation_id, number)
        self._write(ref, issued, installation_id(issued.installation_id), Operation.CREATE, Actor.AGENT)
        return number

    def write_repository(self, repository: wire.StoredRepository) -> WorldEvent:
        ref = repository_ref(repository.owner, repository.name)
        return self._write(ref, repository, REPOSITORIES, Operation.CREATE, Actor.SCENARIO)

    def update_repository(
        self, repository: wire.StoredRepository, actor: Actor = Actor.SCENARIO, after: RecordSnapshot | None = None
    ) -> WorldEvent:
        ref = repository_ref(repository.owner, repository.name)
        return self._write(ref, repository, REPOSITORIES, Operation.UPDATE, actor, after)

    def write_file(self, repository: wire.StoredRepository, file: wire.StoredFile) -> WorldEvent:
        parent = repository_id(repository.owner, repository.name)
        return self._write(file_ref(repository, file.path), file, parent, Operation.CREATE, Actor.SCENARIO)

    def put_issue(
        self, repository: wire.StoredRepository, issue: wire.StoredIssue, *, operation: Operation, actor: Actor
    ) -> WorldEvent:
        """An issue or pull request as it now reads; the log says its title and body."""
        ref = issue_ref(repository.owner, repository.name, issue.number)
        text = issue.title if issue.body is None else f"{issue.title}\n{issue.body}"
        resource = "pulls" if issue.pull is not None else "issues"
        parent = issues_parent(repository.owner, repository.name)
        return self._write(ref, issue, parent, operation, actor, snapshot(resource, text))

    def put_comment(
        self, repository: wire.StoredRepository, comment: wire.StoredComment, *, operation: Operation, actor: Actor
    ) -> WorldEvent:
        ref = comment_ref(repository.owner, repository.name, comment.id)
        parent = comments_parent(repository.owner, repository.name)
        return self._write(ref, comment, parent, operation, actor, snapshot("comments", comment.body))

    def delete_comment(
        self, repository: wire.StoredRepository, comment: wire.StoredComment, *, actor: Actor
    ) -> WorldEvent:
        ref = comment_ref(repository.owner, repository.name, comment.id)
        parent = comments_parent(repository.owner, repository.name)
        return self._store.apply(Change(entity=ref, operation=Operation.DELETE, actor=actor, parent=parent))

    def put_label(
        self, repository: wire.StoredRepository, label: wire.StoredLabel, *, operation: Operation, actor: Actor
    ) -> WorldEvent:
        ref = label_ref(repository.owner, repository.name, label.name)
        parent = labels_parent(repository.owner, repository.name)
        return self._write(ref, label, parent, operation, actor, snapshot("labels", label.name))

    def put_review(
        self, repository: wire.StoredRepository, review: wire.StoredReview, *, operation: Operation, actor: Actor
    ) -> WorldEvent:
        ref = review_ref(repository.owner, repository.name, review.id)
        parent = reviews_parent(repository.owner, repository.name)
        return self._write(ref, review, parent, operation, actor, snapshot("reviews", review.body))

    def put_review_comment(
        self,
        repository: wire.StoredRepository,
        comment: wire.StoredReviewComment,
        *,
        operation: Operation,
        actor: Actor,
    ) -> WorldEvent:
        ref = review_comment_ref(repository.owner, repository.name, comment.id)
        parent = review_comments_parent(repository.owner, repository.name)
        return self._write(ref, comment, parent, operation, actor, snapshot("review_comments", comment.body))

    def put_blob(self, repository: wire.StoredRepository, blob: wire.StoredBlob, *, actor: Actor) -> None:
        """A blob, once: the same bytes are the same sha and are written the one time."""
        ref = blob_ref(repository.owner, repository.name, blob.sha)
        if self._store.get(ref) is None:
            parent = blobs_parent(repository.owner, repository.name)
            self._write(ref, blob, parent, Operation.CREATE, actor)

    def put_file(
        self, repository: wire.StoredRepository, file: wire.StoredFile, *, operation: Operation, actor: Actor
    ) -> WorldEvent:
        """A file of the default branch as a commit made through the API leaves it."""
        parent = repository_id(repository.owner, repository.name)
        return self._write(
            file_ref(repository, file.path), file, parent, operation, actor, snapshot("files", file.path)
        )

    def delete_file(self, repository: wire.StoredRepository, file: wire.StoredFile, *, actor: Actor) -> WorldEvent:
        parent = repository_id(repository.owner, repository.name)
        return self._store.apply(
            Change(entity=file_ref(repository, file.path), operation=Operation.DELETE, actor=actor, parent=parent)
        )

    def arm(self, number: int, fault: wire.StoredFault) -> WorldEvent:
        return self._write(fault_ref(number), fault, FAULTS, Operation.CREATE, Actor.SCENARIO)

    def spend(self, ref: EntityRef, fault: wire.StoredFault) -> WorldEvent:
        """The fault answered one more call. The world, not the agent, did it."""
        spent = fault.model_copy(update={"answered": fault.answered + 1})
        return self._write(ref, spent, FAULTS, Operation.UPDATE, Actor.SCENARIO)

    def write_budget(self, login: str | None, resource: wire.Resource, budget: wire.StoredBudget) -> WorldEvent:
        """The budget as it stands after a call spent from it, or as the scenario starts it. The world's
        bookkeeping, not the agent's act, as a spent fault is."""
        ref = budget_ref(login, resource)
        operation = Operation.CREATE if self._store.get(ref) is None else Operation.UPDATE
        return self._write(ref, budget, BUDGETS, operation, Actor.SCENARIO)

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))
