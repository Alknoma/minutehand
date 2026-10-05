"""Jira Cloud's own JSON: the only module that parses or builds it.

Four families live here:

- **Stored** — `StoredSite`, `StoredUser`, `StoredCredential`, `StoredProject`, `StoredBoard`, `StoredSprint`,
  `StoredIssue`, `StoredComment`, `StoredLink`, `StoredAlias`, `StoredFaultUse`: the body of each entity in the
  store. A site's statuses, issue types, priorities, resolutions, fields and link types are site-wide objects
  in Jira, so they live on the site; a project names the ones it uses.
- **Requests** — one model per body the API takes. Free-form slots (`fields`, a custom field's value) are read
  here and nowhere else.
- **Answers** — the resources as the REST API returns them, built as JSON trees by the `*_out` functions.
- **Errors** — `Refusal`, answered as `{"errorMessages": [...], "errors": {field: message}}`; `GatewayRefusal`
  (`api.atlassian.com`'s own `{"code", "message"}`) and `OAuthRefusal` (`auth.atlassian.com`'s OAuth error).

Every timestamp is written the way Jira Cloud writes one: `2026-08-24T10:50:03.000+0000`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Literal, TypeVar

from pydantic import ConfigDict, Field, JsonValue, ValidationError

from minutehand.domain.errors import Rendered, ServiceRefusal
from minutehand.domain.scenario import Model, TicketState

Json = dict[str, JsonValue]


class Wire(Model):
    """A Jira body, named in Jira's own camelCase where Jira names the thing."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- errors


JSON = "application/json;charset=UTF-8"
"""The content type every Jira, gateway and OAuth answer, refusals included, carries."""


class Refusal(ServiceRefusal):
    """Jira answered with an error status: `messages` are its `errorMessages`, `fields` its `errors` map. Jira's
    body has no error code, so `code` is the status's reason phrase. A 401 carries Jira's `WWW-Authenticate`;
    `retry_after` is the `Retry-After` a rate limit answers with."""

    def __init__(
        self,
        status: int,
        messages: Sequence[str] = (),
        fields: dict[str, str] | None = None,
        *,
        retry_after: int | None = None,
        deliberate: bool = False,
    ) -> None:
        super().__init__(
            code=HTTPStatus(status).phrase,
            message="; ".join([*messages, *(f"{k}: {v}" for k, v in (fields or {}).items())]),
            status=status,
            deliberate=deliberate,
        )
        self.messages = list(messages)
        self.fields = dict(fields or {})
        self.retry_after = retry_after

    def render(self) -> Rendered:
        headers: list[tuple[str, str]] = []
        if self.status == 401:
            headers = [("www-authenticate", 'Basic realm="protected-area"')]
        elif self.retry_after is not None:
            headers = [("retry-after", str(self.retry_after))]
        body = json.dumps({"errorMessages": self.messages, "errors": self.fields}).encode()
        return Rendered(status=self.status, content_type=JSON, body=body, headers=headers)


class GatewayRefusal(ServiceRefusal):
    """`api.atlassian.com` answered with an error status of its own, as `{"code": status, "message": ...}`."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(code=HTTPStatus(status).phrase, message=message, status=status)

    def render(self) -> Rendered:
        body = json.dumps({"code": self.status, "message": self.message}).encode()
        return Rendered(status=self.status, content_type=JSON, body=body)


class OAuthRefusal(ServiceRefusal):
    """`auth.atlassian.com` refused a token request, in OAuth's `{"error": ..., "error_description": ...}`."""

    def __init__(self, status: int, error: str, description: str) -> None:
        super().__init__(code=error, message=description, status=status)

    def render(self) -> Rendered:
        body = json.dumps({"error": self.code, "error_description": self.message}).encode()
        return Rendered(status=self.status, content_type=JSON, body=body)


def error_answer(status: int, message: str) -> Rendered:
    """An error of Minutehand's own (501, 500) in Jira's shape, which its clients read as they read a refusal."""
    return Refusal(status, [message]).render()


def unauthenticated() -> Refusal:
    return Refusal(401, ["You are not signed in: this request carries no credentials the site accepts."])


def no_issue() -> Refusal:
    return Refusal(404, ["This issue does not exist, or you are not allowed to see it."])


def no_project(reference: str) -> Refusal:
    return Refusal(404, [f"There is no project '{reference}' you can see."])


def no_user() -> Refusal:
    return Refusal(404, ["That user does not exist, or you are not allowed to see them."])


def forbidden(action: str) -> Refusal:
    return Refusal(403, [f"You do not have permission to {action} in this project."])


def bad(message: str) -> Refusal:
    return Refusal(400, [message])


def bad_field(field: str, message: str) -> Refusal:
    return Refusal(400, [], {field: message})


def not_on_screen(field: str) -> str:
    return f"Field '{field}' cannot be set: it is not on this screen, or it does not exist."


def jql_error(message: str) -> Refusal:
    return Refusal(400, [message])


def rate_limited(retry_after: int) -> Refusal:
    """A rate limit a seed or `DeclaresFaults` declared: a fault armed on purpose."""
    return Refusal(429, ["Too many requests: wait before you try again."], retry_after=retry_after, deliberate=True)


# --------------------------------------------------------------------------- time


def jira_time(at: datetime) -> str:
    """A moment as Jira Cloud writes one: milliseconds and a `+0000` offset."""
    utc = at.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}+0000"


def jira_date(day: date) -> str:
    return day.isoformat()


# --------------------------------------------------------------------------- Atlassian Document Format

_BLOCKS = {
    "paragraph",
    "heading",
    "blockquote",
    "codeBlock",
    "listItem",
    "panel",
    "tableRow",
    "rule",
    "mediaSingle",
    "mediaGroup",
}


def adf_from_text(text: str) -> Json:
    """Plain text as a document: a paragraph per blank-line-separated block, a hard break per line inside one."""
    content: list[JsonValue] = []
    for block in text.split("\n\n"):
        nodes: list[JsonValue] = []
        for n, line in enumerate(block.split("\n")):
            if n:
                nodes.append({"type": "hardBreak"})
            if line:
                nodes.append({"type": "text", "text": line})
        content.append({"type": "paragraph", "content": nodes})
    return {"type": "doc", "version": 1, "content": content}


def is_document(value: JsonValue) -> bool:
    """A value Jira takes as an Atlassian document: a `doc` node at version 1 whose nodes all name a type."""
    if not isinstance(value, dict) or value.get("type") != "doc" or value.get("version") != 1:
        return False
    content = value.get("content")
    return isinstance(content, list) and all(_node_ok(node) for node in content)


def _node_ok(node: JsonValue) -> bool:
    if not isinstance(node, dict) or not isinstance(node.get("type"), str):
        return False
    if node["type"] == "text" and not isinstance(node.get("text"), str):
        return False
    content = node.get("content", [])
    return isinstance(content, list) and all(_node_ok(child) for child in content)


def adf_text(value: JsonValue) -> str:
    """The words of a document, as a reader sees them: a line per block, a newline per hard break."""
    out: list[str] = []
    _walk(value, out)
    return "".join(out).strip("\n")


def _walk(node: JsonValue, out: list[str]) -> None:
    if isinstance(node, list):
        for child in node:
            _walk(child, out)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    attrs = node.get("attrs")
    if kind == "text":
        text = node.get("text")
        out.append(text if isinstance(text, str) else "")
    elif kind == "hardBreak":
        out.append("\n")
    elif kind in ("mention", "emoji") and isinstance(attrs, dict):
        text = attrs.get("text")
        out.append(text if isinstance(text, str) else "")
    else:
        _walk(node.get("content", []), out)
        if kind in _BLOCKS and out and not out[-1].endswith("\n"):
            out.append("\n")


# --------------------------------------------------------------------------- stored: the site


class Category(StrEnum):
    """A status category, by the key Jira gives it."""

    NEW = "new"
    INDETERMINATE = "indeterminate"
    DONE = "done"


CATEGORY_ID: dict[Category, int] = {Category.NEW: 2, Category.INDETERMINATE: 4, Category.DONE: 3}
CATEGORY_NAME: dict[Category, str] = {
    Category.NEW: "To Do",
    Category.INDETERMINATE: "In Progress",
    Category.DONE: "Done",
}
CATEGORY_COLOUR: dict[Category, str] = {
    Category.NEW: "blue-gray",
    Category.INDETERMINATE: "yellow",
    Category.DONE: "green",
}


class CustomFieldType(StrEnum):
    """What a custom field holds."""

    STRING = "textfield"
    NUMBER = "float"
    OPTION = "select"
    OPTIONS = "multiselect"
    DATE = "datepicker"
    USER = "userpicker"
    SPRINT = "gh-sprint"
    EPIC_LINK = "gh-epic-link"


FIELD_TYPE_KEYS: dict[CustomFieldType, str] = {
    CustomFieldType.STRING: "com.atlassian.jira.plugin.system.customfieldtypes:textfield",
    CustomFieldType.NUMBER: "com.atlassian.jira.plugin.system.customfieldtypes:float",
    CustomFieldType.OPTION: "com.atlassian.jira.plugin.system.customfieldtypes:select",
    CustomFieldType.OPTIONS: "com.atlassian.jira.plugin.system.customfieldtypes:multiselect",
    CustomFieldType.DATE: "com.atlassian.jira.plugin.system.customfieldtypes:datepicker",
    CustomFieldType.USER: "com.atlassian.jira.plugin.system.customfieldtypes:userpicker",
    CustomFieldType.SPRINT: "com.pyxis.greenhopper.jira:gh-sprint",
    CustomFieldType.EPIC_LINK: "com.pyxis.greenhopper.jira:gh-epic-link",
}


class StoredStatus(Wire):
    id: str
    name: str
    category: Category
    outcome: TicketState = Field(description="What an issue in this status is, read across every provider")


class StoredIssueType(Wire):
    id: str
    name: str
    subtask: bool = False
    hierarchyLevel: int = 0
    description: str = ""


class StoredPriority(Wire):
    id: str
    name: str


class StoredResolution(Wire):
    id: str
    name: str
    description: str = ""


class StoredOption(Wire):
    id: str
    value: str


class StoredField(Wire):
    id: str = Field(description="customfield_<n>")
    name: str
    kind: CustomFieldType
    options: list[StoredOption] = []


class StoredLinkType(Wire):
    id: str
    name: str
    inward: str
    outward: str


class StoredRole(Wire):
    id: str
    name: str
    edits: bool = Field(description="Its members create, edit, transition, comment on and delete issues")
    administers: bool = False


class StoredRateLimit(Wire):
    method: str | None = None
    path: str = Field(description="Requests whose API path starts with this are limited")
    times: int = Field(ge=1)
    retry_after: int = Field(ge=0, description="Seconds, as the Retry-After header says them")


class StoredSite(Wire):
    name: str = Field(description="The site's name: <name>.atlassian.net")
    cloudId: str
    agent: str = Field(description="The agent's own accountId")
    statuses: list[StoredStatus]
    issueTypes: list[StoredIssueType]
    priorities: list[StoredPriority] = Field(description="Highest first: the scheme's order")
    defaultPriority: str
    resolutions: list[StoredResolution]
    fields: list[StoredField]
    linkTypes: list[StoredLinkType]
    roles: list[StoredRole]
    sprintField: str
    rateLimits: list[StoredRateLimit] = []

    @property
    def host(self) -> str:
        return f"{self.name}.atlassian.net"

    def status(self, status_id: str) -> StoredStatus:
        return next(s for s in self.statuses if s.id == status_id)

    def issue_type(self, type_id: str) -> StoredIssueType:
        return next(t for t in self.issueTypes if t.id == type_id)

    def priority(self, priority_id: str) -> StoredPriority:
        return next(p for p in self.priorities if p.id == priority_id)

    def resolution(self, resolution_id: str) -> StoredResolution:
        return next(r for r in self.resolutions if r.id == resolution_id)

    def field(self, field_id: str) -> StoredField | None:
        return next((f for f in self.fields if f.id == field_id), None)


class AccountType(StrEnum):
    ATLASSIAN = "atlassian"
    APP = "app"
    CUSTOMER = "customer"


class StoredUser(Wire):
    accountId: str
    displayName: str
    emailAddress: str | None
    emailVisible: bool = True
    accountType: AccountType = AccountType.ATLASSIAN
    active: bool = True
    timeZone: str = "UTC"
    siteAdmin: bool = False
    person: str | None = Field(
        default=None, description="Minutehand's own: the `Person.key` this account was seeded for; never served"
    )


class CredentialKind(StrEnum):
    API_TOKEN = "api_token"
    OAUTH = "oauth"


class StoredCredential(Wire):
    """An API token (Basic, with its account's email) or an OAuth 2.0 (3LO) grant."""

    id: str
    kind: CredentialKind
    account: str
    secret: str = Field(description="The API token, or the current access token")
    refreshToken: str | None = None
    clientId: str | None = None
    clientSecret: str | None = None
    issued: datetime | None = Field(default=None, description="When the access token was minted")
    lifetime: int = Field(default=3600, description="Seconds an access token lives")


# --------------------------------------------------------------------------- stored: projects, boards


class StoredTransition(Wire):
    id: str
    name: str
    to: str = Field(description="Status id")
    sources: list[str] = Field(default=[], description="Status ids it leaves from; empty: from any status")
    screen: list[str] = Field(default=[], description="Field ids on its screen")
    required: list[str] = Field(default=[], description="Field ids its screen requires")


class StoredScreen(Wire):
    """What one issue type's create and edit screens hold in a project."""

    issueType: str
    fields: list[str]
    required: list[str] = []


class StoredMembers(Wire):
    role: str
    accounts: list[str]


class StoredProject(Wire):
    id: str
    key: str
    name: str
    description: str = ""
    lead: str
    projectTypeKey: str = "software"
    simplified: bool = False
    screens: list[StoredScreen]
    statuses: list[str] = Field(description="Status ids, in board order; the first is where an issue starts")
    transitions: list[StoredTransition]
    members: list[StoredMembers]

    def screen(self, type_id: str) -> StoredScreen | None:
        return next((s for s in self.screens if s.issueType == type_id), None)

    def accounts(self, role: str) -> list[str]:
        return next((m.accounts for m in self.members if m.role == role), [])


class SprintState(StrEnum):
    FUTURE = "future"
    ACTIVE = "active"
    CLOSED = "closed"


class StoredBoard(Wire):
    id: int
    name: str
    project: str
    type: Literal["scrum", "kanban"] = "scrum"


class StoredSprint(Wire):
    id: int
    board: int
    name: str
    state: SprintState
    goal: str = ""
    startDate: datetime | None = None
    endDate: datetime | None = None


# --------------------------------------------------------------------------- stored: issues


class StoredValue(Wire):
    """A custom field's value as Jira takes it: a string, a number, an option id or ids, an accountId, a date,
    or sprint ids."""

    field: str
    value: JsonValue


class StoredItem(Wire):
    """One field's change in a history entry, as the changelog reports it."""

    field: str
    fieldtype: Literal["jira", "custom"] = "jira"
    fieldId: str
    from_: str | None = Field(default=None, alias="from")
    fromString: str | None = None
    to: str | None = None
    toString: str | None = None


class StoredHistory(Wire):
    id: str
    author: str | None
    created: datetime
    items: list[StoredItem]


class StoredIssue(Wire):
    id: str
    key: str
    project: str
    issuetype: str
    summary: str
    description: JsonValue = None
    status: str
    resolution: str | None = None
    resolutiondate: datetime | None = None
    priority: str
    assignee: str | None = None
    reporter: str
    creator: str
    created: datetime
    updated: datetime
    duedate: date | None = None
    labels: list[str] = []
    parent: str | None = Field(default=None, description="The parent issue's id")
    custom: list[StoredValue] = []
    originalEstimateSeconds: int | None = None
    timeSpentSeconds: int | None = None
    history: list[StoredHistory] = []
    seededFrom: int | None = Field(
        default=None,
        description="Minutehand's own: the position in `Scenario.tickets` it was seeded from; never served",
    )
    numberDeclared: bool = Field(
        default=False,
        description="Minutehand's own: its key's number is the one its seed declared (`SeededTicket.number`), kept "
        "when a further seed places it; never served",
    )

    def value(self, field: str) -> JsonValue:
        return next((v.value for v in self.custom if v.field == field), None)


class StoredComment(Wire):
    id: str
    issue: str
    author: str
    body: JsonValue
    created: datetime
    updated: datetime
    updateAuthor: str
    seededFrom: int | None = Field(
        default=None,
        description="Minutehand's own: its place among its issue's seeded comments, which read first; never served",
    )


class StoredLink(Wire):
    """One link: `source` does the type's outward act to `destination` ("source blocks destination").

    A create, and a read of the link itself, name the source `outwardIssue` (the "from" issue, as Atlassian's
    reference calls it) and the destination `inwardIssue`. An issue's own `issuelinks` name the OTHER end: the
    source's entry carries `outwardIssue` (the issue it blocks), the destination's `inwardIssue` (the issue that
    blocks it)."""

    id: str
    type: str
    source: str = Field(description="Issue id")
    destination: str = Field(description="Issue id")


class StoredAlias(Wire):
    """What an issue key names. Never deleted, so a project never hands a number out twice."""

    issue: str


class StoredFaultUse(Wire):
    used: int


class StoredDeclaredLimits(Wire):
    """Rate limits declared on an open world (`provider-faults`), kept apart from the site's seeded ones, so a later
    seed fragment that adds to the site's never rewrites what was declared."""

    limits: list[StoredRateLimit] = []


StoredModel = TypeVar(
    "StoredModel",
    StoredSite,
    StoredUser,
    StoredCredential,
    StoredProject,
    StoredBoard,
    StoredSprint,
    StoredIssue,
    StoredComment,
    StoredLink,
    StoredAlias,
    StoredFaultUse,
    StoredDeclaredLimits,
)


def parse(model: type[StoredModel], body: str) -> StoredModel:
    return model.model_validate_json(body)


def dump(entity: Wire) -> str:
    return entity.model_dump_json(by_alias=True)


# --------------------------------------------------------------------------- requests


class Ref(Wire):
    """A reference to an existing thing, as a client writes one into a body."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    id: str | int | None = None
    key: str | None = None
    name: str | None = None
    accountId: str | None = None
    value: str | None = None


class IssueIn(Wire):
    """`POST /issue` and `PUT /issue/{key}`: free-form `fields`, read field by field against the screen."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    fields: Json = {}
    update: Json = {}


class TransitionIn(Wire):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    transition: Ref | None = None
    fields: Json = {}
    update: Json = {}


class CommentIn(Wire):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    body: JsonValue = None


class AssigneeIn(Wire):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    accountId: str | None = None


class LinkIn(Wire):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    type: Ref | None = None
    inwardIssue: Ref | None = None
    outwardIssue: Ref | None = None
    comment: CommentIn | None = None


class SearchIn(Wire):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    jql: str = ""
    maxResults: int | None = None
    fields: list[str] | None = None
    expand: str | None = None
    nextPageToken: str | None = None


class CountIn(Wire):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    jql: str = ""


def _templates(project_type: str, prefix: str, names: str) -> dict[str, str]:
    return {f"{prefix}:{name}": project_type for name in names.split()}


TEMPLATE_TYPES: dict[str, str] = {
    **_templates(
        "software",
        "com.pyxis.greenhopper.jira",
        "gh-simplified-agility-kanban gh-simplified-agility-scrum gh-simplified-basic gh-simplified-kanban-classic "
        "gh-simplified-scrum-classic",
    ),
    **_templates(
        "business",
        "com.atlassian.jira-core-project-templates",
        "jira-core-simplified-content-management jira-core-simplified-document-approval "
        "jira-core-simplified-lead-tracking jira-core-simplified-process-control jira-core-simplified-procurement "
        "jira-core-simplified-project-management jira-core-simplified-recruitment jira-core-simplified-task-tracking",
    ),
    **_templates(
        "service_desk",
        "com.atlassian.servicedesk",
        "simplified-it-service-management simplified-external-service-desk simplified-hr-service-desk "
        "simplified-facilities-service-desk simplified-legal-service-desk simplified-analytics-service-desk "
        "simplified-marketing-service-desk simplified-design-service-desk simplified-sales-service-desk "
        "simplified-finance-service-desk company-managed-blank-service-project "
        "company-managed-general-service-project team-managed-general-service-project next-gen-it-service-desk "
        "next-gen-hr-service-desk next-gen-legal-service-desk next-gen-marketing-service-desk "
        "next-gen-facilities-service-desk next-gen-analytics-service-desk next-gen-finance-service-desk "
        "next-gen-design-service-desk next-gen-sales-service-desk",
    ),
    **_templates("customer_service", "com.atlassian.jcs", "customer-service-management"),
}
"""Each project template Jira Cloud's create-project reference lists, and the one project type it builds
(https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post)."""

PROJECT_TYPES = frozenset(TEMPLATE_TYPES.values())

PROJECT_KEY_MOST = 10
"""The longest project key Jira takes: an uppercase letter, then one or more uppercase letters or digits
(https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-post)."""
_PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9]+")


def project_key_problem(key: str) -> str | None:
    """What is wrong with `key` as a new project's key, in the words of a create's `errors.projectKey`."""
    if not key:
        return "A project needs a key."
    if len(key) > PROJECT_KEY_MOST:
        return f"A project key is at most {PROJECT_KEY_MOST} characters long."
    if not _PROJECT_KEY.fullmatch(key):
        return "A project key is a capital letter followed by capitals and digits."
    return None


class ProjectIn(Wire):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    key: str | None = None
    name: str | None = None
    leadAccountId: str | None = None
    projectTypeKey: str | None = None
    projectTemplateKey: str | None = None
    description: str | None = None
    assigneeType: str | None = None


class RoleActorsIn(Wire):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    user: list[str] = []
    groupId: list[str] = []


class SprintIssuesIn(Wire):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    issues: list[str] = []


class TokenIn(Wire):
    """Atlassian's token endpoint, read from a JSON body or a form."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    grant_type: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None
    code: str | None = None
    redirect_uri: str | None = None


Body = TypeVar(
    "Body",
    IssueIn,
    TransitionIn,
    CommentIn,
    AssigneeIn,
    LinkIn,
    SearchIn,
    CountIn,
    ProjectIn,
    RoleActorsIn,
    SprintIssuesIn,
    TokenIn,
)


def read_body(model: type[Body], raw: bytes) -> Body:
    """A request body as the model; Jira's own refusal for a body that is not JSON or not that shape."""
    if not raw.strip():
        raise bad("The request has no body; this resource needs one.")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise bad("The request body is not valid JSON.") from error
    if not isinstance(decoded, dict):
        raise bad("The request body must be a JSON object.")
    try:
        return model.model_validate(decoded)
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise bad(f"The request body cannot be read at '{where}'.") from error


def read_ref(value: JsonValue) -> Ref | None:
    """A field value that should be a reference (`{"id": …}`, `{"name": …}`); None when it is not one."""
    if not isinstance(value, dict):
        return None
    try:
        return Ref.model_validate(value)
    except ValidationError:
        return None


# --------------------------------------------------------------------------- answers


def render(tree: object) -> bytes:
    return json.dumps(tree, ensure_ascii=False).encode()


def avatar_urls(seed: str) -> Json:
    base = f"https://avatar-management.minutehand.invalid/{seed}"
    return {size: f"{base}/{size}" for size in ("48x48", "24x24", "16x16", "32x32")}


def user_out(base: str, user: StoredUser) -> Json:
    out: Json = {
        "self": f"{base}/rest/api/3/user?accountId={user.accountId}",
        "accountId": user.accountId,
        "accountType": user.accountType.value,
    }
    if user.emailVisible and user.emailAddress is not None:
        out["emailAddress"] = user.emailAddress
    out |= {
        "avatarUrls": avatar_urls(user.accountId),
        "displayName": user.displayName,
        "active": user.active,
        "timeZone": user.timeZone,
        "locale": "en_US",
    }
    return out


def category_out(base: str, category: Category) -> Json:
    return {
        "self": f"{base}/rest/api/3/statuscategory/{CATEGORY_ID[category]}",
        "id": CATEGORY_ID[category],
        "key": category.value,
        "colorName": CATEGORY_COLOUR[category],
        "name": CATEGORY_NAME[category],
    }


def status_out(base: str, status: StoredStatus) -> Json:
    return {
        "self": f"{base}/rest/api/3/status/{status.id}",
        "description": "",
        "iconUrl": f"{base}/images/icons/statuses/generic.png",
        "name": status.name,
        "untranslatedName": status.name,
        "id": status.id,
        "statusCategory": category_out(base, status.category),
    }


def issue_type_out(base: str, issue_type: StoredIssueType) -> Json:
    return {
        "self": f"{base}/rest/api/3/issuetype/{issue_type.id}",
        "id": issue_type.id,
        "description": issue_type.description,
        "iconUrl": f"{base}/rest/api/2/universal_avatar/view/type/issuetype/avatar/{issue_type.id}",
        "name": issue_type.name,
        "untranslatedName": issue_type.name,
        "subtask": issue_type.subtask,
        "hierarchyLevel": issue_type.hierarchyLevel,
    }


def priority_out(base: str, priority: StoredPriority) -> Json:
    return {
        "self": f"{base}/rest/api/3/priority/{priority.id}",
        "iconUrl": f"{base}/images/icons/priorities/{priority.name.lower()}.svg",
        "name": priority.name,
        "id": priority.id,
    }


def resolution_out(base: str, resolution: StoredResolution) -> Json:
    return {
        "self": f"{base}/rest/api/3/resolution/{resolution.id}",
        "id": resolution.id,
        "description": resolution.description,
        "name": resolution.name,
    }


def link_type_out(base: str, link_type: StoredLinkType) -> Json:
    return {
        "id": link_type.id,
        "name": link_type.name,
        "inward": link_type.inward,
        "outward": link_type.outward,
        "self": f"{base}/rest/api/3/issueLinkType/{link_type.id}",
    }


def project_ref_out(base: str, project: StoredProject) -> Json:
    return {
        "self": f"{base}/rest/api/3/project/{project.id}",
        "id": project.id,
        "key": project.key,
        "name": project.name,
        "projectTypeKey": project.projectTypeKey,
        "simplified": project.simplified,
        "avatarUrls": avatar_urls(project.id),
    }


def sprint_out(base: str, sprint: StoredSprint) -> Json:
    out: Json = {
        "id": sprint.id,
        "self": f"{base}/rest/agile/1.0/sprint/{sprint.id}",
        "state": sprint.state.value,
        "name": sprint.name,
        "originBoardId": sprint.board,
        "goal": sprint.goal,
    }
    if sprint.startDate is not None:
        out["startDate"] = sprint.startDate.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if sprint.endDate is not None:
        out["endDate"] = sprint.endDate.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return out


def sprint_value_out(sprint: StoredSprint) -> Json:
    """A sprint as an issue's Sprint field holds it."""
    out: Json = {
        "id": sprint.id,
        "name": sprint.name,
        "state": sprint.state.value,
        "boardId": sprint.board,
        "goal": sprint.goal,
    }
    if sprint.startDate is not None:
        out["startDate"] = jira_time(sprint.startDate)
    if sprint.endDate is not None:
        out["endDate"] = jira_time(sprint.endDate)
    return out


def field_schema(field: StoredField) -> Json:
    custom = FIELD_TYPE_KEYS[field.kind]
    number = int(field.id.removeprefix("customfield_"))
    shapes: dict[CustomFieldType, Json] = {
        CustomFieldType.STRING: {"type": "string"},
        CustomFieldType.NUMBER: {"type": "number"},
        CustomFieldType.OPTION: {"type": "option"},
        CustomFieldType.OPTIONS: {"type": "array", "items": "option"},
        CustomFieldType.DATE: {"type": "date"},
        CustomFieldType.USER: {"type": "user"},
        CustomFieldType.SPRINT: {"type": "array", "items": "json"},
        CustomFieldType.EPIC_LINK: {"type": "any"},
    }
    return shapes[field.kind] | {"custom": custom, "customId": number}


def custom_field_out(field: StoredField) -> Json:
    number = field.id.removeprefix("customfield_")
    return {
        "id": field.id,
        "key": field.id,
        "name": field.name,
        "untranslatedName": field.name,
        "custom": True,
        "orderable": True,
        "navigable": True,
        "searchable": True,
        "clauseNames": [f"cf[{number}]", field.name],
        "schema": field_schema(field),
    }


SYSTEM_FIELDS: list[tuple[str, str, Json]] = [
    ("summary", "Summary", {"type": "string", "system": "summary"}),
    ("description", "Description", {"type": "string", "system": "description"}),
    ("project", "Project", {"type": "project", "system": "project"}),
    ("issuetype", "Issue Type", {"type": "issuetype", "system": "issuetype"}),
    ("status", "Status", {"type": "status", "system": "status"}),
    ("statusCategory", "Status Category", {"type": "statusCategory", "system": "statusCategory"}),
    ("priority", "Priority", {"type": "priority", "system": "priority"}),
    ("resolution", "Resolution", {"type": "resolution", "system": "resolution"}),
    ("resolutiondate", "Resolved", {"type": "datetime", "system": "resolutiondate"}),
    ("assignee", "Assignee", {"type": "user", "system": "assignee"}),
    ("reporter", "Reporter", {"type": "user", "system": "reporter"}),
    ("creator", "Creator", {"type": "user", "system": "creator"}),
    ("created", "Created", {"type": "datetime", "system": "created"}),
    ("updated", "Updated", {"type": "datetime", "system": "updated"}),
    ("duedate", "Due date", {"type": "date", "system": "duedate"}),
    ("labels", "Labels", {"type": "array", "items": "string", "system": "labels"}),
    ("parent", "Parent", {"type": "issuelink", "system": "parent"}),
    ("subtasks", "Sub-tasks", {"type": "array", "items": "issuelinks", "system": "subtasks"}),
    ("issuelinks", "Linked Issues", {"type": "array", "items": "issuelinks", "system": "issuelinks"}),
    ("components", "Components", {"type": "array", "items": "component", "system": "components"}),
    ("fixVersions", "Fix versions", {"type": "array", "items": "version", "system": "fixVersions"}),
    ("timetracking", "Time tracking", {"type": "timetracking", "system": "timetracking"}),
    ("comment", "Comment", {"type": "comments-page", "system": "comment"}),
    ("watches", "Watchers", {"type": "watches", "system": "watches"}),
]


def system_field_out(field_id: str, name: str, schema: Json) -> Json:
    return {
        "id": field_id,
        "key": field_id,
        "name": name,
        "custom": False,
        "orderable": field_id not in ("status", "created", "updated", "creator", "resolutiondate", "subtasks"),
        "navigable": True,
        "searchable": True,
        "clauseNames": [field_id],
        "schema": schema,
    }


def system_schema(field_id: str) -> Json:
    return next(schema for key, _, schema in SYSTEM_FIELDS if key == field_id)


def system_name(field_id: str) -> str:
    return next(name for key, name, _ in SYSTEM_FIELDS if key == field_id)


def duration(seconds: int) -> str:
    """Seconds as Jira writes a duration in a working week: `1w 2d 3h 4m`."""
    parts: list[str] = []
    for unit, size in (("w", 5 * 8 * 3600), ("d", 8 * 3600), ("h", 3600), ("m", 60)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return " ".join(parts) or "0m"
