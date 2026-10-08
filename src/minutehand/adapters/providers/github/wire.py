"""GitHub's own JSON: the only module that parses or builds it.

Four families live here:

- **Stored** — `StoredAccount`, `StoredToken`, `StoredRepository`, `StoredFile`, `StoredFault`: the body of each
  record in the store, holding only what an answer is built from.
- **Faults** — `RateLimited`, `SecondaryRateLimited`, `ServerError`: what a scenario arms, typed, and answered
  in GitHub's own shapes.
- **Answers** — the resources as the REST API returns them, in GitHub's snake_case.
- **Errors** — `Refusal`, answered as `{"message": …, "documentation_url": …, "status": …}` with `errors` when
  a validation failed.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeVar

from pydantic import ConfigDict, Field, JsonValue

from minutehand.domain.errors import Asked, Rendered, ServiceRefusal
from minutehand.domain.scenario import Model

API = "https://api.github.com"
WEB = "https://github.com"
RAW = "https://raw.githubusercontent.com"
DOCS = "https://docs.github.com/rest"
"""Where an error points its reader: the reference's section for the resource, never a vendor page copied."""

JSON = "application/json; charset=utf-8"
API_VERSIONS = ("2022-11-28",)
"""The `X-GitHub-Api-Version` values answered."""
UNSERVED_API_VERSIONS = ("2026-03-10",)
"""Versions GitHub answers (observed 2026-10-08, `tests/providers/github/observed/`) and this provider does not:
refused by name (501). Any other is GitHub's 400."""


class Wire(Model):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- vocabularies


class AccountType(StrEnum):
    USER = "User"
    ORGANIZATION = "Organization"


class TokenKind(StrEnum):
    """The personal access tokens GitHub issues, each known by the prefix GitHub writes on it."""

    CLASSIC = "classic"  # ghp_…, scoped by OAuth scopes
    FINE_GRAINED = "fine_grained"  # github_pat_…, scoped to repositories


TOKEN_PREFIXES: dict[TokenKind, str] = {TokenKind.CLASSIC: "ghp_", TokenKind.FINE_GRAINED: "github_pat_"}


class Permission(StrEnum):
    """A role on a repository, weakest first."""

    PULL = "pull"
    TRIAGE = "triage"
    PUSH = "push"
    MAINTAIN = "maintain"
    ADMIN = "admin"


PERMISSION_ORDER = [Permission.PULL, Permission.TRIAGE, Permission.PUSH, Permission.MAINTAIN, Permission.ADMIN]


class Resource(StrEnum):
    """The rate-limit budget a call spends, as GitHub names it in `X-RateLimit-Resource`."""

    CORE = "core"
    SEARCH = "search"
    CODE_SEARCH = "code_search"
    GRAPHQL = "graphql"


WINDOW_SECONDS: dict[Resource, int] = {
    Resource.CORE: 3600,
    Resource.SEARCH: 60,
    Resource.CODE_SEARCH: 60,
    Resource.GRAPHQL: 3600,
}
LIMITS: dict[Resource, int] = {
    Resource.CORE: 5000,
    Resource.SEARCH: 30,
    Resource.CODE_SEARCH: 10,
    Resource.GRAPHQL: 5000,
}
ANONYMOUS_LIMITS: dict[Resource, int] = {
    Resource.CORE: 60,
    Resource.SEARCH: 10,
    Resource.CODE_SEARCH: 10,
    Resource.GRAPHQL: 0,
}
"""A call with no credential spends the address's budget: 60 an hour for the REST core, and nothing on GraphQL."""


def limit_for(resource: Resource, *, authenticated: bool) -> int:
    return (LIMITS if authenticated else ANONYMOUS_LIMITS)[resource]


class StoredBudget(Wire):
    """One user's (or the address's) primary budget for one resource, in its current window: spent calls and the
    epoch second the window ends. A window that has ended is a whole budget again."""

    limit: int = Field(ge=0)
    used: int = Field(ge=0)
    reset: int = Field(description="UTC epoch seconds, as `X-RateLimit-Reset` carries them")

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)


class BudgetOut(Wire):
    """One resource in `GET /rate_limit`."""

    limit: int
    remaining: int
    used: int
    reset: int
    resource: Resource


class RateLimitOut(Wire):
    resources: dict[Resource, BudgetOut]
    rate: BudgetOut


# --------------------------------------------------------------------------- errors


class FieldError(Wire):
    """One entry of a 422's `errors`."""

    resource: str
    field: str
    code: str
    message: str | None = None


class Refusal(ServiceRefusal):
    """GitHub answered with an error status. `message` is the answer's own `message`."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        section: str = "",
        errors: list[FieldError] | str | None = None,
        headers: dict[str, str] | None = None,
        documentation_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.documentation_url = DOCS + section if documentation_url is None else documentation_url
        self.errors = errors
        self.headers = headers or {}

    def render(self, asked: Asked) -> Rendered:
        """`{"message", "documentation_url", "status"}` and the headers the refusal carries. The app adds the
        caller's rate-limit headers to a refusal it answers itself; one answered here has no caller to read."""
        headers = [(name.lower(), value) for name, value in self.headers.items()]
        return Rendered(status=self.status, content_type=JSON, body=error_body(self), headers=headers)


class ErrorOut(Wire):
    """GitHub's REST error body. `errors` is a list of field errors on a 422 and, observed, a sentence on the 400
    for an unsupported API version."""

    message: str
    errors: list[FieldError] | str | None = None
    documentation_url: str
    status: str


def error_body(refusal: Refusal) -> bytes:
    answer = ErrorOut(
        message=refusal.message,
        errors=refusal.errors,
        documentation_url=refusal.documentation_url,
        status=str(refusal.status),
    )
    return answer.model_dump_json(exclude_none=True).encode()


def error_answer(status: int, message: str) -> Rendered:
    """What Minutehand answers in GitHub's place (501, 500), in GitHub's REST error body: a client reads the
    `message`. GitHub's REST errors carry no code."""
    answer = ErrorOut(message=message, documentation_url=DOCS, status=str(status))
    return Rendered(status=status, content_type=JSON, body=answer.model_dump_json(exclude_none=True).encode())


def not_found(section: str = "") -> Refusal:
    """What GitHub answers for a thing that does not exist and for one the caller may not see: the same 404."""
    return Refusal(404, "Not Found", section=section)


def requires_authentication() -> Refusal:
    """Observed 2026-10-08 on `GET /user` with no credential: its `documentation_url` is the reference's root."""
    return Refusal(401, "Requires authentication")


def unsupported_version(version: str) -> Refusal:
    """Observed 2026-10-08: 400 "Bad Request", the reason in `errors` as a sentence naming the versions GitHub
    answers, the reference's root as `documentation_url`."""
    answered = " and ".join([f'"{v}" (most recent)' for v in UNSERVED_API_VERSIONS] + [f'"{v}"' for v in API_VERSIONS])
    return Refusal(
        400,
        "Bad Request",
        errors=f'The version you specified in the "X-GitHub-API-Version" request header, "{version}", is not a '
        f"supported version. The following versions are currently supported: {answered}.",
    )


def validation_failed(section: str, *errors: FieldError) -> Refusal:
    return Refusal(422, "Validation Failed", section=section, errors=list(errors))


# --------------------------------------------------------------------------- faults


class RateLimited(Wire):
    """The primary rate limit is spent for `resource`: a 403 (or 429) with `X-RateLimit-Remaining: 0` and the
    reset moment; on GraphQL a 200 whose `errors` say RATE_LIMITED, as GitHub answers its GraphQL budget."""

    kind: Literal["rate_limited"] = "rate_limited"
    resource: Resource
    times: int = Field(default=1, ge=1, description="How many calls it answers before it is spent")
    status: Literal[403, 429] = 403


class SecondaryRateLimited(Wire):
    """A secondary (abuse) limit: a 403 (or 429) carrying `Retry-After`."""

    kind: Literal["secondary_rate_limited"] = "secondary_rate_limited"
    resource: Resource | None = Field(default=None, description="None: any call")
    times: int = Field(default=1, ge=1)
    status: Literal[403, 429] = 403
    retry_after: int = Field(default=60, ge=0, description="Seconds, as `Retry-After` carries them")


class ServerError(Wire):
    """GitHub failing on its side."""

    kind: Literal["server_error"] = "server_error"
    resource: Resource | None = Field(default=None, description="None: any call")
    times: int = Field(default=1, ge=1)
    status: Literal[500, 502, 503, 504] = 502


Fault = Annotated[RateLimited | SecondaryRateLimited | ServerError, Field(discriminator="kind")]


# --------------------------------------------------------------------------- stored


class StoredAccount(Wire):
    """A user or an organization: GitHub keeps both in one namespace of logins."""

    login: str
    id: int
    type: AccountType
    name: str | None = None
    email: str | None = None
    members: list[str] = Field(default=[], description="An organization's member logins")
    created_at: str


class StoredToken(Wire):
    """A personal access token, kept under a digest of itself: the token's own text is never written down. Who it
    acts as is world data; what it was issued for is not enforced (README, "Credentials")."""

    kind: TokenKind
    login: str
    scopes: list[str] = Field(default=[], description="A classic token's OAuth scopes, echoed in `X-OAuth-Scopes`")


class StoredStandIn(Wire):
    """Who a credential the world does not hold acts as: Minutehand accepts every credential (README,
    "Credentials"), and the world names the account an unknown one, a JWT or an installation token stands for."""

    login: str


class StoredInstallationToken(Wire):
    """An installation access token GitHub issued on `POST /app/installations/{installation_id}/access_tokens`,
    kept as the exchange left it: the token's own text is never written down, only what the answer carried."""

    installation_id: int
    expires_at: str
    permissions: dict[str, str] | None = Field(description="The permissions the request asked for, as sent")


class Collaborator(Wire):
    login: str
    permission: Permission


class License(Wire):
    key: str
    name: str
    spdx_id: str


class StoredCommit(Wire):
    sha: str
    message: str
    author_login: str | None
    author_name: str
    author_email: str
    date: str
    paths: list[str]
    parent: str | None


class StoredRepository(Wire):
    id: int
    owner: str
    name: str
    private: bool
    description: str | None
    homepage: str | None
    topics: list[str]
    license: License | None
    default_branch: str
    branches: list[str]
    collaborators: list[Collaborator]
    commits: list[StoredCommit] = Field(description="Newest first; every branch points at the first")
    stargazers_count: int
    forks_count: int
    network_count: int
    subscribers_count: int
    tree_entry_limit: int
    directory_entry_limit: int = 1000
    created_at: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class StoredFile(Wire):
    path: str
    content: str = Field(description="The bytes, base64, unwrapped")
    size: int
    sha: str


class StoredFault(Wire):
    fault: Fault
    answered: int = 0


StoredModel = TypeVar(
    "StoredModel",
    StoredAccount,
    StoredToken,
    StoredStandIn,
    StoredInstallationToken,
    StoredRepository,
    StoredFile,
    StoredFault,
    StoredBudget,
)


def parse(model: type[StoredModel], body: str) -> StoredModel:
    return model.model_validate_json(body)


def dump(entity: Wire) -> str:
    return entity.model_dump_json(by_alias=True)


# --------------------------------------------------------------------------- encoding


def timestamp(at: datetime) -> str:
    """A moment as GitHub writes one: UTC, to the second, with a Z."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def blob_sha(raw: bytes) -> str:
    """The git object id of a blob: the sha the contents, trees and blobs endpoints all agree on."""
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def wrapped_base64(raw: bytes) -> str:
    """Base64 in 60-character lines, each ended by a newline: how GitHub carries file bytes in JSON."""
    encoded = base64.b64encode(raw).decode("ascii")
    return "".join(encoded[i : i + 60] + "\n" for i in range(0, len(encoded), 60))


def node_id(kind: str, number: int) -> str:
    return kind + "_" + base64.urlsafe_b64encode(f"{kind}{number}".encode()).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- answers


class AccountOut(Wire):
    """An account as it appears inside another resource (an owner, an author): the description's `simple-user`,
    every URL GitHub assigns from the login."""

    login: str
    id: int
    node_id: str
    avatar_url: str
    gravatar_id: str = ""
    url: str
    html_url: str
    followers_url: str
    following_url: str
    gists_url: str
    starred_url: str
    subscriptions_url: str
    organizations_url: str
    repos_url: str
    events_url: str
    received_events_url: str
    type: AccountType
    site_admin: bool = False

    @classmethod
    def of(cls, account: StoredAccount) -> AccountOut:
        at = f"{API}/users/{account.login}"
        return cls(
            login=account.login,
            id=account.id,
            node_id=node_id("U" if account.type is AccountType.USER else "O", account.id),
            avatar_url=f"https://avatars.githubusercontent.com/u/{account.id}?v=4",
            url=at,
            html_url=f"{WEB}/{account.login}",
            followers_url=f"{at}/followers",
            following_url=f"{at}/following{{/other_user}}",
            gists_url=f"{at}/gists{{/gist_id}}",
            starred_url=f"{at}/starred{{/owner}}{{/repo}}",
            subscriptions_url=f"{at}/subscriptions",
            organizations_url=f"{at}/orgs",
            repos_url=f"{at}/repos",
            events_url=f"{at}/events{{/privacy}}",
            received_events_url=f"{at}/received_events",
            type=account.type,
        )


class UserOut(AccountOut):
    """`GET /user`: the account a credential acts as, in the description's `public-user` view: the private view's
    extra fields (disk usage, two-factor, private gists) are nothing the world holds. Counts are the world's: no
    follows and no gists are in it."""

    user_view_type: Literal["public"] = "public"
    name: str | None
    company: str | None = None
    blog: str = ""
    location: str | None = None
    email: str | None
    hireable: bool | None = None
    bio: str | None = None
    public_repos: int
    public_gists: int = 0
    followers: int = 0
    following: int = 0
    created_at: str
    updated_at: str


class PermissionsOut(Wire):
    admin: bool
    maintain: bool
    push: bool
    triage: bool
    pull: bool


class LicenseOut(Wire):
    key: str
    name: str
    spdx_id: str
    url: str
    node_id: str


class RepositoryLinksOut(Wire):
    """The URLs GitHub assigns a repository from its full name."""

    html_url: str
    url: str
    archive_url: str
    assignees_url: str
    blobs_url: str
    branches_url: str
    collaborators_url: str
    comments_url: str
    commits_url: str
    compare_url: str
    contents_url: str
    contributors_url: str
    deployments_url: str
    downloads_url: str
    events_url: str
    forks_url: str
    git_commits_url: str
    git_refs_url: str
    git_tags_url: str
    hooks_url: str
    issue_comment_url: str
    issue_events_url: str
    issues_url: str
    keys_url: str
    labels_url: str
    languages_url: str
    merges_url: str
    milestones_url: str
    notifications_url: str
    pulls_url: str
    releases_url: str
    stargazers_url: str
    statuses_url: str
    subscribers_url: str
    subscription_url: str
    tags_url: str
    teams_url: str
    trees_url: str

    @classmethod
    def of(cls, full_name: str) -> RepositoryLinksOut:
        at = f"{API}/repos/{full_name}"
        return cls(
            html_url=f"{WEB}/{full_name}",
            url=at,
            archive_url=f"{at}/{{archive_format}}{{/ref}}",
            assignees_url=f"{at}/assignees{{/user}}",
            blobs_url=f"{at}/git/blobs{{/sha}}",
            branches_url=f"{at}/branches{{/branch}}",
            collaborators_url=f"{at}/collaborators{{/collaborator}}",
            comments_url=f"{at}/comments{{/number}}",
            commits_url=f"{at}/commits{{/sha}}",
            compare_url=f"{at}/compare/{{base}}...{{head}}",
            contents_url=f"{at}/contents/{{+path}}",
            contributors_url=f"{at}/contributors",
            deployments_url=f"{at}/deployments",
            downloads_url=f"{at}/downloads",
            events_url=f"{at}/events",
            forks_url=f"{at}/forks",
            git_commits_url=f"{at}/git/commits{{/sha}}",
            git_refs_url=f"{at}/git/refs{{/sha}}",
            git_tags_url=f"{at}/git/tags{{/sha}}",
            hooks_url=f"{at}/hooks",
            issue_comment_url=f"{at}/issues/comments{{/number}}",
            issue_events_url=f"{at}/issues/events{{/number}}",
            issues_url=f"{at}/issues{{/number}}",
            keys_url=f"{at}/keys{{/key_id}}",
            labels_url=f"{at}/labels{{/name}}",
            languages_url=f"{at}/languages",
            merges_url=f"{at}/merges",
            milestones_url=f"{at}/milestones{{/number}}",
            notifications_url=f"{at}/notifications{{?since,all,participating}}",
            pulls_url=f"{at}/pulls{{/number}}",
            releases_url=f"{at}/releases{{/id}}",
            stargazers_url=f"{at}/stargazers",
            statuses_url=f"{at}/statuses/{{sha}}",
            subscribers_url=f"{at}/subscribers",
            subscription_url=f"{at}/subscription",
            tags_url=f"{at}/tags",
            teams_url=f"{at}/teams",
            trees_url=f"{at}/git/trees{{/sha}}",
        )


class MinimalRepositoryOut(RepositoryLinksOut):
    """A repository inside another answer (a search hit): the description's `minimal-repository`."""

    id: int
    node_id: str
    name: str
    full_name: str
    private: bool
    owner: AccountOut
    description: str | None
    fork: bool = False


class RepositoryOut(MinimalRepositoryOut):
    """`GET /repos/{owner}/{repo}` and each of `/user/repos`: the description's `full-repository`. The features a
    repository has on (`has_issues` and the rest) are GitHub's defaults for a new repository, which nothing in the
    world changes. https://docs.github.com/en/rest/repos/repos#create-a-repository-for-the-authenticated-user"""

    git_url: str
    ssh_url: str
    clone_url: str
    svn_url: str
    mirror_url: str | None = None
    homepage: str | None
    size: int
    stargazers_count: int
    watchers_count: int
    language: str | None
    forks_count: int
    archived: bool = False
    disabled: bool = False
    open_issues_count: int = 0
    license: LicenseOut | None
    topics: list[str]
    visibility: Literal["public", "private"]
    forks: int
    open_issues: int = 0
    watchers: int
    default_branch: str
    has_issues: bool = True
    has_projects: bool = True
    has_wiki: bool = True
    has_pages: bool = False
    has_downloads: bool = True
    has_discussions: bool = False
    network_count: int
    subscribers_count: int
    permissions: PermissionsOut | None
    created_at: str
    updated_at: str
    pushed_at: str


class LinksOut(Wire):
    self_: str = Field(alias="self")
    git: str
    html: str


class EntryOut(Wire):
    """One entry of a directory listing: no bytes."""

    type: Literal["file", "dir"]
    size: int
    name: str
    path: str
    sha: str
    url: str
    git_url: str
    html_url: str
    download_url: str | None
    links: LinksOut = Field(alias="_links")


class FileOut(EntryOut):
    """A file's contents: its bytes as wrapped base64, or none past the inline limit."""

    type: Literal["file", "dir"] = "file"
    encoding: Literal["base64", "none"]
    content: str


class BlobOut(Wire):
    sha: str
    node_id: str
    size: int
    url: str
    content: str
    encoding: Literal["base64"] = "base64"


class TreeEntryOut(Wire):
    path: str
    mode: Literal["100644", "040000"]
    type: Literal["blob", "tree"]
    sha: str
    size: int | None = None
    url: str


class TreeOut(Wire):
    sha: str
    url: str
    tree: list[TreeEntryOut]
    truncated: bool


class PersonOut(Wire):
    name: str
    email: str
    date: str


class ShaRefOut(Wire):
    sha: str
    url: str


class ParentOut(ShaRefOut):
    html_url: str


class CommitDetailOut(Wire):
    author: PersonOut
    committer: PersonOut
    message: str
    tree: ShaRefOut
    url: str
    comment_count: int = 0


class CommitOut(Wire):
    sha: str
    node_id: str
    commit: CommitDetailOut
    url: str
    html_url: str
    comments_url: str
    author: AccountOut | None
    committer: AccountOut | None
    parents: list[ParentOut]


class CodeItemOut(Wire):
    name: str
    path: str
    sha: str
    url: str
    git_url: str
    html_url: str
    repository: MinimalRepositoryOut
    score: float


class TermMatchOut(Wire):
    """Where one search term sits in a text match's fragment: its text and its start and end offsets."""

    text: str
    indices: list[int]


class TextMatchOut(Wire):
    """One `text_matches` entry, sent only under the `application/vnd.github.text-match+json` media type."""

    object_url: str
    object_type: str
    property: str
    fragment: str
    matches: list[TermMatchOut]


class MatchedCodeItemOut(CodeItemOut):
    text_matches: list[TextMatchOut]


class CodeSearchOut(Wire):
    total_count: int
    incomplete_results: bool
    items: list[CodeItemOut]


class MatchedCodeSearchOut(Wire):
    total_count: int
    incomplete_results: bool
    items: list[MatchedCodeItemOut]


class BranchOut(Wire):
    name: str
    commit: ShaRefOut
    protected: bool


class GraphErrorLocation(Wire):
    line: int
    column: int


class GraphError(Wire):
    """One entry of a GraphQL answer's `errors`."""

    type: str | None = None
    path: list[str] | None = None
    locations: list[GraphErrorLocation] | None = None
    extensions: dict[str, str] | None = None
    message: str


class GraphErrorsOut(Wire):
    errors: list[GraphError]


class InstallationTokenIn(Wire):
    """`POST /app/installations/{installation_id}/access_tokens`: what the token may reach. Every field is
    optional and the body may be absent; a key the reference does not name is let by, never refused.
    https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    permissions: dict[str, str] | None = None
    repositories: list[str] | None = None
    repository_ids: list[int] | None = None


class InstallationTokenOut(Wire):
    """The installation token: `token` and `expires_at` are all the schema requires; `permissions` is carried
    when the request named them, as it named them."""

    token: str
    expires_at: str
    permissions: dict[str, str] | None = None


class GraphQLIn(Wire):
    """A GraphQL request's body: the query, its variables and the operation to run. Any other key is refused."""

    query: str
    variables: dict[str, JsonValue] | None = None
    operation_name: str | None = Field(default=None, alias="operationName")
