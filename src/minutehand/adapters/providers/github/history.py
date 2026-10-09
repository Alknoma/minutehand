"""A repository's history as git holds it: commits with parents, the default branch, and branches with commits of their
own (`StoredLine`), each of which has the tree its base and its commits make.

The default branch's tree is the repository's file records; every other tree is made by laying a line's commits on the
tree it was made at. A blob is kept under its sha for as long as a tree may name it (`StoredBlob`). A commit made
through the API is a `StoredCommit` with the paths it changed and the blobs they hold, so that a diff between two
trees, and a merge, can be worked out from what is kept.

Every other branch and commit of a seeded repository still shows the default branch's head files: history before the
first commit made here is a list of commits, not a sequence of trees. `README.md` says so.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
from dataclasses import dataclass
from datetime import datetime

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.domain.errors import NotServed
from minutehand.domain.world import Actor, Operation

Tree = dict[str, str]
"""A tree as a path to the sha of the blob it holds."""


def stored_blob(raw: bytes) -> wire.StoredBlob:
    return wire.StoredBlob(sha=wire.blob_sha(raw), content=base64.b64encode(raw).decode("ascii"), size=len(raw))


def trunk_tree(world: GitHubWorld, repository: wire.StoredRepository) -> Tree:
    """The default branch's tree: its files."""
    return {f.path: f.sha for f in world.files(repository)}


def line_tree(line: wire.StoredLine, upto: str | None = None) -> Tree:
    """The tree of a line: the tree it was made at with its commits laid on, oldest first, as far as `upto` (a commit
    sha) when one is named."""
    tree = {e.path: e.sha for e in line.base}
    if upto == line.fork:
        return tree
    for commit in reversed(line.commits):
        for change in commit.changes:
            if change.status is wire.ChangeStatus.REMOVED or change.sha is None:
                tree.pop(change.path, None)
            else:
                tree[change.path] = change.sha
        if upto is not None and commit.sha == upto:
            break
    return tree


def materialize(world: GitHubWorld, repository: wire.StoredRepository, tree: Tree) -> list[wire.StoredFile]:
    """The files a tree names, read from their blobs, ordered by path."""
    files: list[wire.StoredFile] = []
    for path, sha in sorted(tree.items()):
        blob = world.blob(repository, sha)
        if blob is None:
            raise LookupError(f"{repository.full_name} holds no blob {sha}, which {path} names")
        files.append(wire.StoredFile(path=path, content=blob.content, size=blob.size, sha=sha))
    return files


def line_named(repository: wire.StoredRepository, name: str) -> wire.StoredLine | None:
    return next((line for line in repository.lines if line.name == name), None)


def tip(repository: wire.StoredRepository, name: str) -> str | None:
    """The commit a branch points at: the default branch and the branches that follow it point at the head."""
    line = line_named(repository, name)
    if line is not None:
        return line.commits[0].sha if line.commits else line.fork
    if name == repository.default_branch or name in repository.branches:
        return repository.commits[0].sha if repository.commits else None
    return None


def every_commit(repository: wire.StoredRepository) -> list[wire.StoredCommit]:
    """Every commit the repository knows: the default branch's history, then each line's own."""
    return [*repository.commits, *(c for line in repository.lines for c in line.commits)]


def commit_of(repository: wire.StoredRepository, sha: str) -> wire.StoredCommit | None:
    return next((c for c in every_commit(repository) if c.sha == sha), None)


def log(repository: wire.StoredRepository, start: wire.StoredCommit) -> list[wire.StoredCommit]:
    """`start` and every commit it descends from, newest first by the time they were committed, as `git log` lists
    them; a commit made before its own parents' times (equal times, say) still follows what it descends from."""
    known = {c.sha: c for c in every_commit(repository)}
    order = {sha: n for n, sha in enumerate(known)}
    seen: set[str] = set()
    queue: list[tuple[float, int, str]] = []

    def push(commit: wire.StoredCommit) -> None:
        if commit.sha not in seen:
            seen.add(commit.sha)
            heapq.heappush(
                queue, (-datetime.fromisoformat(commit.committer_date).timestamp(), order[commit.sha], commit.sha)
            )

    push(start)
    found: list[wire.StoredCommit] = []
    while queue:
        _, _, sha = heapq.heappop(queue)
        commit = known[sha]
        found.append(commit)
        for parent in (commit.parent, commit.merged):
            if parent is not None and parent in known:
                push(known[parent])
    return found


def between(repository: wire.StoredRepository, base: str, head: str) -> list[wire.StoredCommit]:
    """The commits `head` has that `base` does not, oldest first: what a pull request from `head` into `base` carries."""
    held = {c.sha for c in log(repository, _must(commit_of(repository, base)))}
    return [c for c in reversed(log(repository, _must(commit_of(repository, head)))) if c.sha not in held]


def _must(commit: wire.StoredCommit | None) -> wire.StoredCommit:
    if commit is None:
        raise LookupError("a commit the repository does not hold")
    return commit


def changes_between(before: Tree, after: Tree) -> list[wire.FileChange]:
    """What turns the first tree into the second, by path: added, modified, removed."""
    changes: list[wire.FileChange] = []
    for path in sorted(before.keys() | after.keys()):
        old, new = (before[path] if path in before else None), (after[path] if path in after else None)
        if old == new:
            continue
        status = (
            wire.ChangeStatus.ADDED
            if old is None
            else wire.ChangeStatus.REMOVED
            if new is None
            else wire.ChangeStatus.MODIFIED
        )
        changes.append(wire.FileChange(path=path, status=status, sha=new, previous_sha=old))
    return changes


def refuse_clash(tree: Tree, path: str) -> None:
    """A path is a file or a directory, never both: git's object model."""
    if path in tree:
        return
    if any(p.startswith(path + "/") or path.startswith(p + "/") for p in tree):
        raise NotServed(f"{path} would be both a file and a directory")


def commit_sha(full_name: str, parent: str | None, message: str, date: str, changes: list[wire.FileChange]) -> str:
    """A commit's id: a digest of what makes it the commit it is."""
    shape = "\0".join(f"{c.path}:{c.sha}" for c in changes)
    return hashlib.sha1(f"commit\0{full_name}\0{parent}\0{date}\0{message}\0{shape}".encode()).hexdigest()


def commit_name(account: wire.StoredAccount) -> str:
    """The name a commit made as the account carries: the account's declared name. A login is not a name, and nothing
    else is made up."""
    if account.name is None:
        raise NotServed(f"committing as {account.login}, who has no declared name for a commit to carry")
    return account.name


def commit_email(account: wire.StoredAccount) -> str:
    """The account's email, else GitHub's no-reply address for an account that keeps its email private:
    https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address"""
    return account.email or f"{account.id}+{account.login}@users.noreply.github.com"


@dataclass(frozen=True)
class Authorship:
    """Who made a commit and when, as the commit carries them."""

    author_login: str | None
    author_name: str
    author_email: str
    author_date: str
    committer_login: str | None
    committer_name: str
    committer_email: str
    committer_date: str


def new_commit(
    repository: wire.StoredRepository,
    *,
    message: str,
    by: Authorship,
    parent: str | None,
    merged: str | None,
    changes: list[wire.FileChange],
    salt: str = "",
) -> wire.StoredCommit:
    return wire.StoredCommit(
        sha=commit_sha(repository.full_name + salt, parent, message, by.committer_date, changes),
        message=message,
        author_login=by.author_login,
        author_name=by.author_name,
        author_email=by.author_email,
        committer_login=by.committer_login,
        committer_name=by.committer_name,
        committer_email=by.committer_email,
        committer_date=by.committer_date,
        date=by.author_date,
        paths=[c.path for c in changes],
        parent=parent,
        merged=merged,
        changes=changes,
    )


def diverge(world: GitHubWorld, repository: wire.StoredRepository, name: str) -> wire.StoredRepository:
    """A branch that has followed the default branch's head is left where it is as the default branch moves on: it
    becomes a line at the commit it points at, with the tree the default branch has now."""
    if name not in repository.branches:
        return repository
    head = repository.commits[0].sha
    line = wire.StoredLine(
        name=name,
        fork=head,
        base=[wire.TreeEntry(path=p, sha=s) for p, s in sorted(trunk_tree(world, repository).items())],
        commits=[],
    )
    return repository.model_copy(
        update={"branches": [b for b in repository.branches if b != name], "lines": [*repository.lines, line]}
    )


def commit_to_trunk(
    world: GitHubWorld,
    repository: wire.StoredRepository,
    commit: wire.StoredCommit,
    blobs: list[wire.StoredBlob],
    *,
    actor: Actor,
) -> wire.StoredRepository:
    """`commit` made on the default branch: its blobs kept, its files written, the head moved to it, and every branch
    that followed the head left where it was."""
    for blob in blobs:
        world.put_blob(repository, blob, actor=actor)
    held = repository
    for name in list(repository.branches):
        held = diverge(world, held, name)
    for change in commit.changes:
        if change.status is wire.ChangeStatus.REMOVED or change.sha is None:
            existing = world.file(repository, change.path)
            if existing is not None:
                world.delete_file(repository, existing, actor=actor)
            continue
        blob = world.blob(repository, change.sha)
        assert blob is not None
        file = wire.StoredFile(path=change.path, content=blob.content, size=blob.size, sha=blob.sha)
        existing = world.file(repository, change.path)
        operation = Operation.CREATE if existing is None else Operation.UPDATE
        world.put_file(repository, file, operation=operation, actor=actor)
    moved = held.model_copy(update={"commits": [commit, *held.commits]})
    world.update_repository(moved, actor)
    return moved


def commit_to_line(
    world: GitHubWorld,
    repository: wire.StoredRepository,
    name: str,
    commit: wire.StoredCommit,
    blobs: list[wire.StoredBlob],
    *,
    actor: Actor,
) -> wire.StoredRepository:
    """`commit` made on a branch of its own, which a branch that followed the head becomes by being committed to."""
    for blob in blobs:
        world.put_blob(repository, blob, actor=actor)
    held = diverge(world, repository, name)
    lines = [
        line.model_copy(update={"commits": [commit, *line.commits]}) if line.name == name else line
        for line in held.lines
    ]
    moved = held.model_copy(update={"lines": lines})
    world.update_repository(moved, actor)
    return moved


def authorship(account: wire.StoredAccount, at: str) -> Authorship:
    """A commit made by `account` at `at`: they author and commit it, as the reference defaults it ("By default,
    `committer` will use the information set in `author`", https://docs.github.com/en/rest/git/commits#create-a-commit)."""
    login = account.login if account.type is wire.AccountType.USER else None
    name, email = commit_name(account), commit_email(account)
    return Authorship(
        author_login=login,
        author_name=name,
        author_email=email,
        author_date=at,
        committer_login=login,
        committer_name=name,
        committer_email=email,
        committer_date=at,
    )
