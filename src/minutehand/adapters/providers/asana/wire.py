"""Asana's own JSON: the only module that parses or builds it.

Three families of model live here:

- **Stored** — `AsanaWorkspace`, `AsanaUser`, `AsanaTeam`, `AsanaProject`,
  `AsanaSection`, `AsanaCustomField`, `AsanaTag`, `AsanaCredential`, `AsanaTask`,
  `AsanaStory`: the body of each entity in the store. Each carries Asana's
  `resource_type`, so a record read back is parsed by what it says it is.
- **Writes** — the `{"data": {...}}` envelope of a POST or PUT, read into
  `TaskCreate`, `TaskUpdate`, `ProjectCreate`, ... and refused field by field with
  Asana's own messages. A custom field value is read against its definition in
  `custom_field_value`.
- **Responses** — the full representation of each resource (`TaskOut`, ...),
  narrowed by `opt_fields` or cut to Asana's compact record, inside the
  `{"data": ...}` / `{"errors": [...]}` envelope with offset pagination.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from email.message import Message
from enum import StrEnum
from typing import Annotated, Literal, TypeVar
from urllib.parse import parse_qsl, unquote

from pydantic import Field, JsonValue, SerializerFunctionWrapHandler, TypeAdapter, model_serializer

from minutehand.domain.errors import Asked, NotServed, Rendered, ServiceRefusal
from minutehand.domain.scenario import Model, TicketState

API_BASE = "https://app.asana.com/api/1.0"
ERROR_HELP = (
    "For more information on API status codes and how to handle them, read "
    "the docs on errors: https://developers.asana.com/docs/errors"
)
PAGE_MAX = 100
UNPAGINATED_MAX = 1000
SEARCH_DEFAULT = 20
TYPEAHEAD_DEFAULT = 20
TOKEN_LIFETIME_SECONDS = 3600
RATE_LIMITED = "You've made too many requests and hit a rate limit. Please retry after the given amount of time."
"""https://developers.asana.com/docs/rate-limits gives this body for every limiter."""

_GID = re.compile(r"^[0-9]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


JSON = "application/json; charset=UTF-8"
"""The content type of every answer, refusals included, spelled as Asana spells it
(`tests/data/asana_rest_1_0/real-service-without-a-token-2026-10-08.txt`)."""


class Refusal(ServiceRefusal):
    """Asana answered with an error. `status` and `message` are Asana's own."""

    def __init__(self, status: int, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after

    def render(self, asked: Asked) -> Rendered:
        """Asana's envelope, `{"errors": [{"message", "help"}]}`, with `Retry-After` when it says when to retry."""
        headers = [("retry-after", str(self.retry_after))] if self.retry_after is not None else []
        return Rendered(status=self.status, content_type=JSON, body=failed(self.message), headers=headers)


def error_answer(status: int, message: str) -> Rendered:
    """What Minutehand answers in Asana's place (501, 500), in Asana's envelope: the `asana` client raises
    `ApiException` with the body. The envelope has no place for a code."""
    return Rendered(status=status, content_type=JSON, body=failed(message))


def bad(message: str) -> Refusal:
    return Refusal(400, message)


def unknown(resource: str, gid: str, *, status: int) -> Refusal:
    return Refusal(status, f"{resource}: Unknown object: {gid}")


def forbidden(resource: str) -> Refusal:
    """An object that exists and the caller may not see, as reported of the real service: "You do not have access to
    this project." (https://forum.asana.com/t/67666), "... this task" (https://forum.asana.com/t/95502), "... this
    team." (https://forum.asana.com/t/289156)."""
    return Refusal(403, f"You do not have access to this {resource}.")


def premium(sentence: str) -> Refusal:
    """A feature the workspace's plan does not include, said as a whole sentence."""
    return Refusal(402, sentence)


SEARCH_IS_PREMIUM = "Search is only available to premium users."
"""Reported of the real service, 402: https://forum.asana.com/t/106546."""
FIELDS_ARE_PREMIUM = "Custom Fields are not available for free users or guests."
"""Reported of the real service: https://forum.asana.com/t/189341."""
SETTINGS_ARE_PREMIUM = "Custom Field Settings are not available for free users."
"""Reported of the real service, 402: https://forum.asana.com/t/100330."""


def unsupported(name: str) -> NotServed:
    """Something real Asana has, or a case whose answer Asana does not document, that this provider does not serve:
    the shared not-served refusal (`domain.errors`), answered 501 in Asana's envelope naming the call and `name`, and
    recorded as not implemented. Never ignored in silence."""
    return NotServed(name)


def undocumented(case: str) -> NotServed:
    """A request Asana answers in words no page, recording or report gives: refused by name, never answered with
    invented ones."""
    return unsupported(f"{case} (Asana's answer to it is not documented)")


def is_gid(value: str) -> bool:
    """Asana gids are strings of digits; anything else names no object."""
    return bool(_GID.match(value))


def not_an_id(field: str, value: str) -> Refusal:
    """Reported of the real service: "workspace: Not a recognized ID: we" (https://forum.asana.com/t/19570), "task:
    Not a recognized ID: 456789123" (https://forum.asana.com/t/1011489), "projects: [0]: Not a recognized ID: ..."
    (https://stackoverflow.com/questions/32514368)."""
    return bad(f"{field}: Not a recognized ID: {value}")


def not_a_gid_type(field: str, value: JsonValue) -> Refusal:
    """A gid sent as another JSON type, as reported: "column: Not a valid GID type: number"
    (https://github.com/Asana/python-asana/issues/93), "memberships: [0]: section: Not a valid GID type: object"
    (https://forum.asana.com/t/1108779)."""
    kind = (
        "object"
        if isinstance(value, dict | list) or value is None
        else "boolean"
        if isinstance(value, bool)
        else ("number" if isinstance(value, int | float) else "string")
    )
    return bad(f"{field}: Not a valid GID type: {kind}")


def gid_in_path(resource: str, value: str) -> str:
    if not is_gid(value):
        raise not_an_id(resource, value)
    return value


def is_user_identifier(value: str) -> bool:
    """Where Asana names a user it takes a gid, an email address, or the literal `me`."""
    return is_gid(value) or value == "me" or "@" in value


def stamp(moment: datetime) -> str:
    """Asana's timestamp, returned for every date-time it answers: `YYYY-MM-DDTHH:mm:ss.fffZ`, in UTC
    (https://developers.asana.com/docs/dates-and-times)."""
    at = moment.astimezone(UTC)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def parse_stamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def is_date(value: str) -> bool:
    if not _DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- stored


FieldKind = Literal["enum", "multi_enum", "text", "number", "date", "people"]
"""A custom field's `resource_subtype`. Asana's own vocabulary, held as a closed `Literal` rather than a
`StrEnum`: its words ("text", "date") are ordinary JSON keys elsewhere in the tree, and a StrEnum would make
every comparison against one of them read as this vocabulary spelled out."""


class CredentialKind(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class TaskSubtype(StrEnum):
    """A task's `resource_subtype` (https://developers.asana.com/reference/gettask): `milestone` and `custom` are
    documented too and refused by name."""

    DEFAULT_TASK = "default_task"
    APPROVAL = "approval"


class ApprovalStatus(StrEnum):
    """An approval task's `approval_status`: "`pending` translates to false [`completed`] while `approved`,
    `rejected`, and `changes_requested` translate to true" (`TaskBase.approval_status` in the OpenAPI subset)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class EventAction(StrEnum):
    """`Event.action` and `Event.change.action` (`EventResponse` in the OpenAPI subset)."""

    CHANGED = "changed"
    ADDED = "added"
    REMOVED = "removed"
    DELETED = "deleted"
    UNDELETED = "undeleted"


class CompletedStatus(Model):
    """A task's state is its `completed` box alone."""

    kind: Literal["completed"] = "completed"


class SectionStatus(Model):
    """A task's state is what its section in its first project means (`AsanaSection.means`)."""

    kind: Literal["section"] = "section"


class FieldStatus(Model):
    """A task's state is what the value of one enum custom field means."""

    kind: Literal["custom_field"] = "custom_field"
    field: str = Field(description="The custom field's gid")
    means: dict[str, TicketState] = Field(description="Enum option gid to the state it means")


StatusRule = Annotated[CompletedStatus | SectionStatus | FieldStatus, Field(discriminator="kind")]


class RateWindow(Model):
    """A stretch of the run's time in which every call is answered 429."""

    start: str
    end: str


class AsanaWorkspace(Model):
    resource_type: Literal["workspace"] = "workspace"
    gid: str
    name: str
    is_organization: bool = True
    email_domains: list[str] = []
    premium: bool = True
    status: StatusRule = SectionStatus()
    rate_limits: list[RateWindow] = []
    unpaginated_limit: int = Field(
        default=UNPAGINATED_MAX, ge=1, description="Past this many items a read without `limit` is refused"
    )


class AsanaUser(Model):
    resource_type: Literal["user"] = "user"
    gid: str
    name: str
    email: str
    removed: bool = Field(
        default=False,
        description="Removed from the workspace by an administrator: unlisted and unassignable; what it did before "
        "still names it",
    )


class AsanaTeam(Model):
    resource_type: Literal["team"] = "team"
    gid: str
    name: str
    workspace: str
    members: list[str] = Field(default=[], description="User gids")


class AsanaProject(Model):
    resource_type: Literal["project"] = "project"
    gid: str
    name: str
    notes: str = ""
    archived: bool = False
    workspace: str
    team: str | None = None
    private: bool = Field(default=False, description="Seen only by its members")
    members: list[str] = Field(default=[], description="User gids, in the order they joined")
    custom_fields: list[str] = Field(default=[], description="Custom field gids settled on the project, in order")
    created_at: str


class AsanaSection(Model):
    resource_type: Literal["section"] = "section"
    gid: str
    name: str
    project: str
    means: TicketState | None = Field(default=None, description="The state a task here is in, by the scenario")
    created_at: str


class AsanaEnumOption(Model):
    gid: str
    name: str
    color: str | None = None
    enabled: bool = True


class AsanaCustomField(Model):
    resource_type: Literal["custom_field"] = "custom_field"
    gid: str
    name: str
    subtype: FieldKind
    enum_options: list[AsanaEnumOption] = []
    precision: int = 0
    workspace: str


class AsanaTag(Model):
    resource_type: Literal["tag"] = "tag"
    gid: str
    name: str
    notes: str = ""
    color: str | None = None
    workspace: str
    created_at: str


class AsanaCredential(Model):
    """A token that names a user. The gid is derived from the token itself, which is never stored. Nothing about it
    is checked: a token that names no one acts as the agent."""

    resource_type: Literal["credential"] = "credential"
    gid: str
    kind: CredentialKind
    user: str


class AsanaMembership(Model):
    project: str
    section: str


class AsanaFieldValue(Model):
    """One custom field's value on a task, read by the field's kind: only the member for that kind is set."""

    field: str
    option: str | None = None
    options: list[str] = []
    text: str | None = None
    number: int | float | None = None
    date: str | None = None
    date_time: str | None = None
    people: list[str] = []


class AsanaTask(Model):
    resource_type: Literal["task"] = "task"
    resource_subtype: TaskSubtype = TaskSubtype.DEFAULT_TASK
    gid: str
    name: str = ""
    notes: str = ""
    completed: bool = False
    approval_status: ApprovalStatus | None = Field(
        default=None, description="Held by an approval task only, in step with `completed`"
    )
    completed_at: str | None = None
    due_on: str | None = None
    due_at: str | None = None
    assignee: str | None = Field(default=None, description="User gid")
    created_by: str
    workspace: str
    parent: str | None = Field(default=None, description="Task gid")
    memberships: list[AsanaMembership] = []
    tags: list[str] = []
    followers: list[str] = Field(default=[], description="User gids, in the order they were added")
    dependencies: list[str] = Field(default=[], description="Task gids this task depends on")
    custom_fields: list[AsanaFieldValue] = []
    created_at: str
    modified_at: str


class AsanaStory(Model):
    resource_type: Literal["story"] = "story"
    gid: str
    text: str
    task: str
    created_by: str
    created_at: str
    edited: bool = False


class AsanaAttachment(Model):
    """A file uploaded to a task, its bytes kept exactly as sent (base64 in the record's text)."""

    resource_type: Literal["attachment"] = "attachment"
    gid: str
    name: str
    task: str
    created_at: str
    content_type: str
    content: str = Field(description="The file's bytes, base64")
    size: int


class EventRef(Model):
    """A resource an event names: its gid and type, its subtype where it has one, and its name as it was."""

    gid: str
    resource_type: str
    resource_subtype: str | None = None
    name: str | None = None


class EventChange(Model):
    field: str
    action: EventAction
    new_value: EventRef | None = None
    added_value: EventRef | None = None
    removed_value: EventRef | None = None


class AsanaEvent(Model):
    """One event, as `GET /events` and a webhook report it. `scope` is the resources a subscription to which hears of
    it (itself, and the tasks and projects it bubbles up to: "Change events bubble up")."""

    resource_type: Literal["event"] = "event"
    gid: str
    action: EventAction
    resource: EventRef
    user: str | None = None
    parent: EventRef | None = None
    change: EventChange | None = None
    created_at: str
    scope: list[str]


class AsanaFilter(Model):
    """A `WebhookFilter`: an event passes when it matches every part the filter sets."""

    resource_type: str | None = None
    resource_subtype: str | None = None
    action: EventAction | None = None
    fields: list[str] = []


class AsanaWebhook(Model):
    resource_type: Literal["webhook"] = "webhook"
    gid: str
    resource: str = Field(description="The gid of the task or project subscribed to")
    target: str
    secret: str
    filters: list[AsanaFilter] = []
    workspace: str
    created_by: str
    created_at: str
    active: bool = False
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_failure_content: str | None = None
    delivery_retry_count: int = 0
    cursor: str = Field(description="Every event with a gid above this has not been delivered")


AnyRecord = (
    AsanaWorkspace
    | AsanaUser
    | AsanaTeam
    | AsanaProject
    | AsanaSection
    | AsanaCustomField
    | AsanaTag
    | AsanaCredential
    | AsanaAttachment
    | AsanaEvent
    | AsanaWebhook
)
Record = Annotated[AnyRecord, Field(discriminator="resource_type")]
_RECORD: TypeAdapter[AnyRecord] = TypeAdapter(Record)

StoredModel = TypeVar("StoredModel", AsanaTask, AsanaStory)


def parse_record(body: str) -> AnyRecord:
    return _RECORD.validate_json(body)


def parse(model: type[StoredModel], body: str) -> StoredModel:
    return model.model_validate_json(body)


def dump(entity: Model) -> str:
    return entity.model_dump_json()


# --------------------------------------------------------------------------- writes


Fields = dict[str, JsonValue]

_TASK_UNSUPPORTED = (
    "html_notes",
    "followers",
    "start_on",
    "start_at",
    "assignee_section",
    "external",
    "liked",
)


def envelope(body: bytes) -> Fields:
    """The object inside `{"data": ...}`; Asana refuses a write that is not wrapped."""
    try:
        decoded = json.loads(body) if body else None
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        # Reported: https://stackoverflow.com/questions/38211523
        raise bad("Could not parse request data, invalid JSON") from error
    if isinstance(decoded, dict):
        stray = [k for k in decoded if k not in ("data", "options")]
        if stray:
            raise bad(unwrapped(stray[0]))
    if not isinstance(decoded, dict) or "data" not in decoded or not isinstance(decoded["data"], dict):
        raise undocumented("a write body without a `data` object")
    return decoded["data"]


def unwrapped(field: str) -> str:
    """What Asana answers a body with a field outside `data`, as reported from the real service
    (https://forum.asana.com/t/238695)."""
    return (
        f"Unrecognized request field {field} . The only allowed keys at the top level are: data, options. "
        "Is it possible you did not wrap object properties in a data object?"
    )


def _string(fields: Fields, name: str) -> str | None:
    if name not in fields or fields[name] is None:
        return None
    value = fields[name]
    if not isinstance(value, str):
        raise undocumented(f"{name} that is not a string")
    return value


def _required(fields: Fields, name: str) -> str:
    value = _string(fields, name)
    if value is None or not value.strip():
        raise bad(f"{name}: Missing input")
    return value


def _gid_field(fields: Fields, name: str) -> str | None:
    if name not in fields or fields[name] is None:
        return None
    value = fields[name]
    if not isinstance(value, str):
        raise not_a_gid_type(name, value)
    if not is_gid(value):
        raise not_an_id(name, value)
    return value


def _gids(fields: Fields, name: str) -> list[str]:
    if name not in fields or fields[name] is None:
        return []
    raw = fields[name]
    if not isinstance(raw, list):
        raise undocumented(f"{name} that is not an array")
    found: list[str] = []
    for n, item in enumerate(raw):
        if not isinstance(item, str):
            raise not_a_gid_type(f"{name}: [{n}]", item)
        if not is_gid(item):
            raise not_an_id(f"{name}: [{n}]", item)
        found.append(item)
    return found


def _boolean(fields: Fields, name: str) -> bool:
    value = fields[name]
    if not isinstance(value, bool):
        raise undocumented(f"{name} that is not a JSON boolean")
    return value


def _due(fields: Fields) -> tuple[str | None, str | None]:
    due_on = _string(fields, "due_on")
    due_at = _string(fields, "due_at")
    if due_on is not None and due_at is not None:
        # Reported: https://forum.asana.com/t/808508
        raise bad("You may only provide one of due_on or due_at!")
    if due_on is not None and not is_date(due_on):
        raise not_a_date("due_on", due_on)
    if due_at is not None:
        parsed = parse_stamp(due_at)
        if parsed is None:
            raise undocumented(f"due_at `{due_at}`, not a date-time")
        due_at = stamp(parsed)
    return due_on, due_at


def not_a_date(field: str, value: str) -> Refusal:
    """Reported of the real service: "created_on.after: Date must be in ISO-8601 (yyyy-mm-dd) format, not: ..."
    (https://forum.asana.com/t/69643)."""
    return bad(f"{field}: Date must be in ISO-8601 (yyyy-mm-dd) format, not: {value}")


def _assignee(fields: Fields) -> str | None:
    """As sent; a value that names no user is refused where the workspace is known (`app.View.user`)."""
    value = fields["assignee"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise not_a_gid_type("assignee", value)
    return value


def _notes(fields: Fields) -> str | None:
    """`notes` as sent; None when it was not sent. `html_notes` is not served: Asana keeps the two as one text and
    documents neither how it reads the plain text out of the HTML nor how it marks plain text up
    (https://developers.asana.com/docs/rich-text), so neither can be answered for the other."""
    if "notes" in fields:
        return _string(fields, "notes") or ""
    return None


def _custom_fields(fields: Fields) -> dict[str, JsonValue]:
    """`{field gid: value}` as sent; each value is read against its field's definition by the caller."""
    raw = fields["custom_fields"]
    if raw is None:
        return {}
    if isinstance(raw, str | int | float) and not isinstance(raw, bool):
        # Reported: https://forum.asana.com/t/170757
        raise bad(f"custom_fields: Value is not a JSON object: {raw}")
    if not isinstance(raw, dict):
        raise undocumented("custom_fields that are not an object")
    for gid in raw:
        if not is_gid(gid):
            raise not_an_id("custom_fields", gid)
    return dict(raw)


def _refuse_unsupported(fields: Fields, names: Sequence[str]) -> None:
    for name in names:
        if name in fields:
            raise unsupported(name)


class MembershipIn(Model):
    project: str
    section: str | None = None


def _memberships(fields: Fields) -> list[MembershipIn]:
    raw = fields["memberships"]
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise undocumented("memberships that are not an array")
    found: list[MembershipIn] = []
    for n, item in enumerate(raw):
        if not isinstance(item, dict):
            raise undocumented("a membership that is not an object")
        project = _gid_field(item, "project")
        if project is None:
            # Reported: https://forum.asana.com/t/10481
            raise bad(f"memberships: [{n}]: project: Missing required field")
        found.append(MembershipIn(project=project, section=_gid_field(item, "section")))
    return found


def _subtype(fields: Fields) -> TaskSubtype:
    """`resource_subtype`: `default_task` and `approval` are served; `milestone` and `custom`, which the reference
    documents too, are refused by name."""
    value = _string(fields, "resource_subtype")
    if value is None:
        return TaskSubtype.DEFAULT_TASK
    try:
        return TaskSubtype(value)
    except ValueError as error:
        raise unsupported(f"resource_subtype `{value}`") from error


def _approval(fields: Fields) -> ApprovalStatus | None:
    value = _string(fields, "approval_status")
    if value is None:
        return None
    try:
        return ApprovalStatus(value)
    except ValueError as error:
        raise unsupported(f"approval_status `{value}`") from error


def approval_state(
    subtype: TaskSubtype, status: ApprovalStatus | None, completed: bool | None, *, was: ApprovalStatus | None = None
) -> tuple[bool | None, ApprovalStatus | None]:
    """An approval task's `completed` and `approval_status` after a write that sent `status` and/or `completed`
    ("This field is kept in sync with `completed`, meaning `pending` translates to false while `approved`,
    `rejected`, and `changes_requested` translate to true. If you set completed to true, this field will be set to
    `approved`"): the pair to keep, None where the write set neither. A status on a task that is no approval, and a
    status that contradicts `completed`, are refused by name."""
    if subtype is not TaskSubtype.APPROVAL:
        if status is not None:
            raise undocumented("an approval_status on a task whose resource_subtype is not approval")
        return completed, None
    if status is not None:
        if completed is not None and completed != (status is not ApprovalStatus.PENDING):
            raise undocumented(f"approval_status `{status}` together with a contradicting `completed`")
        return status is not ApprovalStatus.PENDING, status
    if completed is None:
        return None, was
    return completed, ApprovalStatus.APPROVED if completed else ApprovalStatus.PENDING


class TaskCreate(Model):
    name: str = ""
    notes: str = ""
    resource_subtype: TaskSubtype = TaskSubtype.DEFAULT_TASK
    approval_status: ApprovalStatus | None = None
    completed: bool = False
    due_on: str | None = None
    due_at: str | None = None
    assignee: str | None = Field(default=None, description="As the caller named the user: gid, email or `me`")
    projects: list[str] = []
    memberships: list[MembershipIn] = []
    workspace: str | None = None
    parent: str | None = None
    tags: list[str] = []
    custom_fields: dict[str, JsonValue] = {}


def task_create(fields: Fields, *, parent: str | None = None) -> TaskCreate:
    """A new task; `parent` is the task a subtask is created under, when the path names it."""
    _refuse_unsupported(fields, _TASK_UNSUPPORTED)
    due_on, due_at = _due(fields)
    projects = _gids(fields, "projects")
    memberships = _memberships(fields) if "memberships" in fields else []
    if projects and memberships:
        # Asana takes both (https://forum.asana.com/t/92977); how it combines them is not documented.
        raise undocumented("a task created with both projects and memberships")
    workspace = _gid_field(fields, "workspace")
    under = parent if parent is not None else _gid_field(fields, "parent")
    if not projects and not memberships and workspace is None and under is None:
        # Reported: https://forum.asana.com/t/44096
        raise bad("You should specify one of workspace, parent, projects")
    subtype = _subtype(fields)
    sent_completed = _boolean(fields, "completed") if "completed" in fields else None
    completed, approval = approval_state(subtype, _approval(fields), sent_completed)
    if subtype is TaskSubtype.APPROVAL and approval is None:
        completed, approval = False, ApprovalStatus.PENDING
    return TaskCreate(
        name=_string(fields, "name") or "",
        notes=_notes(fields) or "",
        resource_subtype=subtype,
        approval_status=approval,
        completed=bool(completed),
        due_on=due_on,
        due_at=due_at,
        assignee=_assignee(fields) if "assignee" in fields else None,
        projects=projects,
        memberships=memberships,
        workspace=workspace,
        parent=under,
        tags=_gids(fields, "tags"),
        custom_fields=_custom_fields(fields) if "custom_fields" in fields else {},
    )


class TaskUpdate(Model):
    """Only the fields the caller sent are set; `model_fields_set` says which."""

    name: str = ""
    notes: str = ""
    completed: bool = False
    approval_status: ApprovalStatus | None = None
    due_on: str | None = None
    due_at: str | None = None
    assignee: str | None = None
    custom_fields: dict[str, JsonValue] = {}


def task_update(fields: Fields) -> TaskUpdate:
    _refuse_unsupported(fields, _TASK_UNSUPPORTED)
    if "resource_subtype" in fields:
        raise undocumented("resource_subtype written on a task update")
    for fixed in ("projects", "tags"):
        if fixed in fields:
            # Reported: https://forum.asana.com/t/77626, https://stackoverflow.com/questions/42604985
            raise bad(f"{fixed}: Cannot write this property")
    for fixed in ("workspace", "memberships", "parent"):
        if fixed in fields:
            raise undocumented(f"{fixed} written on a task update")
    sent: dict[str, JsonValue] = {}
    if "name" in fields:
        sent["name"] = _string(fields, "name") or ""
    notes = _notes(fields)
    if notes is not None:
        sent["notes"] = notes
    if "completed" in fields:
        sent["completed"] = _boolean(fields, "completed")
    if "approval_status" in fields:
        sent["approval_status"] = _approval(fields)
    due_on, due_at = _due(fields)
    if "due_on" in fields:
        sent["due_on"] = due_on
    if "due_at" in fields:
        sent["due_at"] = due_at
    if "assignee" in fields:
        sent["assignee"] = _assignee(fields)
    if "custom_fields" in fields:
        sent["custom_fields"] = _custom_fields(fields)
    return TaskUpdate.model_validate(sent)


class ParentIn(Model):
    parent: str | None


def parent_in(fields: Fields) -> ParentIn:
    _refuse_unsupported(fields, ("insert_before", "insert_after"))
    if "parent" not in fields:
        raise bad("parent: Missing input")
    return ParentIn(parent=_gid_field(fields, "parent"))


def one_gid(fields: Fields, name: str) -> str:
    """The single gid a small action names: `{"task": gid}`, `{"tag": gid}`, `{"custom_field": gid}`."""
    _refuse_unsupported(fields, ("insert_before", "insert_after", "is_important"))
    if name not in fields or fields[name] is None:
        raise bad(f"{name}: Missing input")
    value = _gid_field(fields, name)
    assert value is not None
    return value


def members_in(fields: Fields) -> list[str]:
    """`members`: Asana takes one comma-separated string, or an array, of user identifiers."""
    if "members" not in fields or fields["members"] is None:
        raise bad("members: Missing input")  # the documented form: https://developers.asana.com/docs/errors
    raw = fields["members"]
    named: list[str] = []
    if isinstance(raw, str):
        named = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, str):
                raise undocumented("a member that is not a string")
            named.append(item.strip())
    else:
        raise undocumented("members that are neither a string nor an array")
    if not named:
        raise bad("members: Missing input")
    for identifier in named:
        if not is_user_identifier(identifier):
            raise undocumented(f"a member `{identifier}` that is not a gid, an email or `me`")
    return named


_PROJECT_UNSUPPORTED = (
    "public",
    "privacy_setting",
    "color",
    "default_view",
    "due_on",
    "due_date",
    "start_on",
    "html_notes",
    "owner",
    "followers",
    "members",
    "custom_fields",
    "icon",
    "default_access_level",
)


class ProjectCreate(Model):
    name: str
    notes: str = ""
    workspace: str | None = None
    team: str | None = None
    archived: bool = False


def project_create(fields: Fields) -> ProjectCreate:
    _refuse_unsupported(fields, _PROJECT_UNSUPPORTED)
    return ProjectCreate(
        name=_required(fields, "name"),
        notes=_string(fields, "notes") or "",
        workspace=_gid_field(fields, "workspace"),
        team=_gid_field(fields, "team"),
        archived=_boolean(fields, "archived") if "archived" in fields else False,
    )


class SectionCreate(Model):
    name: str


def section_create(fields: Fields) -> SectionCreate:
    _refuse_unsupported(fields, ("insert_before", "insert_after"))
    return SectionCreate(name=_required(fields, "name"))


class TagCreate(Model):
    name: str
    color: str | None = None
    workspace: str | None = None


def tag_create(fields: Fields) -> TagCreate:
    _refuse_unsupported(fields, ("followers", "notes"))
    return TagCreate(
        name=_required(fields, "name"), color=_string(fields, "color"), workspace=_gid_field(fields, "workspace")
    )


class StoryCreate(Model):
    text: str


def story_create(fields: Fields) -> StoryCreate:
    _refuse_unsupported(fields, ("html_text", "is_pinned", "sticker_name"))
    text = _string(fields, "text")
    if text is None:
        raise bad("text: Missing input")  # the documented form: https://developers.asana.com/docs/errors
    if not text.strip():
        raise undocumented("a comment of nothing but spaces")
    return StoryCreate(text=text)


def followers_in(fields: Fields) -> list[str]:
    """`followers`: "An array of strings identifying users. These can either be the string "me", an email, or the
    gid of a user" (`TaskAddFollowersRequest`)."""
    raw = fields["followers"] if "followers" in fields else None
    if raw is None:
        raise bad("followers: Missing input")
    if not isinstance(raw, list):
        raise undocumented("followers that are not an array")
    named: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise undocumented("a follower that is not a string")
        if not is_user_identifier(item.strip()):
            raise undocumented(f"a follower `{item}` that is not a gid, an email or `me`")
        named.append(item.strip())
    return named


class ProjectOp(Model):
    project: str
    section: str | None = None


def project_op(fields: Fields, *, adding: bool) -> ProjectOp:
    """`addProject` and `removeProject`. A position (`insert_before`, `insert_after`) is not modelled: tasks are not
    ordered here, so one is refused by name."""
    if adding:
        _refuse_unsupported(fields, ("insert_before", "insert_after"))
    project = _gid_field(fields, "project")
    if project is None:
        raise bad("project: Missing input")
    return ProjectOp(project=project, section=_gid_field(fields, "section") if adding else None)


def dependency_gids(fields: Fields, name: str) -> list[str]:
    """`dependencies` or `dependents`: "An array of task gids"."""
    if name not in fields or fields[name] is None:
        raise bad(f"{name}: Missing input")
    return _gids(fields, name)


def section_update(fields: Fields) -> SectionCreate:
    """ "at this time, the only field that can be updated is the `name` field" (updateSection); a position is not
    modelled."""
    _refuse_unsupported(fields, ("insert_before", "insert_after"))
    return SectionCreate(name=_required(fields, "name"))


class TagUpdate(Model):
    """Only the fields the caller sent are set; `model_fields_set` says which."""

    name: str = ""
    color: str | None = None
    notes: str = ""


def tag_update(fields: Fields) -> TagUpdate:
    sent: dict[str, JsonValue] = {}
    if "name" in fields:
        sent["name"] = _required(fields, "name")
    if "color" in fields:
        sent["color"] = _string(fields, "color")
    if "notes" in fields:
        sent["notes"] = _string(fields, "notes") or ""
    return TagUpdate.model_validate(sent)


class StoryUpdate(Model):
    text: str


def story_update(fields: Fields) -> StoryUpdate:
    """Only a comment's `text` is served; `html_text`, `is_pinned`, `sticker_name` and `resource_subtype` are
    refused by name."""
    _refuse_unsupported(fields, ("html_text", "is_pinned", "sticker_name", "resource_subtype"))
    return StoryUpdate(text=story_create(fields).text)


_PROJECT_UPDATE_OTHERS = (
    "resource_subtype",
    "color",
    "current_status",
    "current_status_update",
    "custom_field_settings",
    "default_access_level",
    "default_view",
    "due_date",
    "due_on",
    "html_notes",
    "icon",
    "members",
    "minimum_access_level_for_customization",
    "minimum_access_level_for_sharing",
    "privacy_setting",
    "public",
    "start_on",
    "custom_fields",
    "custom_type",
    "followers",
    "html_custom_fields",
    "owner",
    "team",
)
"""The other properties `ProjectUpdateRequest` documents; refused by name when sent."""


class ProjectUpdate(Model):
    """Only the fields the caller sent are set; `model_fields_set` says which."""

    name: str = ""
    notes: str = ""
    archived: bool = False


def project_update(fields: Fields) -> ProjectUpdate:
    _refuse_unsupported(fields, _PROJECT_UPDATE_OTHERS)
    sent: dict[str, JsonValue] = {}
    if "name" in fields:
        sent["name"] = _required(fields, "name")
    if "notes" in fields:
        sent["notes"] = _string(fields, "notes") or ""
    if "archived" in fields:
        sent["archived"] = _boolean(fields, "archived")
    return ProjectUpdate.model_validate(sent)


def _filters(fields: Fields) -> list[AsanaFilter]:
    raw = fields["filters"] if "filters" in fields else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise undocumented("filters that are not an array")
    found: list[AsanaFilter] = []
    for item in raw:
        if not isinstance(item, dict):
            raise undocumented("a filter that is not an object")
        action = _string(item, "action")
        try:
            kind = EventAction(action) if action is not None else None
        except ValueError as error:
            raise unsupported(f"a filter action `{action}`") from error
        listed = item["fields"] if "fields" in item else None
        if listed is not None and (not isinstance(listed, list) or not all(isinstance(f, str) for f in listed)):
            raise undocumented("filter fields that are not an array of strings")
        names = [str(f) for f in listed] if isinstance(listed, list) else []
        if names and kind is not EventAction.CHANGED:
            # "This field is only valid for `action` of type `changed`" (WebhookFilter.fields)
            raise undocumented("filter fields on a filter whose action is not `changed`")
        found.append(
            AsanaFilter(
                resource_type=_string(item, "resource_type"),
                resource_subtype=_string(item, "resource_subtype"),
                action=kind,
                fields=names,
            )
        )
    return found


class WebhookCreate(Model):
    resource: str
    target: str
    filters: list[AsanaFilter] = []


def webhook_create(fields: Fields) -> WebhookCreate:
    resource = _gid_field(fields, "resource")
    if resource is None:
        raise bad("resource: Missing input")
    target = _string(fields, "target")
    if target is None or not target.strip():
        raise bad("target: Missing input")
    return WebhookCreate(resource=resource, target=target, filters=_filters(fields))


def webhook_update(fields: Fields) -> list[AsanaFilter]:
    """`filters` is the only property `WebhookUpdateRequest` has; they replace those held."""
    if "filters" not in fields:
        raise bad("filters: Missing input")
    return _filters(fields)


class AttachmentUpload(Model):
    parent: str
    name: str
    content_type: str
    content: bytes


MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
"""https://developers.asana.com/reference/createattachmentforobject: "The 100MB size limit on attachments in Asana is
enforced on this endpoint"."""


def attachment_upload(content_type: str, raw: bytes) -> AttachmentUpload:
    """A `multipart/form-data` body: `parent` and the `file` part, whose bytes are kept exactly. `url`, `name`,
    `resource_subtype` and `connect_to_app` (the external attachment's) are refused by name. A file name is
    URL-decoded, as the reference tells a client to encode it."""
    kind, params = _header(content_type)
    boundary = params["boundary"] if "boundary" in params else None
    if kind != "multipart/form-data" or not boundary:
        raise undocumented("an attachment upload that is not multipart/form-data with a boundary")
    texts: dict[str, JsonValue] = {}
    file: tuple[str, str, bytes, str] | None = None
    delimiter = b"--" + boundary.encode()
    for piece in raw.split(delimiter)[1:]:
        if piece.startswith(b"--"):
            break
        piece = piece.removeprefix(b"\r\n")
        head, gap, body = piece.partition(b"\r\n\r\n")
        if not gap:
            raise undocumented("a multipart part with no headers")
        body = body.removesuffix(b"\r\n")
        message = Message()
        part_type = "application/octet-stream"
        for line in head.decode("utf-8", errors="replace").split("\r\n"):
            name, _, value = line.partition(":")
            message[name.strip()] = value.strip()
        disposition = message["content-disposition"] if "content-disposition" in message else ""
        if "content-type" in message:
            part_type = str(message["content-type"])
        field = message.get_param("name", header="content-disposition")
        if not isinstance(field, str) or not disposition:
            raise undocumented("a multipart part with no field name")
        filename = message.get_filename()
        if filename is not None:
            file = (field, filename, body, part_type)
        else:
            texts[field] = body.decode("utf-8")
    _refuse_unsupported(texts, ("url", "name", "resource_subtype", "connect_to_app"))
    parent = texts["parent"] if "parent" in texts else None
    if not isinstance(parent, str):
        raise bad("parent: Missing input")
    if file is None or file[0] != "file":
        raise bad("file: Missing input")
    if len(file[2]) > MAX_ATTACHMENT_BYTES:
        raise undocumented("an attachment over 100MB")
    return AttachmentUpload(parent=parent, name=unquote(file[1]), content_type=file[3], content=file[2])


def _header(value: str) -> tuple[str, dict[str, str]]:
    message = Message()
    message["content-type"] = value
    return message.get_content_type(), {k.lower(): str(v) for k, v in message.get_params(failobj=[])[1:]}


def custom_field_value(definition: AsanaCustomField, sent: JsonValue, users: Mapping[str, str]) -> AsanaFieldValue:
    """A value sent for one field, read the way its kind takes it; refused in Asana's words when it does not fit.

    `users` maps each user identifier the caller may name (a gid, an email, `me`) to its user gid.
    """
    where = f"custom_fields.{definition.gid}"
    value = AsanaFieldValue(field=definition.gid)
    if sent is None:
        return value
    match definition.subtype:
        case "enum":
            if not isinstance(sent, str) or not is_gid(sent):
                raise undocumented(f"{where} that is not an enum option's gid")
            return value.model_copy(update={"option": _enum_option(definition, sent, where)})
        case "multi_enum":
            if not isinstance(sent, list):
                raise undocumented(f"{where} that is not an array")
            chosen: list[str] = []
            for item in sent:
                if not isinstance(item, str) or not is_gid(item):
                    raise undocumented(f"{where} holding something that is not an enum option's gid")
                chosen.append(_enum_option(definition, item, where))
            return value.model_copy(update={"options": list(dict.fromkeys(chosen))})
        case "text":
            if not isinstance(sent, str):
                raise undocumented(f"{where} that is not a string")
            return value.model_copy(update={"text": sent})
        case "number":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
            if isinstance(sent, bool) or not isinstance(sent, int | float):
                raise undocumented(f"{where} that is not a number")
            return value.model_copy(update={"number": sent})
        case "date":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
            return _date_value(value, sent, where)
        case "people":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
            if not isinstance(sent, list):
                raise undocumented(f"{where} that is not an array")
            people: list[str] = []
            for item in sent:
                if not isinstance(item, str) or item not in users:
                    raise undocumented(f"{where} naming someone who is not a user of the workspace")
                people.append(users[item])
            return value.model_copy(update={"people": list(dict.fromkeys(people))})


def _date_value(value: AsanaFieldValue, sent: JsonValue, where: str) -> AsanaFieldValue:
    """`{"date": "YYYY-MM-DD"}` or `{"date_time": "..."}`; a moment also answers its UTC date."""
    if not isinstance(sent, dict):
        raise undocumented(f"{where} that is not an object")
    at = sent["date_time"] if "date_time" in sent else None
    if at is not None:
        moment = parse_stamp(at) if isinstance(at, str) else None
        if moment is None:
            raise undocumented(f"{where}.date_time that is not a date-time")
        return value.model_copy(update={"date": moment.astimezone(UTC).date().isoformat(), "date_time": stamp(moment)})
    on = sent["date"] if "date" in sent else None
    if on is None:
        return value
    if not isinstance(on, str) or not is_date(on):
        raise undocumented(f"{where}.date that is not a date")
    return value.model_copy(update={"date": on})


def _enum_option(definition: AsanaCustomField, gid: str, where: str) -> str:
    option = next((o for o in definition.enum_options if o.gid == gid), None)
    if option is None:
        raise undocumented(f"{where} naming an enum option the field does not have")
    if not option.enabled:
        raise undocumented(f"{where} naming a disabled enum option")
    return gid


class TokenGrant(Model):
    """A form-encoded request to `/-/oauth_token`."""

    grant_type: str
    refresh_token: str | None = None


def token_grant(body: bytes) -> TokenGrant:
    pairs = dict(parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True))
    if "grant_type" not in pairs:
        raise undocumented("a token request without a grant_type")
    return TokenGrant(
        grant_type=pairs["grant_type"], refresh_token=pairs["refresh_token"] if "refresh_token" in pairs else None
    )


def token_answer(access_token: str, user: AsanaUser) -> bytes:
    """What `/-/oauth_token` answers to a refresh: the new token, how long it lasts, and who it acts as."""
    return json.dumps(
        {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": TOKEN_LIFETIME_SECONDS,
            "data": {"id": int(user.gid), "gid": user.gid, "name": user.name, "email": user.email},
        }
    ).encode()


def display_number(number: float, precision: int) -> str:
    return f"{number:.{precision}f}"


# --------------------------------------------------------------------------- query


class Query:
    """The query string as Asana reads it: one value per name."""

    def __init__(self, pairs: Sequence[tuple[str, str]]) -> None:
        self._values: dict[str, str] = {}
        for name, value in pairs:
            self._values[name] = value

    def names(self) -> list[str]:
        return list(self._values)

    def has(self, name: str) -> bool:
        return name in self._values

    def text(self, name: str) -> str | None:
        return self._values[name] if name in self._values else None

    def boolean(self, name: str) -> bool | None:
        value = self.text(name)
        if value is None:
            return None
        spelled = value.lower()  # Asana's own SDK sends Python's `False`
        if spelled not in ("true", "false"):
            raise undocumented(f"{name} `{value}`, not a boolean")
        return spelled == "true"

    def count(self, name: str) -> int | None:
        value = self.text(name)
        if value is None:
            return None
        if not value.isdigit() or not 1 <= int(value) <= PAGE_MAX:
            raise undocumented(f"{name} `{value}`, not between 1 and {PAGE_MAX}")
        return int(value)

    def moment(self, name: str) -> datetime | None:
        value = self.text(name)
        if value is None:
            return None
        parsed = parse_stamp(value)
        if parsed is None:
            raise undocumented(f"{name} `{value}`, not a date-time")
        return parsed

    def gids(self, name: str) -> list[str] | None:
        """A comma-separated list of gids, as the search filters take them."""
        value = self.text(name)
        if value is None:
            return None
        found = [part.strip() for part in value.split(",") if part.strip()]
        for gid in found:
            if not is_gid(gid):
                raise not_an_id(name, gid)
        return found

    def refuse(self, names: Sequence[str]) -> None:
        for name in names:
            if self.has(name):
                raise unsupported(name)


# --------------------------------------------------------------------------- responses


class Compact(Model):
    gid: str
    resource_type: str
    name: str


class WorkspaceOut(Model):
    gid: str
    resource_type: Literal["workspace"] = "workspace"
    name: str
    is_organization: bool
    email_domains: list[str]


class UserOut(Model):
    gid: str
    resource_type: Literal["user"] = "user"
    name: str
    email: str
    workspaces: list[WorkspaceOut]


class TeamOut(Model):
    gid: str
    resource_type: Literal["team"] = "team"
    name: str
    description: str = ""
    organization: WorkspaceOut
    permalink_url: str


class ProjectOut(Model):
    gid: str
    resource_type: Literal["project"] = "project"
    name: str
    notes: str
    archived: bool
    public: bool
    created_at: str
    workspace: WorkspaceOut
    team: TeamOut | None
    members: list[UserOut]
    permalink_url: str


class SectionOut(Model):
    gid: str
    resource_type: Literal["section"] = "section"
    name: str
    created_at: str
    project: ProjectOut


class EnumOptionOut(Model):
    gid: str
    resource_type: Literal["enum_option"] = "enum_option"
    name: str
    enabled: bool
    color: str | None


_KIND_KEYS: Mapping[str, tuple[str, ...]] = {
    "enum": ("enum_options", "enum_value"),
    "multi_enum": ("enum_options", "multi_enum_values"),
    "text": ("text_value",),
    "number": ("number_value", "precision"),
    "date": ("date_value",),
    "people": ("people_value",),
}
_VALUE_KEYS = {k for keys in _KIND_KEYS.values() for k in keys}


def _only_its_kind(dumped: dict[str, object], kind: FieldKind) -> dict[str, object]:
    """Asana answers a custom field with the members its kind has, and none of the others."""
    return {k: v for k, v in dumped.items() if k not in _VALUE_KEYS or k in _KIND_KEYS[kind]}


class CustomFieldOut(Model):
    """A custom field's definition."""

    gid: str
    resource_type: Literal["custom_field"] = "custom_field"
    name: str
    resource_subtype: FieldKind
    type: FieldKind
    enum_options: list[EnumOptionOut] | None
    precision: int | None
    enabled: bool = True
    is_global_to_workspace: bool = True
    is_formula_field: bool = False

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        return _only_its_kind(handler(self), self.resource_subtype)


class DateValueOut(Model):
    date: str | None
    date_time: str | None


class CustomFieldValueOut(Model):
    """A custom field as a task carries it: the definition, the value in the member for its kind, and how it reads."""

    gid: str
    resource_type: Literal["custom_field"] = "custom_field"
    name: str
    resource_subtype: FieldKind
    type: FieldKind
    enabled: bool = True
    is_formula_field: bool = False
    display_value: str | None
    enum_options: list[EnumOptionOut] | None = None
    enum_value: EnumOptionOut | None = None
    multi_enum_values: list[EnumOptionOut] | None = None
    number_value: int | float | None = None
    precision: int | None = None
    text_value: str | None = None
    date_value: DateValueOut | None = None
    people_value: list[UserOut] | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        return _only_its_kind(handler(self), self.resource_subtype)


class CustomFieldSettingOut(Model):
    gid: str
    resource_type: Literal["custom_field_setting"] = "custom_field_setting"
    is_important: bool = False
    custom_field: CustomFieldOut
    project: ProjectOut
    parent: ProjectOut


class ProjectMembershipOut(Model):
    gid: str
    resource_type: Literal["project_membership"] = "project_membership"
    user: UserOut
    member: UserOut
    project: ProjectOut
    parent: ProjectOut


class TagOut(Model):
    gid: str
    resource_type: Literal["tag"] = "tag"
    name: str
    color: str | None
    notes: str = ""
    created_at: str
    workspace: WorkspaceOut
    followers: list[UserOut] = []
    permalink_url: str


class MembershipOut(Model):
    project: ProjectOut
    section: SectionOut


class TaskRefOut(Model):
    """A task named by another: its parent."""

    gid: str
    resource_type: Literal["task"] = "task"
    resource_subtype: TaskSubtype = TaskSubtype.DEFAULT_TASK
    name: str
    completed: bool
    permalink_url: str


class ResourceRefOut(Model):
    """A task a dependency field names: "The objects contain only the gid" (`TaskBase.dependencies`)."""

    gid: str
    resource_type: Literal["task"] = "task"


class TaskOut(Model):
    """A nested resource is held in full so `opt_fields` can reach into it; it is answered compact otherwise."""

    gid: str
    resource_type: Literal["task"] = "task"
    resource_subtype: TaskSubtype = TaskSubtype.DEFAULT_TASK
    name: str
    notes: str
    completed: bool
    completed_at: str | None
    approval_status: ApprovalStatus | None = Field(
        default=None, description="*Conditional*: an approval task's only; left out of any other"
    )
    due_on: str | None
    due_at: str | None
    created_at: str
    modified_at: str
    assignee: UserOut | None
    created_by: UserOut
    followers: list[UserOut] = []
    parent: TaskRefOut | None
    num_subtasks: int
    dependencies: list[ResourceRefOut] = []
    dependents: list[ResourceRefOut] = []
    memberships: list[MembershipOut]
    projects: list[ProjectOut]
    tags: list[TagOut]
    custom_fields: list[CustomFieldValueOut]
    workspace: WorkspaceOut
    permalink_url: str

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        dumped = handler(self)
        if self.approval_status is None:
            dumped.pop("approval_status", None)
        return dumped


class StoryOut(Model):
    gid: str
    resource_type: Literal["story"] = "story"
    resource_subtype: Literal["comment_added"] = "comment_added"
    type: Literal["comment"] = "comment"
    text: str
    is_edited: bool = False
    created_at: str
    created_by: UserOut
    target: Compact


class AttachmentParentOut(Model):
    """`Attachment.parent`: the task, compact, with who made it."""

    gid: str
    resource_type: Literal["task"] = "task"
    resource_subtype: TaskSubtype
    name: str
    created_by: UserOut


class AttachmentOut(Model):
    gid: str
    resource_type: Literal["attachment"] = "attachment"
    resource_subtype: Literal["asana"] = "asana"
    host: Literal["asana"] = "asana"
    name: str
    created_at: str
    size: int
    download_url: str
    parent: AttachmentParentOut


class FilterOut(Model):
    resource_type: str | None
    resource_subtype: str | None
    action: EventAction | None
    fields: list[str] | None


class WebhookOut(Model):
    gid: str
    resource_type: Literal["webhook"] = "webhook"
    active: bool
    resource: Compact
    target: str
    created_at: str
    last_success_at: str | None
    last_failure_at: str | None
    last_failure_content: str | None
    delivery_retry_count: int
    filters: list[FilterOut]


Representation = (
    AttachmentOut
    | WebhookOut
    | TaskOut
    | UserOut
    | WorkspaceOut
    | TeamOut
    | ProjectOut
    | SectionOut
    | StoryOut
    | CustomFieldOut
    | CustomFieldSettingOut
    | ProjectMembershipOut
    | TagOut
)

COMPACT: Mapping[str, tuple[str, ...]] = {
    "task": ("gid", "resource_type", "name", "resource_subtype"),
    "user": ("gid", "resource_type", "name"),
    "workspace": ("gid", "resource_type", "name"),
    "team": ("gid", "resource_type", "name"),
    "project": ("gid", "resource_type", "name"),
    "section": ("gid", "resource_type", "name"),
    "tag": ("gid", "resource_type", "name"),
    "story": ("gid", "resource_type", "created_at", "created_by", "resource_subtype", "text"),
    "attachment": ("gid", "resource_type", "name", "resource_subtype"),
    "webhook": ("gid", "resource_type", "active", "resource", "target"),
    "enum_option": ("gid", "resource_type", "name", "enabled", "color"),
    "custom_field": (
        "gid",
        "resource_type",
        "name",
        "type",
        "enum_options",
        "enum_value",
        "multi_enum_values",
        "number_value",
        "text_value",
        "date_value",
        "display_value",
        "enabled",
        "is_formula_field",
    ),
    "custom_field_setting": ("gid", "resource_type"),
    "project_membership": ("gid", "resource_type", "member"),
}
"""Each resource's compact record: the fields of its `...Compact` schema in Asana's OpenAPI document that this provider
serves, less those marked [Opt In] (https://developers.asana.com/docs/inputoutput-options)."""

OPT_IN: Mapping[str, frozenset[str]] = {
    "task": frozenset({"num_subtasks", "dependencies", "dependents"}),
    "team": frozenset({"description"}),
    "project_membership": frozenset({"parent", "project"}),
}
"""Fields Asana's OpenAPI document marks [Opt In]: answered only when `opt_fields` names them, never in a full record."""

FieldTree = dict[str, "FieldTree"]

_GROUP = re.compile(r"\(([^()]*)\)")


def _expanded(path: str) -> list[str]:
    """One `opt_fields` path with its groups spread out: `(followers|assignee).name` is two paths
    (https://developers.asana.com/docs/inputoutput-options)."""
    found = _GROUP.search(path)
    if found is None:
        return [path]
    return [
        expanded
        for term in found.group(1).split("|")
        for expanded in _expanded(path[: found.start()] + term + path[found.end() :])
    ]


def field_tree(query: Query) -> FieldTree | None:
    """`opt_fields=name,assignee.email` as a nested selection; None when the caller named none. A path may start
    with `this.` and may group terms, `(a|b)`, as Asana's input/output options page writes them."""
    raw = query.text("opt_fields")
    if raw is None:
        return None
    tree: FieldTree = {}
    for written in raw.split(","):
        for path in _expanded(written.strip()):
            node = tree
            parts = [p for p in path.split(".") if p]
            for part in parts[1:] if parts[:1] == ["this"] else parts:
                node = node.setdefault(part, {})
    return tree


def _known(model: type[Model]) -> frozenset[str]:
    return frozenset(model.model_fields)


KNOWN: Mapping[str, frozenset[str]] = {
    "task": _known(TaskOut) | _known(TaskRefOut),
    "attachment": _known(AttachmentOut),
    "webhook": _known(WebhookOut),
    "user": _known(UserOut),
    "workspace": _known(WorkspaceOut),
    "team": _known(TeamOut),
    "project": _known(ProjectOut),
    "section": _known(SectionOut),
    "story": _known(StoryOut),
    "custom_field": _known(CustomFieldOut) | _known(CustomFieldValueOut),
    "custom_field_setting": _known(CustomFieldSettingOut),
    "project_membership": _known(ProjectMembershipOut),
    "tag": _known(TagOut),
    "enum_option": _known(EnumOptionOut),
}
"""Every field each resource is ever answered with here. A field `opt_fields` names that is not one is refused by name:
answering without it would say Asana has no such field, or that it is empty."""


def _reference(value: JsonValue) -> JsonValue:
    """A field named with nothing under it: a nested object answers its gid and resource_type.

    A custom field is the exception: `custom_fields` named bare answers each field with the value it holds,
    because callers read a task's priority or status that way and name nothing under it.
    """
    if isinstance(value, list):
        return [_reference(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "resource_type" in value and value["resource_type"] == "custom_field":
        return _compact(value)
    if "gid" in value:
        return {k: value[k] for k in ("gid", "resource_type") if k in value}
    return {k: _reference(v) for k, v in value.items()}


def _narrow(value: JsonValue, tree: FieldTree, at: str) -> JsonValue:
    """Exactly the fields `tree` names, and `gid`. A field this resource is never answered with, or a path into
    something that is not an object, is refused naming the path."""
    if not tree:
        return _reference(value)
    if isinstance(value, list):
        return [_narrow(item, tree, at) for item in value]
    if value is None:
        return None
    if not isinstance(value, dict):
        raise unsupported(f"opt_fields={at}.{next(iter(tree))}")
    kind = value["resource_type"] if "resource_type" in value else None
    known = KNOWN[kind] if isinstance(kind, str) and kind in KNOWN else frozenset(value)
    out: dict[str, JsonValue] = {"gid": value["gid"]} if "gid" in value else {}
    for key, sub in tree.items():
        path = f"{at}.{key}" if at else key
        if key not in known:
            raise unsupported(f"opt_fields={path}")
        if key in value:
            out[key] = _narrow(value[key], sub, path)
    return out


def _compact(value: JsonValue) -> JsonValue:
    """A resource's compact record; an object without a `resource_type` (a membership) is compacted inside."""
    if isinstance(value, list):
        return [_compact(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value["resource_type"] if "resource_type" in value else None
    if isinstance(kind, str) and kind in COMPACT:
        return {k: _compact(value[k]) for k in COMPACT[kind] if k in value}
    return {k: _compact(v) for k, v in value.items()}


NESTED_FULL: frozenset[tuple[str, str]] = frozenset(
    {("task", "custom_fields"), ("custom_field_setting", "custom_field"), ("webhook", "filters")}
)
"""The fields of a full record that Asana's OpenAPI document gives as another resource's full record
(`TaskResponse.custom_fields` is `[CustomFieldResponse]`, `CustomFieldSettingResponse.custom_field` a
`CustomFieldResponse`); every other resource a full record names is compact."""


def _full(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    kind = value["resource_type"] if "resource_type" in value else None
    hidden = OPT_IN[kind] if isinstance(kind, str) and kind in OPT_IN else frozenset()
    out: dict[str, JsonValue] = {}
    for key, held in value.items():
        if key in hidden:
            continue
        if isinstance(kind, str) and (kind, key) in NESTED_FULL:
            if isinstance(held, list):
                out[key] = [_full(i) if isinstance(i, dict) else i for i in held]
            else:
                out[key] = _full(held) if isinstance(held, dict) else held
            continue
        out[key] = _compact(held)
    return out


def shape(item: Representation, tree: FieldTree | None, *, full: bool) -> JsonValue:
    """One resource as Asana answers it: narrowed by opt_fields, else full (one resource) or compact (a list).

    A full record leaves out what is [Opt In] and holds the resources it names compact, but where Asana's document
    gives them full (`NESTED_FULL`).
    """
    value: JsonValue = item.model_dump(mode="json")
    if tree is not None:
        return _narrow(value, tree, "")
    if not full or not isinstance(value, dict):
        return _compact(value)
    return _full(value)


class NextPage(Model):
    offset: str
    path: str
    uri: str


def encode_offset(after: str) -> str:
    return base64.urlsafe_b64encode(f"after:{after}".encode()).decode().rstrip("=")


BAD_OFFSET = "offset: Your pagination token is invalid."
"""Reported of the real service, 400: https://forum.asana.com/t/538741."""


def decode_offset(token: str) -> str:
    try:
        text = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError) as error:
        raise bad(BAD_OFFSET) from error
    head, _, position = text.partition(":")
    if head != "after" or not is_gid(position):
        raise bad(BAD_OFFSET)
    return position


Item = TypeVar("Item", bound=Representation)


def page(
    items: list[Item], query: Query, path: str, *, unpaginated_limit: int = UNPAGINATED_MAX
) -> tuple[list[Item], NextPage | None]:
    """One page by gid, which is the order every collection here is answered in.

    Without `limit` the whole collection is answered, until it is larger than `unpaginated_limit` (the workspace's
    own threshold), when it is not answered at all.
    """
    limit = query.count("limit")
    offset = query.text("offset")
    if offset is not None and limit is None:
        raise undocumented("an offset without a limit")
    if limit is None:
        if len(items) > unpaginated_limit:
            raise bad("The result is too large. You should use pagination (may require specifying a workspace)!")
        return items, None
    rest = items if offset is None else [i for i in items if int(i.gid) > int(decode_offset(offset))]
    chosen = rest[:limit]
    if len(rest) <= limit:
        return chosen, None
    token = encode_offset(chosen[-1].gid)
    kept = [(n, query.text(n) or "") for n in query.names() if n != "offset"]
    query_string = "&".join(f"{n}={v}" for n, v in [*kept, ("offset", token)])
    return chosen, NextPage(offset=token, path=f"{path}?{query_string}", uri=f"{API_BASE}{path}?{query_string}")


def _event_ref(ref: EventRef, *, named: bool) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = {"gid": ref.gid, "resource_type": ref.resource_type}
    if ref.resource_subtype is not None:
        out["resource_subtype"] = ref.resource_subtype
    if named and ref.name is not None:
        out["name"] = ref.name
    return out


def event_json(event: AsanaEvent, names: Mapping[str, str], *, named: bool) -> dict[str, JsonValue]:
    """An event as Asana reports it: `user` (null for none), `created_at`, `type` (deprecated: the resource's type),
    `action`, `resource`, `parent` (null unless added or removed), and `change` when changed. `named` adds the names
    the compact records `GET /events` answers carry; a webhook's events hold the gid and type only
    (`EventResponse.change.new_value` in the OpenAPI subset)."""
    out: dict[str, JsonValue] = {
        "user": {"gid": event.user, "resource_type": "user"} if event.user is not None else None,
        "created_at": event.created_at,
        "type": event.resource.resource_type,
        "action": event.action.value,
        "resource": _event_ref(event.resource, named=named),
        "parent": _event_ref(event.parent, named=named) if event.parent is not None else None,
    }
    if event.user is not None and named and event.user in names:
        out["user"] = {"gid": event.user, "resource_type": "user", "name": names[event.user]}
    if event.change is not None:
        change: dict[str, JsonValue] = {"field": event.change.field, "action": event.change.action.value}
        for key, value in (
            ("new_value", event.change.new_value),
            ("added_value", event.change.added_value),
            ("removed_value", event.change.removed_value),
        ):
            if value is not None:
                change[key] = _event_ref(value, named=named)
        out["change"] = change
    return out


def events_page(events: Sequence[dict[str, JsonValue]], tree: FieldTree | None, *, sync: str, more: bool) -> bytes:
    """`GET /events`: `data`, the `sync` token for the next call and `has_more`; `opt_fields` narrows each event to
    the properties it names."""
    shown: list[JsonValue] = []
    for event in events:
        if tree is None:
            shown.append(event)
            continue
        padded: dict[str, JsonValue] = {"change": None, **event}
        narrowed = _narrow(padded, tree, "")
        if isinstance(narrowed, dict) and "change" in narrowed and narrowed["change"] is None:
            del narrowed["change"]
        shown.append(narrowed)
    return json.dumps({"data": shown, "sync": sync, "has_more": more}).encode()


def sync_failed(message: str, sync: str) -> bytes:
    """The 412 for a request with no sync token or an expired one: the errors, and the token to start from
    (`getEvents` 412 in the OpenAPI subset)."""
    return json.dumps({"errors": [{"message": message, "help": ERROR_HELP}], "sync": sync}).encode()


SYNC_REQUIRED = (
    "Sync token invalid or too old. If you are attempting to keep resources in sync, you must fetch the full dataset "
    "for this query now and use the new sync token for the next sync."
)
"""The message `getEvents`' 412 gives as its example (OpenAPI subset); it is also the answer to a request with no token,
which "returns a `412 Precondition Failed` error containing the sync token" (https://developers.asana.com/docs/events)."""


def one(item: Representation, tree: FieldTree | None) -> bytes:
    return json.dumps({"data": shape(item, tree, full=True)}).encode()


def many(items: Sequence[Representation], tree: FieldTree | None, next_page: NextPage | None) -> bytes:
    return json.dumps(
        {
            "data": [shape(i, tree, full=False) for i in items],
            "next_page": next_page.model_dump(mode="json") if next_page is not None else None,
        }
    ).encode()


def unpaged(items: Sequence[Representation], tree: FieldTree | None) -> bytes:
    """Search and typeahead: at most `limit` results and no `next_page`."""
    return json.dumps({"data": [shape(i, tree, full=False) for i in items]}).encode()


def empty() -> bytes:
    return json.dumps({"data": {}}).encode()


def failed(message: str) -> bytes:
    return json.dumps({"errors": [{"message": message, "help": ERROR_HELP}]}).encode()
