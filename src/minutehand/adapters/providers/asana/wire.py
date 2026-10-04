"""Asana's own JSON: the only module that parses or builds it.

Three families of model live here:

- **Stored** — `AsanaWorkspace`, `AsanaUser`, `AsanaProject`, `AsanaSection`,
  `AsanaTask`, `AsanaStory`: the body of each entity in the store. Each carries
  Asana's `resource_type`, so a record read back is parsed by what it says it is.
- **Writes** — the `{"data": {...}}` envelope of a POST or PUT, read into
  `TaskCreate`, `TaskUpdate` and `StoryCreate`, refused field by field with
  Asana's own messages.
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
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, TypeVar

from pydantic import Field, JsonValue, TypeAdapter

from minutehand.domain.scenario import Model

API_BASE = "https://app.asana.com/api/1.0"
ERROR_HELP = (
    "For more information on API status codes and how to handle them, read "
    "the docs on errors: https://developers.asana.com/docs/errors"
)
PAGE_MAX = 100
UNPAGINATED_MAX = 1000
SEARCH_DEFAULT = 20
TYPEAHEAD_DEFAULT = 20

_GID = re.compile(r"^[0-9]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TAG = re.compile(r"<[^>]+>")


class Refusal(Exception):
    """Asana answered with an error. `status` and `message` are Asana's own."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def bad(message: str) -> Refusal:
    return Refusal(400, message)


def unknown(resource: str, gid: str, *, status: int) -> Refusal:
    return Refusal(status, f"{resource}: Unknown object: {gid}")


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
    at = moment.astimezone(timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def parse_stamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# --------------------------------------------------------------------------- stored


class SectionRole(StrEnum):
    """What a seeded section means for the state of a task in it."""

    TODO = "todo"
    DONE = "done"
    CANCELLED = "cancelled"


class AsanaWorkspace(Model):
    resource_type: Literal["workspace"] = "workspace"
    gid: str
    name: str
    is_organization: bool = True
    email_domains: list[str] = []


class AsanaUser(Model):
    resource_type: Literal["user"] = "user"
    gid: str
    name: str
    email: str


class AsanaProject(Model):
    resource_type: Literal["project"] = "project"
    gid: str
    name: str
    notes: str = ""
    archived: bool = False
    workspace: str
    created_at: str


class AsanaSection(Model):
    resource_type: Literal["section"] = "section"
    gid: str
    name: str
    project: str
    role: SectionRole
    created_at: str


class AsanaMembership(Model):
    project: str
    section: str


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
    memberships: list[AsanaMembership] = []
    created_at: str
    modified_at: str


class AsanaStory(Model):
    resource_type: Literal["story"] = "story"
    gid: str
    text: str
    task: str
    created_by: str
    created_at: str


Record = Annotated[AsanaWorkspace | AsanaUser | AsanaProject | AsanaSection, Field(discriminator="resource_type")]
_RECORD: TypeAdapter[AsanaWorkspace | AsanaUser | AsanaProject | AsanaSection] = TypeAdapter(Record)

StoredModel = TypeVar("StoredModel", AsanaTask, AsanaStory)


def parse_record(body: str) -> AsanaWorkspace | AsanaUser | AsanaProject | AsanaSection:
    return _RECORD.validate_json(body)


def parse(model: type[StoredModel], body: str) -> StoredModel:
    return model.model_validate_json(body)


def dump(entity: Model) -> str:
    return entity.model_dump_json()


# --------------------------------------------------------------------------- writes


Fields = dict[str, JsonValue]

_TASK_UNSUPPORTED = (
    "html_notes", "parent", "custom_fields", "tags", "followers", "memberships", "start_on", "start_at",
    "assignee_section", "resource_subtype", "approval_status", "external", "liked",
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


def _due(fields: Fields) -> tuple[str | None, str | None]:
    due_on = _string(fields, "due_on")
    due_at = _string(fields, "due_at")
    if due_on is not None and due_at is not None:
        raise bad("Cannot specify both due_on and due_at")
    if due_on is not None:
        if not _DATE.match(due_on):
            raise bad("due_on: Invalid date")
        try:
            date.fromisoformat(due_on)
        except ValueError as error:
            raise bad("due_on: Invalid date") from error
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


def _completed(fields: Fields) -> bool:
    value = fields["completed"]
    if not isinstance(value, bool):
        raise bad("completed: Not a boolean")
    return value


def _refuse_unsupported(fields: Fields) -> None:
    for name in _TASK_UNSUPPORTED:
        if name in fields:
            raise unsupported(name)


class TaskCreate(Model):
    name: str = ""
    notes: str = ""
    completed: bool = False
    due_on: str | None = None
    due_at: str | None = None
    assignee: str | None = Field(default=None, description="As the caller named the user: gid, email or `me`")
    projects: list[str] = []
    workspace: str | None = None


def task_create(fields: Fields) -> TaskCreate:
    _refuse_unsupported(fields)
    due_on, due_at = _due(fields)
    projects: list[str] = []
    if "projects" in fields and fields["projects"] is not None:
        raw = fields["projects"]
        if not isinstance(raw, list):
            raise bad("projects: Not an array")
        for item in raw:
            if not isinstance(item, str) or not is_gid(item):
                raise bad("projects: Not a Recognized ID")
            projects.append(item)
    workspace = _string(fields, "workspace")
    if workspace is not None and not is_gid(workspace):
        raise bad("workspace: Not a Recognized ID")
    if not projects and workspace is None:
        raise bad("Missing input: workspace")
    return TaskCreate(
        name=_string(fields, "name") or "", notes=_string(fields, "notes") or "",
        completed=_completed(fields) if "completed" in fields else False,
        due_on=due_on, due_at=due_at, assignee=_assignee(fields) if "assignee" in fields else None,
        projects=projects, workspace=workspace,
    )


class TaskUpdate(Model):
    """Only the fields the caller sent are set; `model_fields_set` says which."""

    name: str = ""
    notes: str = ""
    completed: bool = False
    due_on: str | None = None
    due_at: str | None = None
    assignee: str | None = None


def task_update(fields: Fields) -> TaskUpdate:
    _refuse_unsupported(fields)
    for fixed in ("projects", "workspace"):
        if fixed in fields:
            raise bad(f"{fixed}: Cannot write this property")
    sent: dict[str, str | bool | None] = {}
    for name in ("name", "notes"):
        if name in fields:
            sent[name] = _string(fields, name) or ""
    if "completed" in fields:
        sent["completed"] = _completed(fields)
    due_on, due_at = _due(fields)
    if "due_on" in fields:
        sent["due_on"] = due_on
    if "due_at" in fields:
        sent["due_at"] = due_at
    if "assignee" in fields:
        sent["assignee"] = _assignee(fields)
    return TaskUpdate.model_validate(sent)


class StoryCreate(Model):
    text: str


def story_create(fields: Fields) -> StoryCreate:
    for name in ("html_text", "is_pinned", "sticker_name"):
        if name in fields:
            raise unsupported(name)
    text = _string(fields, "text")
    if text is None or not text.strip():
        raise bad("Missing input: text")
    return StoryCreate(text=text)


def html_notes(notes: str) -> str:
    return "<body>" + html.escape(notes, quote=False) + "</body>"


def plain(text: str) -> str:
    return _TAG.sub("", text)


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
        if value not in ("true", "false"):
            raise bad(f"{name}: Not a boolean")
        return value == "true"

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


class ProjectOut(Model):
    gid: str
    resource_type: Literal["project"] = "project"
    name: str
    notes: str
    archived: bool
    created_at: str
    workspace: WorkspaceOut
    permalink_url: str


class SectionOut(Model):
    gid: str
    resource_type: Literal["section"] = "section"
    name: str
    created_at: str
    project: ProjectOut


class MembershipOut(Model):
    project: ProjectOut
    section: SectionOut


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
    memberships: list[MembershipOut]
    projects: list[ProjectOut]
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


Representation = TaskOut | UserOut | WorkspaceOut | ProjectOut | SectionOut | StoryOut

COMPACT: Mapping[str, tuple[str, ...]] = {
    "task": ("gid", "resource_type", "name", "resource_subtype"),
    "user": ("gid", "resource_type", "name"),
    "workspace": ("gid", "resource_type", "name"),
    "project": ("gid", "resource_type", "name"),
    "section": ("gid", "resource_type", "name"),
    "story": ("gid", "resource_type", "created_at", "created_by", "resource_subtype", "text"),
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
    """A field named with nothing under it: a nested object answers its gid and resource_type."""
    if isinstance(value, list):
        return [_reference(item) for item in value]
    if not isinstance(value, dict):
        return value
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
    return json.dumps({
        "data": [shape(i, tree, full=False) for i in items],
        "next_page": next_page.model_dump(mode="json") if next_page is not None else None,
    }).encode()


def unpaged(items: Sequence[Representation], tree: FieldTree | None) -> bytes:
    """Search and typeahead: at most `limit` results and no `next_page`."""
    return json.dumps({"data": [shape(i, tree, full=False) for i in items]}).encode()


def empty() -> bytes:
    return json.dumps({"data": {}}).encode()


def failed(message: str) -> bytes:
    return json.dumps({"errors": [{"message": message, "help": ERROR_HELP}]}).encode()
