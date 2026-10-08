"""The GitHub a scenario starts with.

`GitHubSeed` is this provider's own seed model: accounts (users, each optionally a scenario person),
organizations and their members, personal access tokens, repositories with their files, history, collaborators
and branches, the faults armed against the agent, and primary rate-limit budgets that start part spent. The shared scenario has no place for it yet, so it is handed to
`seed()` beside the scenario (`GitHubProvider.seed_with`); a world seeded from the scenario alone has an empty
GitHub, in which every token is unknown.

Every commit's date is an offset before the scenario's start, so the same file replays on any day. A repository
with files and no commits is given one, "Initial commit", by its owner at the start; one with neither is empty,
as a repository just created is.

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
    before: timedelta = Field(description="How long before the scenario's start it was made")
    paths: list[str] = Field(default=[], description="The paths it changed; what `commits?path=` filters on")


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
        befores = [c.before for c in self.commits]
        if any(later >= earlier for earlier, later in pairwise(befores)):
            raise ValueError(f"{self.owner}/{self.name}: commits are oldest first, each made after the one before")
        if self.default_branch in self.branches:
            raise ValueError(f"{self.owner}/{self.name}: {self.default_branch} is the default branch already")
        return self


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


def _commits(
    repository: SeedRepository, accounts: dict[str, wire.StoredAccount], start: datetime
) -> list[wire.StoredCommit]:
    full_name = f"{repository.owner}/{repository.name}"
    written = list(repository.commits)
    if not written and repository.files:
        owner = accounts[repository.owner.lower()]
        initial = SeedCommit(message="Initial commit", author=owner.login, before=timedelta(0))
        written = [initial.model_copy(update={"paths": [f.path for f in repository.files]})]
    made: list[wire.StoredCommit] = []
    parent: str | None = None
    for position, commit in enumerate(written):
        author = accounts[commit.author.lower()]
        date = wire.timestamp(start - commit.before)
        sha = _commit_sha(full_name, position, commit.message, date)
        made.append(
            wire.StoredCommit(
                sha=sha,
                message=commit.message,
                author_login=author.login if author.type is wire.AccountType.USER else None,
                author_name=author.name or author.login,
                # GitHub's no-reply address for an account that keeps its email private:
                # https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address
                author_email=author.email or f"{author.id}+{author.login}@users.noreply.github.com",
                date=date,
                paths=commit.paths,
                parent=parent,
            )
        )
        parent = sha
    return list(reversed(made))


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
