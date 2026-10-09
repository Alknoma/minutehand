"""The GitHub a scenario starts with.

`GitHubSeed` is this provider's own seed model: accounts (users, each optionally a scenario person),
organizations and their members, personal access tokens, repositories with their files, history, collaborators
and branches, the faults armed against the agent, and primary rate-limit budgets that start part spent. The shared scenario has no place for it yet, so it is handed to
`seed()` beside the scenario (`GitHubProvider.seed_with`); a world seeded from the scenario alone has an empty
GitHub, in which every token is unknown.

Every commit's date is an offset before the scenario's start, so the same file replays on any day. A repository
with files declares the commits that made them: GitHub holds no file outside a commit, and nothing here makes one
up. One with neither is empty, as a repository just created is.

A repository may also hold labels, issues and pull requests (each numbered by the seed, since GitHub numbers both
from one sequence and a fragment added to an open world must not move what it already holds), and branches with
commits of their own, which a pull request is made from.

Everything is written as actor SCENARIO.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
from datetime import datetime, timedelta
from itertools import pairwise
from typing import ClassVar, Self

from pydantic import Field, model_validator

from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.manifest import MANIFEST
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.domain.provider import Keyed
from minutehand.domain.scenario import Person, Scenario
from minutehand.domain.world import Actor, Operation
from minutehand.ports.store import Store

LOGIN = r"^[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}$"
"""GitHub's rule for a login: letters, digits and single inner hyphens, at most 39 characters."""
REPOSITORY_NAME = r"^[A-Za-z0-9._-]{1,100}$"


class SeedUser(wire.Wire, Keyed):
    IDENTITY: ClassVar[tuple[str, ...]] = ("login",)
    login: str = Field(pattern=LOGIN)
    person: str | None = Field(default=None, description="Person.key: the account's name and email are theirs")
    name: str | None = Field(default=None, description="When no person is named")


class SeedOrganization(wire.Wire, Keyed):
    IDENTITY: ClassVar[tuple[str, ...]] = ("login",)
    login: str = Field(pattern=LOGIN)
    name: str | None = None
    members: list[str] = Field(default=[], description="Logins of seeded users")


class SeedToken(wire.Wire, Keyed):
    """A personal access token and the user it acts as. Its prefix must be the one GitHub writes on its kind. What
    it was issued for is echoed (`X-OAuth-Scopes`), never enforced: every credential is accepted (README)."""

    IDENTITY: ClassVar[tuple[str, ...]] = ("token",)

    token: str
    kind: wire.TokenKind
    login: str
    scopes: list[str] = Field(default=["repo"], description="Classic only: echoed in `X-OAuth-Scopes`")

    @model_validator(mode="after")
    def _shaped_as_its_kind(self) -> Self:
        prefix = wire.TOKEN_PREFIXES[self.kind]
        if not self.token.startswith(prefix) or len(self.token) <= len(prefix):
            raise ValueError(f"a {self.kind.value} token starts with {prefix!r}")
        if self.kind is wire.TokenKind.FINE_GRAINED and self.scopes != ["repo"]:
            raise ValueError("a fine-grained token has no OAuth scopes")
        return self


class SeedFile(wire.Wire, Keyed):
    """A file at the head of every branch: text, or bytes as base64 for a binary file."""

    IDENTITY: ClassVar[tuple[str, ...]] = ("path",)

    path: str = Field(pattern=r"^[^/].*[^/]$|^[^/]$")
    text: str | None = None
    base64_bytes: str | None = None

    @model_validator(mode="after")
    def _one_content(self) -> Self:
        if (self.text is None) == (self.base64_bytes is None):
            raise ValueError(f"{self.path}: give exactly one of text and base64_bytes")
        if "//" in self.path:
            raise ValueError(f"{self.path}: a path has no empty segment")
        return self

    def raw(self) -> bytes:
        if self.text is not None:
            return self.text.encode("utf-8")
        assert self.base64_bytes is not None
        try:
            return base64.b64decode(self.base64_bytes, validate=True)
        except binascii.Error as error:
            raise ValueError(f"{self.path}: base64_bytes is not base64") from error


class SeedCommit(wire.Wire):
    message: str = Field(min_length=1)
    author: str = Field(description="Login of a seeded user")
    committer: str | None = Field(
        default=None,
        description="Login of a seeded user who committed it; None is the author, as GitHub's create-a-commit "
        "reference defaults it (https://docs.github.com/en/rest/git/commits#create-a-commit)",
    )
    committed_before: timedelta | None = Field(
        default=None,
        description="How long before the scenario's start it was committed; None is when it was authored, since "
        'the reference\'s committer "will use the information set in `author`" by default',
    )
    before: timedelta = Field(description="How long before the scenario's start it was made")
    paths: list[str] = Field(
        default=[], description="The file paths it changed; what `commits?path=` filters on. Every file is named here"
    )


class SeedLabel(wire.Wire, Keyed):
    IDENTITY: ClassVar[tuple[str, ...]] = ("name",)
    name: str = Field(min_length=1)
    color: str = Field(pattern=r"^[0-9A-Fa-f]{6}$", description="Six hexadecimal digits, without the #")
    description: str | None = Field(default=None, max_length=100)
    default: bool = Field(default=False, description="Whether it comes by default in a new repository")


class SeedComment(wire.Wire):
    author: str = Field(description="Login of a seeded user")
    body: str
    before: timedelta = Field(description="How long before the scenario's start it was written")


class SeedReview(wire.Wire):
    author: str = Field(description="Login of a seeded user")
    state: wire.ReviewState
    body: str = ""
    before: timedelta = Field(description="How long before the scenario's start it was submitted")


class SeedIssue(wire.Wire, Keyed):
    IDENTITY: ClassVar[tuple[str, ...]] = ("number",)
    number: int = Field(ge=1, description="One sequence numbers the repository's issues and pull requests")
    title: str = Field(min_length=1)
    body: str | None = None
    author: str = Field(description="Login of a seeded user")
    state: wire.IssueState = wire.IssueState.OPEN
    state_reason: wire.StateReason | None = None
    labels: list[str] = Field(default=[], description="Names of the repository's labels")
    assignees: list[str] = Field(default=[], description="Logins of seeded users")
    before: timedelta = Field(description="How long before the scenario's start it was opened")
    closed_before: timedelta | None = Field(default=None, description="When it was closed; required if it is closed")
    closed_by: str | None = Field(default=None, description="Login; None is the author")
    locked: bool = False
    lock_reason: wire.LockReason | None = Field(default=None, description="Why it is locked, if a reason was given")
    comments: list[SeedComment] = Field(default=[], description="Oldest first")

    @model_validator(mode="after")
    def _closed_when_it_is(self) -> Self:
        if (self.state is wire.IssueState.CLOSED) != (self.closed_before is not None):
            raise ValueError(f"issue {self.number}: give closed_before exactly when the issue is closed")
        if self.lock_reason is not None and not self.locked:
            raise ValueError(f"issue {self.number}: a reason for locking it, and it is not locked")
        if self.state is wire.IssueState.OPEN and self.state_reason not in (None, wire.StateReason.REOPENED):
            raise ValueError(f"issue {self.number}: an open issue has no reason beyond having been reopened")
        return self


class SeedPull(wire.Wire, Keyed):
    IDENTITY: ClassVar[tuple[str, ...]] = ("number",)
    number: int = Field(ge=1, description="One sequence numbers the repository's issues and pull requests")
    title: str = Field(min_length=1)
    body: str | None = None
    author: str = Field(description="Login of a seeded user")
    head: str = Field(description="A branch of `diverged_branches`")
    draft: bool = False
    state: wire.IssueState = wire.IssueState.OPEN
    labels: list[str] = []
    assignees: list[str] = Field(default=[], description="Logins of seeded users")
    requested_reviewers: list[str] = Field(default=[], description="Logins of seeded users")
    before: timedelta = Field(description="How long before the scenario's start it was opened")
    closed_before: timedelta | None = Field(default=None, description="When it was closed; required if it is closed")
    closed_by: str | None = Field(default=None, description="Login; None is the author")
    comments: list[SeedComment] = Field(default=[], description="Oldest first")
    reviews: list[SeedReview] = Field(default=[], description="Oldest first")

    @model_validator(mode="after")
    def _closed_when_it_is(self) -> Self:
        if (self.state is wire.IssueState.CLOSED) != (self.closed_before is not None):
            raise ValueError(f"pull request {self.number}: give closed_before exactly when it is closed")
        return self


class SeedChange(wire.Wire, Keyed):
    """What a commit does to one path: gives it the text or bytes, or deletes it."""

    IDENTITY: ClassVar[tuple[str, ...]] = ("path",)
    path: str = Field(pattern=r"^[^/].*[^/]$|^[^/]$")
    text: str | None = None
    base64_bytes: str | None = None
    delete: bool = False

    @model_validator(mode="after")
    def _one_thing(self) -> Self:
        if [self.text is not None, self.base64_bytes is not None, self.delete].count(True) != 1:
            raise ValueError(f"{self.path}: give exactly one of text, base64_bytes and delete")
        return self

    def raw(self) -> bytes:
        if self.text is not None:
            return self.text.encode("utf-8")
        assert self.base64_bytes is not None
        try:
            return base64.b64decode(self.base64_bytes, validate=True)
        except binascii.Error as error:
            raise ValueError(f"{self.path}: base64_bytes is not base64") from error


class SeedLineCommit(wire.Wire):
    message: str = Field(min_length=1)
    author: str = Field(description="Login of a seeded user")
    before: timedelta = Field(description="How long before the scenario's start it was made")
    changes: list[SeedChange] = Field(min_length=1)


class SeedBranch(wire.Wire, Keyed):
    """A branch with commits of its own, made at the default branch's head as the seed leaves it."""

    IDENTITY: ClassVar[tuple[str, ...]] = ("name",)
    name: str = Field(min_length=1)
    commits: list[SeedLineCommit] = Field(min_length=1, description="Oldest first")


class SeedRepository(wire.Wire, Keyed):
    IDENTITY: ClassVar[tuple[str, ...]] = (
        "owner",
        "name",
    )
    owner: str = Field(description="Login of a seeded user or organization")
    name: str = Field(pattern=REPOSITORY_NAME)
    private: bool = False
    description: str | None = None
    homepage: str | None = None
    topics: list[str] = []
    license: wire.License | None = None
    default_branch: str = "main"
    branches: list[str] = Field(default=[], description="More branch names, each at the head commit")
    collaborators: list[wire.Collaborator] = []
    files: list[SeedFile] = []
    commits: list[SeedCommit] = Field(default=[], description="Oldest first")
    diverged_branches: list[SeedBranch] = Field(
        default=[], description="Branches with commits of their own, the default branch's head as the seed leaves it"
    )
    labels: list[SeedLabel] = []
    issues: list[SeedIssue] = []
    pulls: list[SeedPull] = []
    stargazers_count: int = Field(default=0, ge=0)
    forks_count: int = Field(default=0, ge=0)
    network_count: int | None = Field(default=None, ge=0, description="The fork network's size; None is forks_count")
    subscribers_count: int = Field(default=0, ge=0, description="Who watches it")
    tree_entry_limit: int = Field(
        default=100_000,
        ge=1,
        description="Entries a recursive tree holds before GitHub answers it truncated; lowered only so a "
        "small repository can be read truncated",
    )
    directory_entry_limit: int = Field(
        default=1000,
        ge=1,
        description="Entries the contents endpoint lists of one directory, saying nothing of the rest; lowered "
        "only so a small directory can be read cut short",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        paths = [f.path for f in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError(f"{self.owner}/{self.name}: two files share a path")
        as_dirs = {p.rsplit("/", 1)[0] for p in paths if "/" in p}
        clash = sorted(p for p in paths if p in as_dirs or any(d.startswith(p + "/") for d in as_dirs))
        if clash:
            raise ValueError(f"{self.owner}/{self.name}: {clash[0]} is both a file and a directory")
        if self.files and not self.commits:
            raise ValueError(
                f"{self.owner}/{self.name}: a repository with files has the commits that made them; declare at least "
                "one (message, author, before)"
            )
        made = {p for c in self.commits for p in c.paths}
        orphans = sorted(p for p in paths if p not in made)
        if orphans:
            raise ValueError(
                f"{self.owner}/{self.name}: {orphans[0]} is in no commit's paths; declare the commit that added it"
            )
        befores = [c.before for c in self.commits]
        if any(later >= earlier for earlier, later in pairwise(befores)):
            raise ValueError(f"{self.owner}/{self.name}: commits are oldest first, each made after the one before")
        if self.default_branch in self.branches:
            raise ValueError(f"{self.owner}/{self.name}: {self.default_branch} is the default branch already")
        self._tracker_consistent()
        return self

    def _tracker_consistent(self) -> None:
        full = f"{self.owner}/{self.name}"
        lines = [b.name for b in self.diverged_branches]
        taken = [self.default_branch, *self.branches]
        clash = sorted({n for n in lines if n in taken or lines.count(n) > 1})
        if clash:
            raise ValueError(f"{full}: the branch {clash[0]} is named twice")
        if self.diverged_branches and not self.commits:
            raise ValueError(
                f"{full}: a branch with commits of its own is made at a commit; declare one on the default"
            )
        head_before = self.commits[-1].before if self.commits else None
        for branch in self.diverged_branches:
            befores = [c.before for c in branch.commits]
            if any(later >= earlier for earlier, later in pairwise(befores)):
                raise ValueError(f"{full}: {branch.name}'s commits are oldest first, each made after the one before")
            if head_before is not None and befores[0] >= head_before:
                raise ValueError(f"{full}: {branch.name}'s commits are made after the default branch's head")
        numbers = [i.number for i in self.issues] + [p.number for p in self.pulls]
        if len(numbers) != len(set(numbers)):
            raise ValueError(f"{full}: two issues or pull requests share a number")
        defined = {label.name for label in self.labels}
        if len(defined) != len(self.labels):
            raise ValueError(f"{full}: two labels share a name")
        for item in [*self.issues, *self.pulls]:
            missing = sorted(set(item.labels) - defined)
            if missing:
                raise ValueError(f"{full}: #{item.number} carries the label {missing[0]}, which the repository has not")
        for pull in self.pulls:
            if pull.head not in lines:
                raise ValueError(
                    f"{full}: pull request {pull.number} is from {pull.head}, which is not in diverged_branches"
                )
        for item in [*self.issues, *self.pulls]:
            moments = [item.before, *(c.before for c in item.comments)]
            if any(later >= earlier for earlier, later in pairwise(moments)):
                raise ValueError(f"{full}: #{item.number}'s comments are oldest first, each after the one before")


class SeedLimits(wire.Wire):
    """A repository's reading limits set apart from its seed: what a test lowers on a world already open."""

    repository: str = Field(description="`owner/name` of a seeded repository")
    tree_entry_limit: int | None = Field(default=None, ge=1, description="None leaves it as it is")
    directory_entry_limit: int | None = Field(default=None, ge=1, description="None leaves it as it is")


class SeedBudget(wire.Wire):
    """Where a primary rate-limit budget starts: what remains of it at the scenario's start, in a window that ends
    a full window later. A budget not named starts whole."""

    login: str | None = Field(description="Login of a seeded user; None is the address calls with no credential")
    resource: wire.Resource
    remaining: int = Field(ge=0)

    @model_validator(mode="after")
    def _within_the_limit(self) -> Self:
        limit = wire.limit_for(self.resource, authenticated=self.login is not None)
        if self.remaining > limit:
            raise ValueError(f"{self.resource.value}: {self.remaining} remaining is more than the limit of {limit}")
        return self


class GitHubSeed(wire.Wire):
    users: list[SeedUser] = []
    organizations: list[SeedOrganization] = []
    tokens: list[SeedToken] = []
    repositories: list[SeedRepository] = []
    faults: list[wire.Fault] = Field(default=[], description="Answered to the agent's calls in the order armed")
    limits: list[SeedLimits] = Field(
        default=[], description="Each repository's reading limits, over what its own seed says; applied in order"
    )
    budgets: list[SeedBudget] = Field(default=[], description="Primary budgets that start part spent")
    unknown_credentials_act_as: str | None = Field(
        default=None,
        description="Login of a seeded user every credential the world does not hold acts as (an unseeded token, "
        "a JWT, an installation token); None is the first user this seed lists",
    )

    @model_validator(mode="after")
    def _names_resolve(self) -> Self:
        users = {u.login.lower() for u in self.users}
        logins = [u.login.lower() for u in self.users] + [o.login.lower() for o in self.organizations]
        if len(logins) != len(set(logins)):
            raise ValueError("two accounts share a login")
        accounts = set(logins)
        named_users = [m for o in self.organizations for m in o.members]
        named_users += [t.login for t in self.tokens]
        named_users += [c.login for r in self.repositories for c in r.collaborators]
        named_users += [c.author for r in self.repositories for c in r.commits]
        named_users += [c.committer for r in self.repositories for c in r.commits if c.committer is not None]
        for r in self.repositories:
            for i in r.issues:
                named_users += [i.author, *i.assignees, *(c.author for c in i.comments)]
                named_users += [i.closed_by] if i.closed_by is not None else []
            for p in r.pulls:
                named_users += [p.author, *p.assignees, *p.requested_reviewers, *(c.author for c in p.comments)]
                named_users += [v.author for v in p.reviews]
                named_users += [p.closed_by] if p.closed_by is not None else []
            named_users += [c.author for b in r.diverged_branches for c in b.commits]
        named_users += [b.login for b in self.budgets if b.login is not None]
        named_users += [self.unknown_credentials_act_as] if self.unknown_credentials_act_as is not None else []
        missing = sorted({n for n in named_users if n.lower() not in users})
        if missing:
            raise ValueError(f"no such user: {', '.join(missing)}")
        owners = sorted({r.owner for r in self.repositories if r.owner.lower() not in accounts})
        if owners:
            raise ValueError(f"no such owner: {', '.join(owners)}")
        names = [f"{r.owner}/{r.name}".lower() for r in self.repositories]
        if len(names) != len(set(names)):
            raise ValueError("two repositories share a name")
        limited = sorted({x.repository for x in self.limits if x.repository.lower() not in names})
        if limited and self.repositories:
            raise ValueError(f"limits name no such repository: {', '.join(limited)}")
        tokens = [t.token for t in self.tokens]
        if len(tokens) != len(set(tokens)):
            raise ValueError("two tokens are the same token")
        budgets = [((b.login or "").lower(), b.resource) for b in self.budgets]
        if len(budgets) != len(set(budgets)):
            raise ValueError("a budget is started twice")
        return self


def number(text: str) -> int:
    """A stable positive id from a name: the same name has the same id in every run."""
    return int(hashlib.sha1(text.lower().encode()).hexdigest()[:8], 16)


def _commit_sha(full_name: str, position: int, message: str, date: str) -> str:
    return hashlib.sha1(f"commit\0{full_name}\0{position}\0{date}\0{message}".encode()).hexdigest()


def _people(scenario: Scenario) -> dict[str, Person]:
    return {p.key: p for p in scenario.people}


def _user(user: SeedUser, people: dict[str, Person], created: str) -> wire.StoredAccount:
    name, email = user.name, None
    if user.person is not None:
        if user.person not in people:
            raise ValueError(f"GitHub user {user.login} is person {user.person}, who is nobody in the scenario")
        name, email = people[user.person].name, people[user.person].email
    return wire.StoredAccount(
        login=user.login, id=number(user.login), type=wire.AccountType.USER, name=name, email=email, created_at=created
    )


def _login(account: wire.StoredAccount) -> str | None:
    return account.login if account.type is wire.AccountType.USER else None


def _name(account: wire.StoredAccount, where: str) -> str:
    """A commit's author or committer name: the account's declared name (its own, or its person's). A login is not
    a name, and nothing else is made up: an account with none is refused, naming the commit."""
    if account.name is None:
        raise ValueError(f"{where}: {account.login} has no name; declare the account's name or its person")
    return account.name


def _email(account: wire.StoredAccount) -> str:
    """The account's email, else GitHub's no-reply address for an account that keeps its email private:
    https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address"""
    return account.email or f"{account.id}+{account.login}@users.noreply.github.com"


def _commits(
    repository: SeedRepository, accounts: dict[str, wire.StoredAccount], start: datetime
) -> list[wire.StoredCommit]:
    full_name = f"{repository.owner}/{repository.name}"
    written = list(repository.commits)
    made: list[wire.StoredCommit] = []
    parent: str | None = None
    for position, commit in enumerate(written):
        author = accounts[commit.author.lower()]
        committer = author if commit.committer is None else accounts[commit.committer.lower()]
        date = wire.timestamp(start - commit.before)
        committed = date if commit.committed_before is None else wire.timestamp(start - commit.committed_before)
        where = f"{full_name}: the commit {commit.message.splitlines()[0]!r}"
        sha = _commit_sha(full_name, position, commit.message, date)
        made.append(
            wire.StoredCommit(
                sha=sha,
                message=commit.message,
                author_login=_login(author),
                author_name=_name(author, where),
                author_email=_email(author),
                committer_login=_login(committer),
                committer_name=_name(committer, where),
                committer_email=_email(committer),
                committer_date=committed,
                date=date,
                paths=commit.paths,
                parent=parent,
            )
        )
        parent = sha
    return list(reversed(made))


def _stored_blob(raw: bytes) -> wire.StoredBlob:
    return wire.StoredBlob(sha=wire.blob_sha(raw), content=base64.b64encode(raw).decode("ascii"), size=len(raw))


def _lines(
    repository: SeedRepository,
    accounts: dict[str, wire.StoredAccount],
    trunk: list[wire.StoredCommit],
    start: datetime,
) -> tuple[list[wire.StoredLine], list[wire.StoredBlob]]:
    """Each branch with commits of its own: made at the default branch's head, every commit the paths it changed and
    the blobs they hold, checked against the tree it is made on (a path it deletes or replaces is there; a file is
    not also a directory)."""
    if not repository.diverged_branches:
        return [], []
    full_name = f"{repository.owner}/{repository.name}"
    head = trunk[0]
    base = {f.path: wire.blob_sha(f.raw()) for f in repository.files}
    lines: list[wire.StoredLine] = []
    blobs: list[wire.StoredBlob] = []
    for branch in repository.diverged_branches:
        tree = dict(base)
        parent = head.sha
        made: list[wire.StoredCommit] = []
        for position, commit in enumerate(branch.commits):
            where = f"{full_name}: {branch.name}'s commit {commit.message.splitlines()[0]!r}"
            author = accounts[commit.author.lower()]
            date = wire.timestamp(start - commit.before)
            changes: list[wire.FileChange] = []
            for change in commit.changes:
                previous = tree.get(change.path)
                if change.delete:
                    if previous is None:
                        raise ValueError(f"{where} deletes {change.path}, which the branch has not")
                    del tree[change.path]
                    changes.append(
                        wire.FileChange(
                            path=change.path, status=wire.ChangeStatus.REMOVED, sha=None, previous_sha=previous
                        )
                    )
                    continue
                blob = _stored_blob(change.raw())
                blobs.append(blob)
                if previous == blob.sha:
                    raise ValueError(f"{where} gives {change.path} the bytes it already has")
                held = [p for p in tree if p.startswith(change.path + "/") or change.path.startswith(p + "/")]
                if held and previous is None:
                    raise ValueError(f"{where}: {change.path} would be both a file and a directory")
                status = wire.ChangeStatus.ADDED if previous is None else wire.ChangeStatus.MODIFIED
                changes.append(wire.FileChange(path=change.path, status=status, sha=blob.sha, previous_sha=previous))
                tree[change.path] = blob.sha
            sha = _commit_sha(f"{full_name}@{branch.name}", position, commit.message, date)
            made.append(
                wire.StoredCommit(
                    sha=sha,
                    message=commit.message,
                    author_login=_login(author),
                    author_name=_name(author, where),
                    author_email=_email(author),
                    committer_login=_login(author),
                    committer_name=_name(author, where),
                    committer_email=_email(author),
                    committer_date=date,
                    date=date,
                    paths=[c.path for c in changes],
                    parent=parent,
                    changes=changes,
                )
            )
            parent = sha
        lines.append(
            wire.StoredLine(
                name=branch.name,
                fork=head.sha,
                base=[wire.TreeEntry(path=p, sha=b) for p, b in sorted(base.items())],
                commits=list(reversed(made)),
            )
        )
    return lines, blobs


def _tracker(
    github: GitHubWorld,
    stored: wire.StoredRepository,
    repository: SeedRepository,
    accounts: dict[str, wire.StoredAccount],
    start: datetime,
) -> None:
    """The repository's labels, issues and pull requests, each with its comments and reviews, as the seed left them."""
    full = stored.full_name

    def login(name: str) -> str:
        return accounts[name.lower()].login

    def at(before: timedelta) -> str:
        return wire.timestamp(start - before)

    for label in repository.labels:
        github.put_label(
            stored,
            wire.StoredLabel(
                id=number(f"label/{full}/{label.name}"),
                name=label.name,
                color=label.color,
                description=label.description,
                default=label.default,
            ),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
        )
    lines = {line.name: line for line in stored.lines}
    items: list[SeedIssue | SeedPull] = sorted([*repository.issues, *repository.pulls], key=lambda i: i.number)
    for item in items:
        pull: wire.StoredPull | None = None
        tip: str | None = None
        if isinstance(item, SeedPull):
            line = lines[item.head]
            tip = line.commits[0].sha
            closed = item.state is wire.IssueState.CLOSED
            pull = wire.StoredPull(
                id=number(f"pull/{full}/{item.number}"),
                head=item.head,
                base=stored.default_branch,
                draft=item.draft,
                requested_reviewers=[login(r) for r in item.requested_reviewers],
                head_sha=tip if closed else None,
                base_sha=stored.commits[0].sha if closed else None,
            )
        moments = [item.before, *(c.before for c in item.comments)]
        if isinstance(item, SeedPull):
            moments += [v.before for v in item.reviews]
        if item.closed_before is not None:
            moments.append(item.closed_before)
        github.put_issue(
            stored,
            wire.StoredIssue(
                number=item.number,
                id=number(f"issue/{full}/{item.number}"),
                title=item.title,
                body=item.body,
                author=login(item.author),
                state=item.state,
                state_reason=item.state_reason if isinstance(item, SeedIssue) else None,
                labels=item.labels,
                assignees=[login(a) for a in item.assignees],
                locked=item.locked if isinstance(item, SeedIssue) else False,
                lock_reason=item.lock_reason if isinstance(item, SeedIssue) else None,
                created_at=at(item.before),
                updated_at=at(min(moments)),
                closed_at=None if item.closed_before is None else at(item.closed_before),
                closed_by=None if item.closed_before is None else login(item.closed_by or item.author),
                pull=pull,
            ),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
        )
        for position, comment in enumerate(item.comments):
            github.put_comment(
                stored,
                wire.StoredComment(
                    id=number(f"comment/{full}/{item.number}/{position}"),
                    issue=item.number,
                    author=login(comment.author),
                    body=comment.body,
                    created_at=at(comment.before),
                    updated_at=at(comment.before),
                ),
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
            )
        if isinstance(item, SeedPull):
            assert tip is not None
            for position, review in enumerate(item.reviews):
                github.put_review(
                    stored,
                    wire.StoredReview(
                        id=number(f"review/{full}/{item.number}/{position}"),
                        pull=item.number,
                        author=login(review.author),
                        state=review.state,
                        body=review.body,
                        commit_id=tip,
                        submitted_at=at(review.before),
                    ),
                    operation=Operation.CREATE,
                    actor=Actor.SCENARIO,
                )


def seed(given: GitHubSeed, scenario: Scenario, world: Store) -> None:
    """Write `given` into the world; people it names are the scenario's."""
    github = GitHubWorld(world)
    created = wire.timestamp(scenario.starts_at)
    people = _people(scenario)

    accounts: dict[str, wire.StoredAccount] = {}
    for user in given.users:
        accounts[user.login.lower()] = _user(user, people, created)
    for organization in given.organizations:
        accounts[organization.login.lower()] = wire.StoredAccount(
            login=organization.login,
            id=number(organization.login),
            type=wire.AccountType.ORGANIZATION,
            name=organization.name,
            members=[accounts[m.lower()].login for m in organization.members],
            created_at=created,
        )
    for account in accounts.values():
        github.write_account(account)

    for token in given.tokens:
        github.write_token(
            token.token,
            wire.StoredToken(
                kind=token.kind,
                login=accounts[token.login.lower()].login,
                scopes=token.scopes if token.kind is wire.TokenKind.CLASSIC else [],
            ),
        )
    _stand_in(github, given, accounts)

    for repository in given.repositories:
        owner = accounts[repository.owner.lower()]
        commits = _commits(repository, accounts, scenario.starts_at)
        lines, line_blobs = _lines(repository, accounts, commits, scenario.starts_at)
        stored = wire.StoredRepository(
            id=number(f"{owner.login}/{repository.name}"),
            owner=owner.login,
            name=repository.name,
            private=repository.private,
            description=repository.description,
            homepage=repository.homepage,
            topics=repository.topics,
            license=repository.license,
            default_branch=repository.default_branch,
            branches=repository.branches,
            collaborators=[
                c.model_copy(update={"login": accounts[c.login.lower()].login}) for c in repository.collaborators
            ],
            commits=commits,
            lines=lines,
            stargazers_count=repository.stargazers_count,
            forks_count=repository.forks_count,
            network_count=repository.forks_count if repository.network_count is None else repository.network_count,
            subscribers_count=repository.subscribers_count,
            tree_entry_limit=repository.tree_entry_limit,
            directory_entry_limit=repository.directory_entry_limit,
            created_at=commits[-1].date if commits else created,
        )
        github.write_repository(stored)
        for file in repository.files:
            raw = file.raw()
            github.write_file(
                stored,
                wire.StoredFile(
                    path=file.path, content=base64.b64encode(raw).decode("ascii"), size=len(raw), sha=wire.blob_sha(raw)
                ),
            )
            github.put_blob(stored, _stored_blob(raw), actor=Actor.SCENARIO)
        for blob in line_blobs:
            github.put_blob(stored, blob, actor=Actor.SCENARIO)
        _tracker(github, stored, repository, accounts, scenario.starts_at)

    write_faults(github, given.faults)
    write_limits(github, given.limits)
    for budget in given.budgets:
        limit = wire.limit_for(budget.resource, authenticated=budget.login is not None)
        login = None if budget.login is None else accounts[budget.login.lower()].login
        reset = math.ceil(scenario.starts_at.timestamp()) + wire.WINDOW_SECONDS[budget.resource]
        github.write_budget(
            login, budget.resource, wire.StoredBudget(limit=limit, used=limit - budget.remaining, reset=reset)
        )


def _stand_in(github: GitHubWorld, given: GitHubSeed, accounts: dict[str, wire.StoredAccount]) -> None:
    """Who a credential the world does not hold acts as: the user the seed names, else the first user a seed
    lists, kept from the first seed that lists one so a later fragment does not move it."""
    if given.unknown_credentials_act_as is not None:
        github.write_stand_in(wire.StoredStandIn(login=accounts[given.unknown_credentials_act_as.lower()].login))
    elif given.users and github.stand_in() is None:
        github.write_stand_in(wire.StoredStandIn(login=accounts[given.users[0].login.lower()].login))


def github_seed(scenario: Scenario) -> GitHubSeed:
    """The scenario's own seed for GitHub, or an empty GitHub when it gives none."""
    found = scenario.provider_seed(MANIFEST.key)
    return GitHubSeed() if found is None else GitHubSeed.model_validate_json(found.body)


DECLARED = 1_000_000
"""Where the numbers of faults declared on an open world (`provider-faults`) start: above every number a seed gives
its own faults, which count from 0 in the seed's order, so a fault a later seed fragment adds never takes the
number of one declared before it, and the seed's are armed ahead of the declared ones."""


def write_faults(github: GitHubWorld, faults: list[wire.Fault], *, declared: bool = False) -> None:
    """Arm each fault after those already armed."""
    first = (DECLARED if declared else 0) + len(github.faults())
    for position, fault in enumerate(faults, start=first):
        github.arm(position, wire.StoredFault(fault=fault))


def write_limits(github: GitHubWorld, limits: list[SeedLimits]) -> None:
    """Set each repository's reading limits, as the scenario; a repository the world does not hold is refused."""
    for limit in limits:
        owner, _, name = limit.repository.partition("/")
        stored = github.repository(owner, name)
        if stored is None:
            raise ValueError(f"no repository {limit.repository} in this world")
        changed = stored.model_copy(
            update={
                "tree_entry_limit": limit.tree_entry_limit or stored.tree_entry_limit,
                "directory_entry_limit": limit.directory_entry_limit or stored.directory_entry_limit,
            }
        )
        github.update_repository(changed)
