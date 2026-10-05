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

from minutehand.domain.scenario import Model

API = "https://api.github.com"
WEB = "https://github.com"
RAW = "https://raw.githubusercontent.com"
DOCS = "https://docs.github.com/rest"
"""Where an error points its reader: the reference's section for the resource, never a vendor page copied."""

JSON = "application/json; charset=utf-8"
API_VERSIONS = ("2022-11-28",)
"""The `X-GitHub-Api-Version` values answered. Any other is refused with 400, as GitHub refuses an unknown one."""


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


# --------------------------------------------------------------------------- errors


class FieldError(Wire):
    """One entry of a 422's `errors`."""

    resource: str
    field: str
    code: str
    message: str | None = None


class Refusal(Exception):
    """GitHub answered with an error status. `message` is the answer's own `message`."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        section: str = "",
        errors: list[FieldError] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.section = section
        self.errors = errors
        self.headers = headers or {}


class ErrorOut(Wire):
    message: str
    errors: list[FieldError] | None = None
    documentation_url: str
    status: str


def error_body(refusal: Refusal) -> bytes:
    answer = ErrorOut(
        message=refusal.message,
        errors=refusal.errors,
        documentation_url=DOCS + refusal.section,
        status=str(refusal.status),
    )
    return answer.model_dump_json(exclude_none=True).encode()


def not_found(section: str = "") -> Refusal:
    """What GitHub answers for a thing that does not exist and for one the caller may not see: the same 404."""
    return Refusal(404, "Not Found", section=section)


def bad_credentials() -> Refusal:
    return Refusal(401, "Bad credentials", section="/authentication")


def requires_authentication() -> Refusal:
    return Refusal(401, "Requires authentication", section="/authentication")


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
    """A personal access token, kept under a digest of itself: the token's own text is never written down."""

    kind: TokenKind
    login: str
    scopes: list[str] = Field(default=[], description="A classic token's OAuth scopes")
    repositories: list[str] | None = Field(
        default=None, description="A fine-grained token's selected repositories (owner/name); None is all"
    )


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
    tree_entry_limit: int
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


StoredModel = TypeVar("StoredModel", StoredAccount, StoredToken, StoredRepository, StoredFile, StoredFault)


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
    """An account as it appears inside another resource: an owner, an author."""

    login: str
    id: int
    node_id: str
    avatar_url: str
    gravatar_id: str = ""
    url: str
    html_url: str
    repos_url: str
    type: AccountType
    site_admin: bool = False


class UserOut(AccountOut):
    """`GET /user`: the account a credential acts as."""

    name: str | None
    company: str | None = None
    blog: str = ""
    location: str | None = None
    email: str | None
    bio: str | None = None
    public_repos: int
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


class RepositoryOut(Wire):
    id: int
    node_id: str
    name: str
    full_name: str
    private: bool
    owner: AccountOut
    html_url: str
    description: str | None
    fork: bool = False
    url: str
    contents_url: str
    commits_url: str
    trees_url: str
    languages_url: str
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


class SearchRepositoryOut(Wire):
    id: int
    node_id: str
    name: str
    full_name: str
    owner: AccountOut
    private: bool
    html_url: str
    description: str | None
    fork: bool = False
    url: str


class CodeItemOut(Wire):
    name: str
    path: str
    sha: str
    url: str
    git_url: str
    html_url: str
    repository: SearchRepositoryOut
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


class GraphQLIn(Wire):
    """A GraphQL request's body: the query, its variables and the operation to run. Any other key is refused."""

    query: str
    variables: dict[str, JsonValue] | None = None
    operation_name: str | None = Field(default=None, alias="operationName")
