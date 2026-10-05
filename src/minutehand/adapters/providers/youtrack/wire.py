"""YouTrack's own JSON, and Hub's: the only module that parses or builds it.

Five families live here:

- **Stored** — the body of each entity in the store, named in YouTrack's own camelCase and holding only what an
  answer is built from: users, the instance's custom fields, projects with their fields and bundles, issues with
  their field values, comments, tags, link types, links, tokens, grants and faults.
- **Requests** — one model per body YouTrack or Hub takes. A property the entity has not got is refused with
  `Unsupported property`, never applied or ignored.
- **Answers** — the entities as the REST API returns them, every one carrying its `$type`. `render` narrows an
  answer to what `fields=` named.
- **Hub** — Hub's paged collections, its permission cache, and its OAuth token answer.
- **Errors** — `Refusal`, answered as `{"error": …, "error_description": …}`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, TypeVar

from pydantic import ConfigDict, Field, JsonValue, ValidationError

from minutehand.domain.scenario import Model, TicketState

PAGE_DEFAULT = 42
"""What YouTrack answers a collection with when the request names no `$top`."""
HUB_PAGE_DEFAULT = 100
"""What Hub answers a collection with when the request names no `$top`."""


class Wire(Model):
    """A YouTrack body: `$type` is spelled as YouTrack spells it, and read by either name."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- errors


class Refusal(Exception):
    """YouTrack answered with an error status. `error` and `description` are its own words."""

    def __init__(
        self,
        status: int,
        error: str,
        description: str,
        *,
        developer_message: str | None = None,
        field: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(description)
        self.status = status
        self.error = error
        self.description = description
        self.developer_message = developer_message
        self.field = field
        self.retry_after = retry_after


class ErrorOut(Wire):
    error: str
    error_description: str
    error_developer_message: str | None = None
    error_field: str | None = None


def error_body(refusal: Refusal) -> bytes:
    answer = ErrorOut(
        error=refusal.error,
        error_description=refusal.description,
        error_developer_message=refusal.developer_message,
        error_field=refusal.field,
    )
    return answer.model_dump_json(exclude_none=True).encode()


def not_found(what: str) -> Refusal:
    return Refusal(404, "Not Found", f"Entity with id {what} not found")


def bad_request(description: str) -> Refusal:
    return Refusal(400, "bad_request", description)


def invalid_entity_id(what: str) -> Refusal:
    """A slot that takes a database id (`0-3`) was handed something of another shape: refused before any lookup."""
    return bad_request(f"Invalid structure of entity id: {what}")


def value_not_allowed() -> Refusal:
    """The refusal for a field value the field will not take: a value not in the bundle, an assignee off the team,
    a clear of a field that cannot be empty."""
    return Refusal(400, "", "Value is not allowed", developer_message="Value is not allowed", field="value")


def invalid_query(value: str, field: str) -> Refusal:
    return Refusal(400, "invalid_query", f'The value "{value}" isn\'t used for the {field} field.')


def unparsed_query(text: str, why: str) -> Refusal:
    return Refusal(400, "invalid_query", f"Cannot parse search query {text!r}: {why}")


def unauthorized() -> Refusal:
    return Refusal(401, "Unauthorized", "Not authorized, try to login first")


def forbidden(permission: str) -> Refusal:
    return Refusal(403, "Forbidden", f"Insufficient permissions: {permission} is required")


# --------------------------------------------------------------------------- stored


class FieldType(StrEnum):
    """A custom field's type, by the id YouTrack's `fieldType(id)` answers. Only single-valued types are held."""

    STATE = "state[1]"
    ENUM = "enum[1]"
    USER = "user[1]"
    VERSION = "version[1]"
    DATE = "date"
    DATE_TIME = "date and time"
    PERIOD = "period"
    FLOAT = "float"
    INTEGER = "integer"
    STRING = "string"


BUNDLED = frozenset({FieldType.STATE, FieldType.ENUM, FieldType.VERSION})
"""The types whose values are elements of a bundle the project field owns."""
DATED = frozenset({FieldType.DATE, FieldType.DATE_TIME})

PROJECT_FIELD_TYPES: dict[FieldType, str] = {
    FieldType.STATE: "StateProjectCustomField",
    FieldType.ENUM: "EnumProjectCustomField",
    FieldType.USER: "UserProjectCustomField",
    FieldType.VERSION: "VersionProjectCustomField",
    FieldType.DATE: "SimpleProjectCustomField",
    FieldType.DATE_TIME: "SimpleProjectCustomField",
    FieldType.PERIOD: "PeriodProjectCustomField",
    FieldType.FLOAT: "SimpleProjectCustomField",
    FieldType.INTEGER: "SimpleProjectCustomField",
    FieldType.STRING: "SimpleProjectCustomField",
}
"""The `ProjectCustomField` subtype a field of each type is attached to a project as."""

ISSUE_FIELD_TYPES: dict[FieldType, str] = {
    FieldType.STATE: "StateIssueCustomField",
    FieldType.ENUM: "SingleEnumIssueCustomField",
    FieldType.USER: "SingleUserIssueCustomField",
    FieldType.VERSION: "SingleVersionIssueCustomField",
    FieldType.DATE: "DateIssueCustomField",
    FieldType.DATE_TIME: "DateIssueCustomField",
    FieldType.PERIOD: "PeriodIssueCustomField",
    FieldType.FLOAT: "SimpleIssueCustomField",
    FieldType.INTEGER: "SimpleIssueCustomField",
    FieldType.STRING: "SimpleIssueCustomField",
}
"""The `IssueCustomField` subtype an issue's field of each type answers as."""


class Permission(StrEnum):
    """A permission by Hub's key. The first two are held across the instance, the rest in named projects."""

    CREATE_PROJECT = "jetbrains.jetpass.project-create"
    READ_USER = "jetbrains.jetpass.user-read-basic"
    READ_PROJECT = "jetbrains.jetpass.project-read-basic"
    UPDATE_PROJECT = "jetbrains.jetpass.project-update"
    READ_ISSUE = "jetbrains.youtrack.readIssue"
    CREATE_ISSUE = "jetbrains.youtrack.createIssue"
    UPDATE_ISSUE = "jetbrains.youtrack.updateIssue"
    DELETE_ISSUE = "jetbrains.youtrack.deleteIssue"


PERMISSION_NAMES: dict[Permission, str] = {
    Permission.CREATE_PROJECT: "Create Project",
    Permission.READ_USER: "Read User Basic",
    Permission.READ_PROJECT: "Read Project Basic",
    Permission.UPDATE_PROJECT: "Update Project",
    Permission.READ_ISSUE: "Read Issue",
    Permission.CREATE_ISSUE: "Create Issue",
    Permission.UPDATE_ISSUE: "Update Issue",
    Permission.DELETE_ISSUE: "Delete Issue",
}
GLOBAL_PERMISSIONS = frozenset({Permission.CREATE_PROJECT, Permission.READ_USER})


class StoredUser(Wire):
    id: str
    login: str
    fullName: str
    email: str | None = Field(description="The account's email, shown or not; None when it has none")
    ringId: str = Field(description="The same person's id in Hub")
    banned: bool = False
    emailVisible: bool = Field(default=True, description="False: no email is shown for it, as for a hidden one")
    person: str | None = Field(
        default=None,
        description="The Person.key it was seeded for; None for the agent and a seed's own users. Never served",
    )

    @property
    def shown_email(self) -> str | None:
        """The email a client is shown, and finds the account by: none when hidden."""
        return self.email if self.emailVisible else None


class StoredFieldDefinition(Wire):
    """One custom field the instance defines; a project carries a subset of them."""

    id: str
    name: str
    fieldType: FieldType


class StoredBundleValue(Wire):
    id: str
    name: str
    ordinal: int
    isResolved: bool = Field(default=False, description="State values only")
    outcome: TicketState = Field(
        default=TicketState.OPEN, description="What an issue in this state is, read across every provider"
    )


class StoredProjectField(Wire):
    """One of the instance's fields as one project carries it: its own id, which an issue's field shares."""

    id: str
    field: str = Field(description="StoredFieldDefinition id")
    bundle: str | None = Field(default=None, description="The bundle's id, for a bundled or user field")
    values: list[StoredBundleValue] = []
    canBeEmpty: bool = True
    emptyFieldText: str = "No value"
    defaultValue: str | None = Field(default=None, description="A StoredBundleValue id")


class StoredProject(Wire):
    id: str
    shortName: str
    name: str
    description: str = ""
    leader: str = Field(description="User id")
    createdBy: str = Field(description="User id")
    team: list[str] = Field(description="User ids: who a user field of this project accepts")
    teamGroup: str
    ringId: str = Field(description="Hub's id for the project")
    teamRingId: str = Field(description="Hub's id for the project's team group")
    createdThroughApi: bool = Field(
        default=False,
        description="Made by POST /admin/projects: Hub holds no project for it, and nobody holds Update Project on it",
    )
    fields: list[StoredProjectField]


FieldValue = str | int | float
"""A stored field value: a bundle element's or a user's id, epoch milliseconds, minutes, a number or a string."""


class StoredIssue(Wire):
    id: str
    idReadable: str
    numberInProject: int
    project: str = Field(description="Project id")
    summary: str
    description: str | None = None
    reporter: str = Field(description="User id")
    updater: str = Field(description="User id")
    created: int = Field(description="Epoch milliseconds")
    updated: int
    resolved: int | None = None
    values: dict[str, FieldValue] = Field(default={}, description="By StoredProjectField id; absent is empty")
    tags: list[str] = Field(default=[], description="StoredTag ids, in the order they were added")
    seededFrom: int | None = Field(
        default=None, description="Its position in `Scenario.tickets`, for a seeded issue: how a happening finds it"
    )
    numberDeclared: bool = Field(
        default=False, description="Its seed declared its number (`SeededTicket.number`): a further seed keeps it"
    )


class StoredComment(Wire):
    id: str
    issue: str
    text: str
    author: str
    created: int
    updated: int | None = None
    seededFrom: int | None = Field(
        default=None, description="Its position among its seeded ticket's comments, for a seeded comment"
    )


class StoredAlias(Wire):
    """What a readable id (`DEMO-12`) names: the issue's database id. Never deleted, so numbers are never reused."""

    issue: str


class StoredTag(Wire):
    id: str
    name: str
    owner: str = Field(description="User id")
    seededFrom: int | None = Field(
        default=None, description="Its position among the seeded tickets' labels, first use first, for a seeded tag"
    )


class StoredLinkType(Wire):
    id: str
    name: str
    sourceToTarget: str
    targetToSource: str
    directed: bool
    aggregation: bool = False


class StoredLink(Wire):
    """One edge, read from both of its ends: `source` is the end `sourceToTarget` describes."""

    source: str
    linkType: str
    target: str
    created: int
    author: str
    removed: int | None = Field(default=None, description="When it was taken off; a removed link is kept as history")
    remover: str | None = None


class StoredToken(Wire):
    """A credential that acts as a user. Kept under its digest, never in the clear."""

    user: str
    client: str | None = Field(default=None, description="The Hub service that was issued it, for an OAuth token")
    expires: int | None = Field(default=None, description="Epoch milliseconds; None for a permanent token")


class StoredService(Wire):
    """A Hub service that may ask for a token with its secret, and acts as `user`."""

    clientId: str
    secretDigest: str
    user: str


class StoredGrant(Wire):
    """A permission given to or taken from one user, in one project or all of them; later grants win."""

    user: str
    permission: Permission
    project: str | None = Field(description="Project id; None for every project")
    held: bool


class StoredFault(Wire):
    """A refusal the scenario puts in the way of calls to one path for a while of simulated time."""

    method: str
    path: str = Field(description="As the API names it, without `/api` or `/youtrack/api`; `*` matches one segment")
    status: int
    starts: int
    ends: int | None


class StoredInstance(Wire):
    """What applies to the whole instance."""

    tokensRequired: bool = Field(description="Only seeded or issued tokens are accepted; otherwise any bearer is")
    countUnknown: bool = Field(default=False, description="issuesGetter/count answers -1, as while still counting")


StoredModel = TypeVar(
    "StoredModel",
    StoredUser,
    StoredFieldDefinition,
    StoredProject,
    StoredIssue,
    StoredComment,
    StoredAlias,
    StoredTag,
    StoredLinkType,
    StoredLink,
    StoredToken,
    StoredService,
    StoredGrant,
    StoredFault,
    StoredInstance,
)


def parse(model: type[StoredModel], body: str) -> StoredModel:
    return model.model_validate_json(body)


def dump(entity: Wire) -> str:
    return entity.model_dump_json(by_alias=True)


# --------------------------------------------------------------------------- requests


class EntityIn(Wire):
    """A reference to an existing entity, as a client writes one into a body."""

    type_: str | None = Field(default=None, alias="$type")
    id: str | None = None
    idReadable: str | None = None
    shortName: str | None = None
    name: str | None = None
    login: str | None = None


class ValueIn(Wire):
    """A custom field's value written as an object: a bundle element or user named by id, name or login, or a
    period by minutes or presentation."""

    type_: str | None = Field(default=None, alias="$type")
    id: str | None = None
    name: str | None = None
    login: str | None = None
    minutes: int | None = None
    presentation: str | None = None


FieldValueIn = bool | int | float | str | ValueIn
"""What a client writes as a custom field's `value`: epoch milliseconds, a number, text, or an object. A boolean is
read only so it can be refused."""


class CustomFieldIn(Wire):
    type_: str | None = Field(default=None, alias="$type")
    id: str | None = None
    name: str | None = None
    value: FieldValueIn | None = None


class FieldValueWriteIn(Wire):
    """`POST /issues/{id}/customFields/{fieldId}`: the field is the path's, so only its value and type are here."""

    type_: str | None = Field(default=None, alias="$type")
    id: str | None = None
    name: str | None = None
    value: FieldValueIn | None = None


class IssueCreateIn(Wire):
    type_: str | None = Field(default=None, alias="$type")
    project: EntityIn | None = None
    summary: str | None = None
    description: str | None = None
    usesMarkdown: bool | None = None
    customFields: list[CustomFieldIn] = []
    tags: list[EntityIn] = []


class IssueUpdateIn(Wire):
    type_: str | None = Field(default=None, alias="$type")
    summary: str | None = None
    description: str | None = None
    usesMarkdown: bool | None = None
    customFields: list[CustomFieldIn] = []
    tags: list[EntityIn] = []


class CommentIn(Wire):
    type_: str | None = Field(default=None, alias="$type")
    text: str | None = None
    usesMarkdown: bool | None = None


class CommandIn(Wire):
    type_: str | None = Field(default=None, alias="$type")
    query: str | None = None
    issues: list[EntityIn] = []
    comment: str | None = None
    silent: bool = False


class CountIn(Wire):
    type_: str | None = Field(default=None, alias="$type")
    query: str | None = None
    folder: EntityIn | None = None


class TagIn(Wire):
    type_: str | None = Field(default=None, alias="$type")
    id: str | None = None
    name: str | None = None


class ProjectCreateIn(Wire):
    type_: str | None = Field(default=None, alias="$type")
    name: str | None = None
    shortName: str | None = None
    description: str | None = None
    leader: EntityIn | None = None


class ProjectFieldIn(Wire):
    """`POST /admin/projects/{id}/customFields`: attach one of the instance's fields to a project."""

    type_: str | None = Field(default=None, alias="$type")
    field: EntityIn | None = None
    bundle: EntityIn | None = None
    canBeEmpty: bool | None = None
    emptyFieldText: str | None = None


class FieldDefinitionIn(Wire):
    """`POST /admin/customFieldSettings/customFields`: a new field for the instance."""

    type_: str | None = Field(default=None, alias="$type")
    name: str | None = None
    fieldType: EntityIn | None = None


class HubUserRefIn(Wire):
    """Hub's body naming a user by Hub id: `POST /hub/api/rest/usergroups/{id}/users`."""

    type_: str | None = Field(default=None, alias="type")
    id: str | None = None


Body = TypeVar(
    "Body",
    IssueCreateIn,
    IssueUpdateIn,
    FieldValueWriteIn,
    CommentIn,
    CommandIn,
    CountIn,
    TagIn,
    EntityIn,
    ProjectCreateIn,
    ProjectFieldIn,
    FieldDefinitionIn,
    HubUserRefIn,
)


def read_body(model: type[Body], raw: bytes) -> Body:
    """A request body, held to the properties the entity has."""
    if not raw.strip():
        raise bad_request("Request body required")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise bad_request("Malformed JSON in the request body") from error
    if not isinstance(decoded, dict):
        raise bad_request("The request body is not an entity")
    try:
        return model.model_validate(decoded)
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        if first["type"] == "extra_forbidden":
            raise bad_request(f"Unsupported property: {where}") from error
        raise bad_request(f"Invalid value of {where}") from error


# --------------------------------------------------------------------------- fields= and paging

type Spec = dict[str, Spec | None]
"""`fields=id,summary,project(id,shortName)` as a tree: a name with no sub-selection maps to None."""


def parse_fields(text: str | None) -> Spec | None:
    """None when the request named no fields at all."""
    if text is None or not text.strip():
        return None
    spec, rest = _fields(text, 0)
    if rest != len(text):
        raise bad_request(f"Invalid fields: {text}")
    return spec


def parse_hub_fields(text: str | None) -> Spec | None:
    """Hub's `fields=`: YouTrack's parentheses, or nested names joined with slashes (`permission/key`)."""
    if text is None or not text.strip():
        return None
    if "/" not in text:
        return parse_fields(text)
    tree: Spec = {}
    for path in text.split(","):
        node = tree
        names = [n.strip() for n in path.split("/")]
        if not all(names):
            raise bad_request(f"Invalid fields: {text}")
        for depth, name in enumerate(names):
            if depth == len(names) - 1:
                node.setdefault(name, None)
                continue
            inner = node.get(name)
            if inner is None:
                inner = {}
                node[name] = inner
            node = inner
    return tree


def _fields(text: str, at: int) -> tuple[Spec, int]:
    spec: Spec = {}
    name = ""
    while at < len(text):
        char = text[at]
        if char == "(":
            inner, at = _fields(text, at + 1)
            if at >= len(text) or text[at] != ")" or not name.strip():
                raise bad_request(f"Invalid fields: {text}")
            _merge(spec, name.strip(), inner)
            name = ""
            at += 1
            continue
        if char == ")":
            break
        if char == ",":
            if name.strip():
                spec.setdefault(name.strip(), None)
            name = ""
        else:
            name += char
        at += 1
    if name.strip():
        spec.setdefault(name.strip(), None)
    return spec, at


def _merge(spec: Spec, name: str, inner: Spec) -> None:
    """`a(b),a(c)` asks for both: a name selected twice keeps everything either selection named."""
    was = spec.get(name)
    if was is None:
        spec[name] = inner
        return
    for key, value in inner.items():
        if value is None:
            was.setdefault(key, None)
        else:
            _merge(was, key, value)


def select(value: JsonValue, spec: Spec | None) -> JsonValue:
    """Narrow an answer to what was asked: an entity asked for by name alone is its `id` and `$type`."""
    if isinstance(value, list):
        return [select(item, spec) for item in value]
    if not isinstance(value, dict):
        return value
    if spec is None:
        return {key: value[key] for key in ("id", "$type") if key in value}
    narrowed: dict[str, JsonValue] = {}
    for name, inner in spec.items():
        if name in value:
            narrowed[name] = select(value[name], inner)
    if "$type" in value:
        narrowed["$type"] = value["$type"]
    return narrowed


def page_bounds(skip: str | None, top: str | None, *, default: int = PAGE_DEFAULT) -> tuple[int, int | None]:
    """`$skip` and `$top` as an offset and a limit; `$top=-1` is every entity."""
    try:
        start = int(skip) if skip else 0
        limit = int(top) if top else default
    except ValueError as error:
        raise bad_request("$skip and $top take integers") from error
    if start < 0:
        raise bad_request("$skip cannot be negative")
    return start, None if limit < 0 else limit


# --------------------------------------------------------------------------- answers


class UserOut(Wire):
    type_: Literal["User", "Me"] = Field(default="User", serialization_alias="$type")
    id: str
    login: str
    fullName: str
    name: str
    email: str | None
    ringId: str
    avatarUrl: str
    banned: bool = False
    guest: bool = False
    online: bool = False


class MeOut(UserOut):
    """The caller's own account, which `/api/users/me` types as `Me`."""

    type_: Literal["User", "Me"] = Field(default="Me", serialization_alias="$type")


class FieldTypeOut(Wire):
    type_: Literal["FieldType"] = Field(default="FieldType", serialization_alias="$type")
    id: str
    presentation: str


class ProjectRefOut(Wire):
    type_: Literal["Project"] = Field(default="Project", serialization_alias="$type")
    id: str
    name: str
    shortName: str


class ProjectFieldRefOut(Wire):
    """A project field as the instance's field lists it among its instances."""

    type_: str = Field(serialization_alias="$type")
    id: str
    project: ProjectRefOut


class CustomFieldOut(Wire):
    type_: Literal["CustomField"] = Field(default="CustomField", serialization_alias="$type")
    id: str
    name: str
    localizedName: str | None = None
    fieldType: FieldTypeOut
    isAutoAttached: bool = False
    isDisplayedInIssueList: bool = True
    ordinal: int
    hasRunningJob: bool = False
    isUpdateable: bool = True
    instances: list[ProjectFieldRefOut] = []


class StateValueOut(Wire):
    type_: Literal["StateBundleElement"] = Field(default="StateBundleElement", serialization_alias="$type")
    id: str
    name: str
    localizedName: str | None = None
    isResolved: bool
    ordinal: int
    archived: bool = False
    description: str | None = None


class EnumValueOut(Wire):
    type_: Literal["EnumBundleElement"] = Field(default="EnumBundleElement", serialization_alias="$type")
    id: str
    name: str
    localizedName: str | None = None
    ordinal: int
    archived: bool = False
    description: str | None = None


class VersionValueOut(Wire):
    type_: Literal["VersionBundleElement"] = Field(default="VersionBundleElement", serialization_alias="$type")
    id: str
    name: str
    ordinal: int
    archived: bool = False
    released: bool = False
    releaseDate: int | None = None
    description: str | None = None


BundleValueOut = StateValueOut | EnumValueOut | VersionValueOut


class StateBundleOut(Wire):
    type_: Literal["StateBundle"] = Field(default="StateBundle", serialization_alias="$type")
    id: str
    name: str
    values: list[StateValueOut]
    isUpdateable: bool = True


class EnumBundleOut(Wire):
    type_: Literal["EnumBundle"] = Field(default="EnumBundle", serialization_alias="$type")
    id: str
    name: str
    values: list[EnumValueOut]
    isUpdateable: bool = True


class VersionBundleOut(Wire):
    type_: Literal["VersionBundle"] = Field(default="VersionBundle", serialization_alias="$type")
    id: str
    name: str
    values: list[VersionValueOut]
    isUpdateable: bool = True


class UserGroupRefOut(Wire):
    type_: Literal["UserGroup"] = Field(default="UserGroup", serialization_alias="$type")
    id: str
    name: str
    ringId: str | None = None


class UserBundleOut(Wire):
    type_: Literal["UserBundle"] = Field(default="UserBundle", serialization_alias="$type")
    id: str
    name: str
    aggregatedUsers: list[UserOut]
    groups: list[UserGroupRefOut]
    individuals: list[UserOut] = []
    isUpdateable: bool = True


BundleOut = StateBundleOut | EnumBundleOut | VersionBundleOut | UserBundleOut


class ProjectFieldOut(Wire):
    """A project's custom field. `bundle` is null on a simple or period field, as YouTrack answers it."""

    type_: str = Field(serialization_alias="$type")
    id: str
    field: CustomFieldOut
    project: ProjectRefOut
    bundle: BundleOut | None
    canBeEmpty: bool
    emptyFieldText: str
    isPublic: bool = True
    ordinal: int
    hasRunningJob: bool = False
    defaultValues: list[BundleValueOut] = []


class UserGroupOut(Wire):
    type_: Literal["UserGroup", "ProjectTeam"] = Field(default="UserGroup", serialization_alias="$type")
    id: str
    name: str
    ringId: str | None = None
    usersCount: int
    users: list[UserOut]


class ProjectOut(Wire):
    type_: Literal["Project"] = Field(default="Project", serialization_alias="$type")
    id: str
    name: str
    shortName: str
    description: str
    archived: bool = False
    template: bool = False
    leader: UserOut
    createdBy: UserOut
    team: UserGroupOut
    customFields: list[ProjectFieldOut]


class PeriodOut(Wire):
    type_: Literal["PeriodValue"] = Field(default="PeriodValue", serialization_alias="$type")
    id: str
    minutes: int
    presentation: str


IssueFieldValueOut = StateValueOut | EnumValueOut | VersionValueOut | UserOut | PeriodOut | int | float | str


class IssueFieldOut(Wire):
    """An issue's custom field: its `$type` names the field's type, as YouTrack's does."""

    type_: str = Field(serialization_alias="$type")
    id: str
    name: str
    value: IssueFieldValueOut | None
    projectCustomField: ProjectFieldOut


class TagOut(Wire):
    type_: Literal["IssueTag"] = Field(default="IssueTag", serialization_alias="$type")
    id: str
    name: str
    owner: UserOut
    untagOnResolve: bool = False


class LinkTypeOut(Wire):
    type_: Literal["IssueLinkType"] = Field(default="IssueLinkType", serialization_alias="$type")
    id: str
    name: str
    localizedName: str | None = None
    sourceToTarget: str
    localizedSourceToTarget: str | None = None
    targetToSource: str
    localizedTargetToSource: str | None = None
    directed: bool
    aggregation: bool
    readOnly: bool = False


class LinkedIssueOut(Wire):
    """An issue named inside another answer. It carries no links or comments of its own, so a read cannot
    recurse."""

    type_: Literal["Issue"] = Field(default="Issue", serialization_alias="$type")
    id: str
    idReadable: str
    numberInProject: int
    summary: str
    description: str | None
    created: int
    updated: int
    resolved: int | None
    project: ProjectRefOut


class LinkDirection(StrEnum):
    OUTWARD = "OUTWARD"
    INWARD = "INWARD"
    BOTH = "BOTH"


class LinkOut(Wire):
    type_: Literal["IssueLink"] = Field(default="IssueLink", serialization_alias="$type")
    id: str
    direction: LinkDirection
    linkType: LinkTypeOut
    issues: list[LinkedIssueOut]
    trimmedIssues: list[LinkedIssueOut]


class CommentOut(Wire):
    type_: Literal["IssueComment"] = Field(default="IssueComment", serialization_alias="$type")
    id: str
    text: str
    textPreview: str
    author: UserOut
    created: int
    updated: int | None
    deleted: bool = False
    pinned: bool = False
    issue: LinkedIssueOut


class IssueOut(Wire):
    type_: Literal["Issue"] = Field(default="Issue", serialization_alias="$type")
    id: str
    idReadable: str
    numberInProject: int
    summary: str
    description: str | None
    wikifiedDescription: str
    project: ProjectOut
    reporter: UserOut
    updater: UserOut
    created: int
    updated: int
    resolved: int | None
    customFields: list[IssueFieldOut]
    fields: list[IssueFieldOut]
    comments: list[CommentOut]
    commentsCount: int
    tags: list[TagOut]
    links: list[LinkOut]
    isDraft: bool = False
    votes: int = 0


class IssueRefOut(Wire):
    type_: Literal["Issue"] = Field(default="Issue", serialization_alias="$type")
    id: str
    idReadable: str


class ParsedCommandOut(Wire):
    type_: Literal["ParsedCommand"] = Field(default="ParsedCommand", serialization_alias="$type")
    description: str
    error: bool = False
    delete: bool = False


class CommandListOut(Wire):
    type_: Literal["CommandList"] = Field(default="CommandList", serialization_alias="$type")
    query: str
    issues: list[IssueRefOut]
    commands: list[ParsedCommandOut]
    comment: str | None
    silent: bool


class CountOut(Wire):
    type_: Literal["IssueCountResponse"] = Field(default="IssueCountResponse", serialization_alias="$type")
    id: str = "IssueCountResponse"
    count: int
    unresolvedOnly: bool = False


class ActivityCategoryOut(Wire):
    type_: Literal["ActivityCategory"] = Field(default="ActivityCategory", serialization_alias="$type")
    id: str


class FilterFieldOut(Wire):
    type_: Literal["CustomFilterField", "PredefinedFilterField"] = Field(serialization_alias="$type")
    id: str
    name: str
    presentation: str


ActivityValueOut = (
    StateValueOut | EnumValueOut | VersionValueOut | UserOut | TagOut | CommentOut | LinkedIssueOut | int | float | str
)


class ActivityOut(Wire):
    """One entry of an issue's history. `added` and `removed` are a collection for a bundled, user, tag, link or
    comment change, and the value itself for a simple one."""

    type_: str = Field(serialization_alias="$type")
    id: str
    timestamp: int
    author: UserOut
    category: ActivityCategoryOut
    target: LinkedIssueOut | CommentOut
    targetMember: str | None
    field: FilterFieldOut | None
    added: list[ActivityValueOut] | ActivityValueOut | None
    removed: list[ActivityValueOut] | ActivityValueOut | None


Answer = (
    UserOut
    | MeOut
    | CustomFieldOut
    | ProjectOut
    | ProjectFieldOut
    | StateBundleOut
    | EnumBundleOut
    | VersionBundleOut
    | UserBundleOut
    | StateValueOut
    | EnumValueOut
    | VersionValueOut
    | IssueOut
    | IssueFieldOut
    | CommentOut
    | CommandListOut
    | CountOut
    | TagOut
    | LinkTypeOut
    | LinkOut
    | UserGroupOut
    | ActivityOut
    | LinkedIssueOut
)


def render(answer: Answer | Sequence[Answer], spec: Spec | None) -> bytes:
    """The answer as YouTrack would send it for this `fields=`."""
    return json.dumps(select(_tree(answer), spec), ensure_ascii=False).encode()


def _tree(answer: Wire | Sequence[Wire]) -> JsonValue:
    if isinstance(answer, Wire):
        return answer.model_dump(mode="json", by_alias=True)
    return [item.model_dump(mode="json", by_alias=True) for item in answer]


# --------------------------------------------------------------------------- Hub


class HubUserOut(Wire):
    type_: Literal["user"] = Field(default="user", serialization_alias="type")
    id: str = Field(description="Hub's id: the user's ringId")
    login: str
    name: str
    banned: bool = False
    guest: bool = False


class HubGroupRefOut(Wire):
    type_: Literal["userGroup"] = Field(default="userGroup", serialization_alias="type")
    id: str
    name: str


class HubProjectRefOut(Wire):
    type_: Literal["project"] = Field(default="project", serialization_alias="type")
    id: str
    key: str


class HubProjectOut(Wire):
    type_: Literal["project"] = Field(default="project", serialization_alias="type")
    id: str
    key: str
    name: str
    archived: bool = False
    team: HubGroupRefOut


class HubGroupOut(Wire):
    type_: Literal["userGroup"] = Field(default="userGroup", serialization_alias="type")
    id: str
    name: str
    project: HubProjectRefOut | None
    users: list[HubUserOut]


class HubPermissionRefOut(Wire):
    key: str
    name: str


class HubCachedPermissionOut(Wire):
    permission: HubPermissionRefOut
    global_: bool = Field(serialization_alias="global")
    projects: list[HubProjectRefOut]


HubAnswer = HubUserOut | HubProjectOut | HubGroupOut | HubCachedPermissionOut


def render_hub_page(
    collection: str, items: Sequence[HubAnswer], *, skip: int, top: int, total: int, spec: Spec | None
) -> bytes:
    """One page of a Hub collection: `fields=` narrows the rows, and the page's own counters are always there."""
    page: dict[str, JsonValue] = {"skip": skip, "top": top, "total": total, collection: _select_hub(_tree(items), spec)}
    return json.dumps(page, ensure_ascii=False).encode()


def render_hub(answer: HubAnswer | Sequence[HubAnswer], spec: Spec | None) -> bytes:
    """A Hub entity or bare list, narrowed to what `fields=` named."""
    return json.dumps(_select_hub(_tree(answer), spec), ensure_ascii=False).encode()


def _select_hub(value: JsonValue, spec: Spec | None) -> JsonValue:
    """Hub answers a field asked for by name alone in full, and an entity asked for with no `fields=` in full."""
    if isinstance(value, list):
        return [_select_hub(item, spec) for item in value]
    if not isinstance(value, dict) or spec is None:
        return value
    return {name: _select_hub(value[name], inner) for name, inner in spec.items() if name in value}


class TokenRequestIn(Wire):
    """An OAuth token request's form."""

    grant_type: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None


class TokenOut(Wire):
    """Hub's answer to an OAuth token request."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    scope: str


def token_body(answer: TokenOut) -> bytes:
    return answer.model_dump_json().encode()


def oauth_refusal(status: int, error: str, description: str) -> Refusal:
    """Hub's OAuth errors are RFC 6749's: `invalid_client`, `unsupported_grant_type`, `invalid_request`."""
    return Refusal(status, error, description)
