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
import html
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeVar
from urllib.parse import parse_qsl

from pydantic import Field, JsonValue, SerializerFunctionWrapHandler, TypeAdapter, model_serializer

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
RATE_LIMITED = "You have made too many requests recently. Please, be chill."

_GID = re.compile(r"^[0-9]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TAG = re.compile(r"<[^>]+>")


class Refusal(Exception):
    """Asana answered with an error. `status` and `message` are Asana's own."""

    def __init__(self, status: int, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


def bad(message: str) -> Refusal:
    return Refusal(400, message)


def unknown(resource: str, gid: str, *, status: int) -> Refusal:
    return Refusal(status, f"{resource}: Unknown object: {gid}")


def forbidden() -> Refusal:
    """An object that exists and the caller may not see."""
    return Refusal(403, "Forbidden")


def premium(sentence: str) -> Refusal:
    """A feature the workspace's plan does not include, said as a whole sentence."""
    return Refusal(402, sentence)


SEARCH_IS_PREMIUM = "Search is only available to premium Asana workspaces."
FIELDS_ARE_PREMIUM = "Custom fields are only available to premium Asana workspaces."


def unsupported(name: str) -> Refusal:
    """Something real Asana has and this provider does not. Never ignored in silence."""
    return Refusal(501, f"{name}: Not supported by this simulation of Asana")


def is_gid(value: str) -> bool:
    """Asana gids are strings of digits; anything else names no object."""
    return bool(_GID.match(value))


def gid_in_path(resource: str, value: str) -> str:
    if not is_gid(value):
        raise bad(f"{resource}: Not a Recognized ID")
    return value


def is_user_identifier(value: str) -> bool:
    """Where Asana names a user it takes a gid, an email address, or the literal `me`."""
    return is_gid(value) or value == "me" or "@" in value


def stamp(moment: datetime) -> str:
    """Asana's timestamp: UTC, milliseconds, `Z`."""
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
    strict_tokens: bool = Field(default=False, description="Only a seeded or minted token is accepted")
    status: StatusRule = SectionStatus()
    rate_limits: list[RateWindow] = []


class AsanaUser(Model):
    resource_type: Literal["user"] = "user"
    gid: str
    name: str
    email: str


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
    color: str | None = None
    workspace: str
    created_at: str


class AsanaCredential(Model):
    """A token the workspace accepts. The gid is derived from the token itself, which is never stored."""

    resource_type: Literal["credential"] = "credential"
    gid: str
    kind: CredentialKind
    user: str
    expires_at: str | None = None


class AsanaMembership(Model):
    project: str
    section: str


class AsanaFieldValue(Model):
    """One custom field's value on a task, read by the field's kind: only the member for that kind is set."""

    field: str
    option: str | None = None
    options: list[str] = []
    text: str | None = None
    number: float | None = None
    date: str | None = None
    date_time: str | None = None
    people: list[str] = []


class AsanaTask(Model):
    resource_type: Literal["task"] = "task"
    gid: str
    name: str = ""
    notes: str = ""
    completed: bool = False
    completed_at: str | None = None
    due_on: str | None = None
    due_at: str | None = None
    assignee: str | None = Field(default=None, description="User gid")
    created_by: str
    workspace: str
    parent: str | None = Field(default=None, description="Task gid")
    memberships: list[AsanaMembership] = []
    tags: list[str] = []
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


AnyRecord = (
    AsanaWorkspace | AsanaUser | AsanaTeam | AsanaProject | AsanaSection | AsanaCustomField | AsanaTag | AsanaCredential
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
    "followers",
    "start_on",
    "start_at",
    "assignee_section",
    "resource_subtype",
    "approval_status",
    "external",
    "liked",
)


def envelope(body: bytes) -> Fields:
    """The object inside `{"data": ...}`; Asana refuses a write that is not wrapped."""
    try:
        decoded = json.loads(body) if body else None
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise bad("Could not parse request data, invalid JSON") from error
    if not isinstance(decoded, dict) or "data" not in decoded or not isinstance(decoded["data"], dict):
        raise bad("Missing input: data")
    return decoded["data"]


def _string(fields: Fields, name: str) -> str | None:
    if name not in fields or fields[name] is None:
        return None
    value = fields[name]
    if not isinstance(value, str):
        raise bad(f"{name}: Not a string")
    return value


def _required(fields: Fields, name: str) -> str:
    value = _string(fields, name)
    if value is None or not value.strip():
        raise bad(f"{name}: Missing input")
    return value


def _gid_field(fields: Fields, name: str) -> str | None:
    value = _string(fields, name)
    if value is not None and not is_gid(value):
        raise bad(f"{name}: Not a Recognized ID")
    return value


def _gids(fields: Fields, name: str) -> list[str]:
    if name not in fields or fields[name] is None:
        return []
    raw = fields[name]
    if not isinstance(raw, list):
        raise bad(f"{name}: Not an array")
    found: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not is_gid(item):
            raise bad(f"{name}: Not a Recognized ID")
        found.append(item)
    return found


def _boolean(fields: Fields, name: str) -> bool:
    value = fields[name]
    if not isinstance(value, bool):
        raise bad(f"{name}: Not a boolean")
    return value


def _due(fields: Fields) -> tuple[str | None, str | None]:
    due_on = _string(fields, "due_on")
    due_at = _string(fields, "due_at")
    if due_on is not None and due_at is not None:
        raise bad("Cannot specify both due_on and due_at")
    if due_on is not None and not is_date(due_on):
        raise bad("due_on: Invalid date")
    if due_at is not None:
        parsed = parse_stamp(due_at)
        if parsed is None:
            raise bad("due_at: Invalid datetime")
        due_at = stamp(parsed)
    return due_on, due_at


def _assignee(fields: Fields) -> str | None:
    value = fields["assignee"]
    if value is None:
        return None
    if not isinstance(value, str) or not is_user_identifier(value):
        raise bad("assignee: Not a Recognized ID")
    return value


def _notes(fields: Fields) -> str | None:
    """`notes`, or `html_notes` as the text it shows; Asana refuses both at once. None when neither was sent."""
    if "notes" in fields and "html_notes" in fields:
        raise bad("Cannot specify both notes and html_notes")
    if "html_notes" in fields:
        marked = _string(fields, "html_notes") or ""
        if "<body>" not in marked:
            raise bad("html_notes: Invalid HTML: must be enclosed in <body> tags")
        return html.unescape(plain(marked))
    if "notes" in fields:
        return _string(fields, "notes") or ""
    return None


def _custom_fields(fields: Fields) -> dict[str, JsonValue]:
    """`{field gid: value}` as sent; each value is read against its field's definition by the caller."""
    raw = fields["custom_fields"]
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise bad("custom_fields: Not an object")
    for gid in raw:
        if not is_gid(gid):
            raise bad("custom_fields: Not a Recognized ID")
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
        raise bad("memberships: Not an array")
    found: list[MembershipIn] = []
    for item in raw:
        if not isinstance(item, dict):
            raise bad("memberships: Not an object")
        project = _gid_field(item, "project")
        if project is None:
            raise bad("memberships.project: Missing input")
        found.append(MembershipIn(project=project, section=_gid_field(item, "section")))
    return found


class TaskCreate(Model):
    name: str = ""
    notes: str = ""
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
        raise bad("Cannot specify both projects and memberships")
    workspace = _gid_field(fields, "workspace")
    under = parent if parent is not None else _gid_field(fields, "parent")
    if not projects and not memberships and workspace is None and under is None:
        raise bad("Missing input: workspace")
    return TaskCreate(
        name=_string(fields, "name") or "",
        notes=_notes(fields) or "",
        completed=_boolean(fields, "completed") if "completed" in fields else False,
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
    due_on: str | None = None
    due_at: str | None = None
    assignee: str | None = None
    custom_fields: dict[str, JsonValue] = {}


def task_update(fields: Fields) -> TaskUpdate:
    _refuse_unsupported(fields, _TASK_UNSUPPORTED)
    for fixed in ("projects", "workspace", "memberships", "tags", "parent"):
        if fixed in fields:
            raise bad(f"{fixed}: Cannot write this property")
    sent: dict[str, JsonValue] = {}
    if "name" in fields:
        sent["name"] = _string(fields, "name") or ""
    notes = _notes(fields)
    if notes is not None:
        sent["notes"] = notes
    if "completed" in fields:
        sent["completed"] = _boolean(fields, "completed")
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
        raise bad("members: Missing input")
    raw = fields["members"]
    named: list[str] = []
    if isinstance(raw, str):
        named = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, str):
                raise bad("members: Not a Recognized ID")
            named.append(item.strip())
    else:
        raise bad("members: Not an array")
    if not named:
        raise bad("members: Missing input")
    for identifier in named:
        if not is_user_identifier(identifier):
            raise bad("members: Not a Recognized ID")
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
    if text is None or not text.strip():
        raise bad("Missing input: text")
    return StoryCreate(text=text)


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
                raise bad(f"{where}: Not a Recognized ID")
            return value.model_copy(update={"option": _enum_option(definition, sent, where)})
        case "multi_enum":
            if not isinstance(sent, list):
                raise bad(f"{where}: Not an array")
            chosen: list[str] = []
            for item in sent:
                if not isinstance(item, str) or not is_gid(item):
                    raise bad(f"{where}: Not a Recognized ID")
                chosen.append(_enum_option(definition, item, where))
            return value.model_copy(update={"options": list(dict.fromkeys(chosen))})
        case "text":
            if not isinstance(sent, str):
                raise bad(f"{where}: Not a string")
            return value.model_copy(update={"text": sent})
        case "number":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
            if isinstance(sent, bool) or not isinstance(sent, int | float):
                raise bad(f"{where}: Not a number")
            return value.model_copy(update={"number": float(sent)})
        case "date":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
            return _date_value(value, sent, where)
        case "people":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
            if not isinstance(sent, list):
                raise bad(f"{where}: Not an array")
            people: list[str] = []
            for item in sent:
                if not isinstance(item, str) or not is_user_identifier(item):
                    raise bad(f"{where}: Not a Recognized ID")
                if item not in users:
                    raise bad(f"{where}: Unknown object: {item}")
                people.append(users[item])
            return value.model_copy(update={"people": list(dict.fromkeys(people))})


def _date_value(value: AsanaFieldValue, sent: JsonValue, where: str) -> AsanaFieldValue:
    """`{"date": "YYYY-MM-DD"}` or `{"date_time": "..."}`; a moment also answers its UTC date."""
    if not isinstance(sent, dict):
        raise bad(f"{where}: Not an object")
    at = sent["date_time"] if "date_time" in sent else None
    if at is not None:
        moment = parse_stamp(at) if isinstance(at, str) else None
        if moment is None:
            raise bad(f"{where}.date_time: Invalid datetime")
        return value.model_copy(update={"date": moment.astimezone(UTC).date().isoformat(), "date_time": stamp(moment)})
    on = sent["date"] if "date" in sent else None
    if on is None:
        return value
    if not isinstance(on, str) or not is_date(on):
        raise bad(f"{where}.date: Invalid date")
    return value.model_copy(update={"date": on})


def _enum_option(definition: AsanaCustomField, gid: str, where: str) -> str:
    option = next((o for o in definition.enum_options if o.gid == gid), None)
    if option is None:
        raise bad(f"{where}: Not a recognized enum option: {gid}")
    if not option.enabled:
        raise bad(f"{where}: Enum option {gid} is disabled")
    return gid


class OAuthRefusal(Exception):
    """The OAuth endpoint's own error shape (RFC 6749), not the API's envelope."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


class TokenGrant(Model):
    """A form-encoded request to `/-/oauth_token`."""

    grant_type: str
    refresh_token: str | None = None


def token_grant(body: bytes) -> TokenGrant:
    pairs = dict(parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True))
    if "grant_type" not in pairs:
        raise OAuthRefusal("invalid_request", "The grant_type parameter is missing.")
    return TokenGrant(
        grant_type=pairs["grant_type"], refresh_token=pairs["refresh_token"] if "refresh_token" in pairs else None
    )


def oauth_failed(refusal: OAuthRefusal) -> bytes:
    return json.dumps({"error": refusal.error, "error_description": refusal.description}).encode()


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


def html_notes(notes: str) -> str:
    return "<body>" + html.escape(notes, quote=False) + "</body>"


def plain(text: str) -> str:
    return _TAG.sub("", text)


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
            raise bad(f"{name}: Not a boolean")
        return spelled == "true"

    def count(self, name: str) -> int | None:
        value = self.text(name)
        if value is None:
            return None
        if not value.isdigit() or not 1 <= int(value) <= PAGE_MAX:
            raise bad(f"{name}: Must be between 1 and {PAGE_MAX}")
        return int(value)

    def moment(self, name: str) -> datetime | None:
        value = self.text(name)
        if value is None:
            return None
        parsed = parse_stamp(value)
        if parsed is None:
            raise bad(f"{name}: Invalid datetime")
        return parsed

    def gids(self, name: str) -> list[str] | None:
        """A comma-separated list of gids, as the search filters take them."""
        value = self.text(name)
        if value is None:
            return None
        found = [part.strip() for part in value.split(",") if part.strip()]
        for gid in found:
            if not is_gid(gid):
                raise bad(f"{name}: Not a Recognized ID")
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
    has_notifications_enabled: bool = False
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
    number_value: float | None = None
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
    write_access: Literal["full_write"] = "full_write"
    access_level: Literal["editor"] = "editor"


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
    """A task named by another: its parent or one of its subtasks."""

    gid: str
    resource_type: Literal["task"] = "task"
    resource_subtype: Literal["default_task"] = "default_task"
    name: str
    completed: bool
    permalink_url: str


class TaskOut(Model):
    """A nested resource is held in full so `opt_fields` can reach into it; it is answered compact otherwise."""

    gid: str
    resource_type: Literal["task"] = "task"
    resource_subtype: Literal["default_task"] = "default_task"
    name: str
    notes: str
    html_notes: str
    completed: bool
    completed_at: str | None
    due_on: str | None
    due_at: str | None
    created_at: str
    modified_at: str
    assignee: UserOut | None
    created_by: UserOut
    parent: TaskRefOut | None
    subtasks: list[TaskRefOut]
    num_subtasks: int
    memberships: list[MembershipOut]
    projects: list[ProjectOut]
    tags: list[TagOut]
    custom_fields: list[CustomFieldValueOut]
    workspace: WorkspaceOut
    permalink_url: str


class StoryOut(Model):
    gid: str
    resource_type: Literal["story"] = "story"
    resource_subtype: Literal["comment_added"] = "comment_added"
    type: Literal["comment"] = "comment"
    text: str
    html_text: str
    created_at: str
    created_by: UserOut
    target: Compact


Representation = (
    TaskOut
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
    "enum_option": ("gid", "resource_type", "name", "enabled", "color"),
    "custom_field": (
        "gid",
        "resource_type",
        "name",
        "resource_subtype",
        "type",
        "enum_options",
        "enum_value",
        "multi_enum_values",
        "number_value",
        "text_value",
        "date_value",
        "people_value",
        "display_value",
        "enabled",
        "is_formula_field",
        "precision",
    ),
    "custom_field_setting": ("gid", "resource_type"),
    "project_membership": ("gid", "resource_type", "user", "parent", "access_level"),
}

FieldTree = dict[str, "FieldTree"]


def field_tree(query: Query) -> FieldTree | None:
    """`opt_fields=name,assignee.email` as a nested selection; None when the caller named none."""
    raw = query.text("opt_fields")
    if raw is None:
        return None
    tree: FieldTree = {}
    for path in raw.split(","):
        node = tree
        for part in path.strip().split("."):
            if part:
                node = node.setdefault(part, {})
    return tree


def _reference(value: JsonValue) -> JsonValue:
    """A field named with nothing under it: a nested object answers its gid and resource_type.

    A custom field is the exception: `custom_fields` named bare answers each field with the value it holds,
    because callers read a task's priority or status that way and name nothing under it.
    """
    if isinstance(value, list):
        return [_reference(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("resource_type") == "custom_field":
        return _compact(value)
    if "gid" in value:
        return {k: value[k] for k in ("gid", "resource_type") if k in value}
    return {k: _reference(v) for k, v in value.items()}


def _narrow(value: JsonValue, tree: FieldTree) -> JsonValue:
    if not tree:
        return _reference(value)
    if isinstance(value, list):
        return [_narrow(item, tree) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, JsonValue] = {"gid": value["gid"]} if "gid" in value else {}
    for key, sub in tree.items():
        if key in value:
            out[key] = _narrow(value[key], sub)
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


def shape(item: Representation, tree: FieldTree | None, *, full: bool) -> JsonValue:
    """One resource as Asana answers it: narrowed by opt_fields, else full (one resource) or compact (a list).

    A full record holds the resources it names compact.
    """
    value: JsonValue = item.model_dump(mode="json")
    if tree is not None:
        return _narrow(value, tree)
    if not full or not isinstance(value, dict):
        return _compact(value)
    return {k: _compact(v) for k, v in value.items()}


class NextPage(Model):
    offset: str
    path: str
    uri: str


def encode_offset(after: str) -> str:
    return base64.urlsafe_b64encode(f"after:{after}".encode()).decode().rstrip("=")


def decode_offset(token: str) -> str:
    try:
        text = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError) as error:
        raise bad("offset: Invalid offset token") from error
    head, _, position = text.partition(":")
    if head != "after" or not is_gid(position):
        raise bad("offset: Invalid offset token")
    return position


Item = TypeVar("Item", bound=Representation)


def page(items: list[Item], query: Query, path: str) -> tuple[list[Item], NextPage | None]:
    """One page by gid, which is the order every collection here is answered in.

    Without `limit` the whole collection is answered, until it is too large to answer at all.
    """
    limit = query.count("limit")
    offset = query.text("offset")
    if offset is not None and limit is None:
        raise bad("offset: Cannot be used without limit")
    if limit is None:
        if len(items) > UNPAGINATED_MAX:
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
