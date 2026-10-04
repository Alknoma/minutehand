"""YouTrack's own JSON: the only module that parses or builds it.

Four families live here:

- **Stored** — `StoredUser`, `StoredProject`, `StoredIssue`, `StoredComment`,
  `StoredAlias`: the body of each entity in the store. Named in YouTrack's own
  camelCase, and holding only what an answer is built from.
- **Requests** — one model per body YouTrack takes. A property the entity has not
  got is refused with `Unsupported property`, never applied or ignored.
- **Answers** — the entities as the REST API returns them, every one carrying its
  `$type`. `render` narrows an answer to what `fields=` named.
- **Errors** — `Refusal`, answered as `{"error": …, "error_description": …}`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Literal, TypeVar

from pydantic import ConfigDict, Field, JsonValue, ValidationError

from minutehand.domain.scenario import Model, TicketState

PAGE_DEFAULT = 42
"""What YouTrack answers a collection with when the request names no `$top`."""


class Wire(Model):
    """A YouTrack body: `$type` is spelled as YouTrack spells it, and read by either name."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- errors


class Refusal(Exception):
    """YouTrack answered with an error status. `error` and `description` are its own words."""

    def __init__(
        self, status: int, error: str, description: str, *, developer_message: str | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(description)
        self.status = status
        self.error = error
        self.description = description
        self.developer_message = developer_message
        self.field = field


class ErrorOut(Wire):
    error: str
    error_description: str
    error_developer_message: str | None = None
    error_field: str | None = None


def error_body(refusal: Refusal) -> bytes:
    answer = ErrorOut(
        error=refusal.error, error_description=refusal.description,
        error_developer_message=refusal.developer_message, error_field=refusal.field,
    )
    return answer.model_dump_json(exclude_none=True).encode()


def not_found(what: str) -> Refusal:
    return Refusal(404, "Not Found", f"Entity with id {what} not found")


def bad_request(description: str) -> Refusal:
    return Refusal(400, "bad_request", description)


def value_not_allowed() -> Refusal:
    """The refusal for a field value the field will not take: a state not in the bundle, an assignee off the team."""
    return Refusal(
        400, "", "Value is not allowed", developer_message="Value is not allowed", field="value",
    )


def invalid_query(value: str, field: str) -> Refusal:
    return Refusal(400, "invalid_query", f'The value "{value}" isn\'t used for the {field} field.')


# --------------------------------------------------------------------------- stored


class StoredState(Wire):
    id: str
    name: str
    isResolved: bool
    ordinal: int
    outcome: TicketState = Field(description="What an issue in this state is, read across every provider")


class StoredUser(Wire):
    id: str
    login: str
    fullName: str
    email: str | None
    ringId: str


class StoredProject(Wire):
    id: str
    shortName: str
    name: str
    description: str = ""
    leader: str = Field(description="User id")
    team: list[str] = Field(description="User ids: who the Assignee field accepts")
    stateField: str = Field(description="The State ProjectCustomField's id, which an issue's field shares")
    stateFieldDefinition: str = Field(description="The instance-wide CustomField id of State")
    stateBundle: str
    states: list[StoredState] = Field(min_length=1)
    assigneeField: str
    assigneeFieldDefinition: str
    assigneeBundle: str
    teamGroup: str


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
    state: str = Field(description="StoredState id")
    assignee: str | None = Field(default=None, description="User id")


class StoredComment(Wire):
    id: str
    issue: str
    text: str
    author: str
    created: int
    updated: int | None = None


class StoredAlias(Wire):
    """What a readable id (`DEMO-12`) names: the issue's database id. Never deleted, so numbers are never reused."""

    issue: str


StoredModel = TypeVar("StoredModel", StoredUser, StoredProject, StoredIssue, StoredComment, StoredAlias)


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


class CustomFieldIn(Wire):
    type_: str | None = Field(default=None, alias="$type")
    id: str | None = None
    name: str | None = None
    value: EntityIn | None = None


class IssueCreateIn(Wire):
    type_: str | None = Field(default=None, alias="$type")
    project: EntityIn | None = None
    summary: str | None = None
    description: str | None = None
    usesMarkdown: bool | None = None
    customFields: list[CustomFieldIn] = []


class IssueUpdateIn(Wire):
    type_: str | None = Field(default=None, alias="$type")
    summary: str | None = None
    description: str | None = None
    usesMarkdown: bool | None = None
    customFields: list[CustomFieldIn] = []


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


Body = TypeVar("Body", IssueCreateIn, IssueUpdateIn, CommentIn, CommandIn)


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


def _fields(text: str, at: int) -> tuple[Spec, int]:
    spec: Spec = {}
    name = ""
    while at < len(text):
        char = text[at]
        if char == "(":
            inner, at = _fields(text, at + 1)
            if at >= len(text) or text[at] != ")" or not name.strip():
                raise bad_request(f"Invalid fields: {text}")
            spec[name.strip()] = inner
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


def page_bounds(skip: str | None, top: str | None) -> tuple[int, int | None]:
    """`$skip` and `$top` as an offset and a limit; `$top=-1` is every entity."""
    try:
        start = int(skip) if skip else 0
        limit = int(top) if top else PAGE_DEFAULT
    except ValueError as error:
        raise bad_request("$skip and $top take integers") from error
    if start < 0:
        raise bad_request("$skip cannot be negative")
    return start, None if limit < 0 else limit


# --------------------------------------------------------------------------- answers


class UserOut(Wire):
    type_: Literal["User", "Me"] = Field(default="User", alias="$type")
    id: str
    login: str
    fullName: str
    name: str
    email: str | None
    ringId: str
    banned: bool = False
    guest: bool = False


class MeOut(UserOut):
    """The caller's own account, which `/api/users/me` types as `Me`."""

    type_: Literal["User", "Me"] = Field(default="Me", alias="$type")


class FieldTypeOut(Wire):
    type_: Literal["FieldType"] = Field(default="FieldType", alias="$type")
    id: str


class CustomFieldOut(Wire):
    type_: Literal["CustomField"] = Field(default="CustomField", alias="$type")
    id: str
    name: str
    fieldType: FieldTypeOut


class StateValueOut(Wire):
    type_: Literal["StateBundleElement"] = Field(default="StateBundleElement", alias="$type")
    id: str
    name: str
    isResolved: bool
    ordinal: int
    archived: bool = False


class StateBundleOut(Wire):
    type_: Literal["StateBundle"] = Field(default="StateBundle", alias="$type")
    id: str
    values: list[StateValueOut]


class UserBundleOut(Wire):
    type_: Literal["UserBundle"] = Field(default="UserBundle", alias="$type")
    id: str
    aggregatedUsers: list[UserOut]


class StateProjectFieldOut(Wire):
    type_: Literal["StateProjectCustomField"] = Field(default="StateProjectCustomField", alias="$type")
    id: str
    field: CustomFieldOut
    bundle: StateBundleOut
    canBeEmpty: bool = False
    emptyFieldText: str = "No state"
    isPublic: bool = True
    ordinal: int = 0


class UserProjectFieldOut(Wire):
    type_: Literal["UserProjectCustomField"] = Field(default="UserProjectCustomField", alias="$type")
    id: str
    field: CustomFieldOut
    bundle: UserBundleOut
    canBeEmpty: bool = True
    emptyFieldText: str = "Unassigned"
    isPublic: bool = True
    ordinal: int = 1


ProjectFieldOut = StateProjectFieldOut | UserProjectFieldOut


class UserGroupOut(Wire):
    type_: Literal["UserGroup"] = Field(default="UserGroup", alias="$type")
    id: str
    name: str
    usersCount: int
    users: list[UserOut]


class ProjectOut(Wire):
    type_: Literal["Project"] = Field(default="Project", alias="$type")
    id: str
    name: str
    shortName: str
    description: str
    archived: bool = False
    leader: UserOut
    team: UserGroupOut
    customFields: list[ProjectFieldOut]


class StateIssueFieldOut(Wire):
    type_: Literal["StateIssueCustomField"] = Field(default="StateIssueCustomField", alias="$type")
    id: str
    name: str
    value: StateValueOut | None
    projectCustomField: StateProjectFieldOut


class UserIssueFieldOut(Wire):
    type_: Literal["SingleUserIssueCustomField"] = Field(default="SingleUserIssueCustomField", alias="$type")
    id: str
    name: str
    value: UserOut | None
    projectCustomField: UserProjectFieldOut


IssueFieldOut = StateIssueFieldOut | UserIssueFieldOut


class CommentOut(Wire):
    type_: Literal["IssueComment"] = Field(default="IssueComment", alias="$type")
    id: str
    text: str
    textPreview: str
    author: UserOut
    created: int
    updated: int | None
    deleted: bool = False


class IssueOut(Wire):
    type_: Literal["Issue"] = Field(default="Issue", alias="$type")
    id: str
    idReadable: str
    numberInProject: int
    summary: str
    description: str | None
    project: ProjectOut
    reporter: UserOut
    updater: UserOut
    created: int
    updated: int
    resolved: int | None
    customFields: list[IssueFieldOut]
    comments: list[CommentOut]
    commentsCount: int


class IssueRefOut(Wire):
    type_: Literal["Issue"] = Field(default="Issue", alias="$type")
    id: str
    idReadable: str


class ParsedCommandOut(Wire):
    type_: Literal["ParsedCommand"] = Field(default="ParsedCommand", alias="$type")
    description: str
    error: bool = False
    delete: bool = False


class CommandListOut(Wire):
    type_: Literal["CommandList"] = Field(default="CommandList", alias="$type")
    query: str
    issues: list[IssueRefOut]
    commands: list[ParsedCommandOut]
    comment: str | None
    silent: bool


Answer = UserOut | MeOut | ProjectOut | ProjectFieldOut | IssueOut | IssueFieldOut | CommentOut | CommandListOut


def render(answer: Answer | Sequence[Answer], spec: Spec | None) -> bytes:
    """The answer as YouTrack would send it for this `fields=`."""
    if isinstance(answer, Wire):
        tree: JsonValue = answer.model_dump(mode="json", by_alias=True)
    else:
        tree = [item.model_dump(mode="json", by_alias=True) for item in answer]
    return json.dumps(select(tree, spec), ensure_ascii=False).encode()
