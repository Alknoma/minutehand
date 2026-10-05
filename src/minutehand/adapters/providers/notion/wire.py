"""Notion's own JSON, version 2022-06-28: the only module that parses or builds it.

Four families live here:

- **Errors** — `Refusal`, raised anywhere and answered as Notion's error object
  (`object`, `status`, `code`, `message`, `request_id`).
- **Stored** — what each entity's body holds in the store: `StoredPage` (a page or a
  database row, with its whole block tree inside it), `StoredDatabase`, `StoredUser`,
  `StoredIntegration`, `StoredToken`, `StoredComment`, and the provider's own schedule.
  Every one carries a `kind` literal, so a body read back is told apart by its type.
- **Requests** — rich text, blocks and property values as a caller writes them, checked
  and normalised into their stored form; anything Notion refuses raises a `Refusal`.
- **Responses** — every object rendered the way the API serves it.

Block and property bodies are Notion's own open shapes, so they are kept as JSON values
here and nowhere else.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from minutehand.domain.errors import Asked, Rendered, ServiceRefusal
from minutehand.domain.scenario import Model

API_VERSION = "2022-06-28"
MAX_PAGE_SIZE = 100
MAX_CHILDREN = 100
MAX_NESTING = 2
"""Levels of children below a block in one request: a block, its children and theirs."""
MAX_TEXT = 2000
MAX_RICH_TEXT_ITEMS = 100
MAX_EQUATION = 1000

Json = dict[str, JsonValue]
_OBJECT: TypeAdapter[Json] = TypeAdapter(Json)


# --------------------------------------------------------------------------- errors


class ErrorCode(StrEnum):
    UNAUTHORIZED = "unauthorized"
    RESTRICTED_RESOURCE = "restricted_resource"
    OBJECT_NOT_FOUND = "object_not_found"
    RATE_LIMITED = "rate_limited"
    INVALID_JSON = "invalid_json"
    INVALID_REQUEST_URL = "invalid_request_url"
    INVALID_REQUEST = "invalid_request"
    VALIDATION_ERROR = "validation_error"
    MISSING_VERSION = "missing_version"
    CONFLICT_ERROR = "conflict_error"
    INTERNAL_SERVER_ERROR = "internal_server_error"


_STATUS = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.RESTRICTED_RESOURCE: 403,
    ErrorCode.OBJECT_NOT_FOUND: 404,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INVALID_JSON: 400,
    ErrorCode.INVALID_REQUEST_URL: 400,
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.MISSING_VERSION: 400,
    ErrorCode.CONFLICT_ERROR: 409,
    ErrorCode.INTERNAL_SERVER_ERROR: 500,
}

ERROR_TYPE = "application/json; charset=utf-8"
"""The content type Notion answers with, errors included."""


class Refusal(ServiceRefusal):
    """Notion answered with an error object."""

    def __init__(self, code: ErrorCode, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.headers = headers or {}

    @property
    def status(self) -> int:
        return _STATUS[self.code]

    def body(self, request_id: str) -> str:
        return json.dumps(
            {
                "object": "error",
                "status": self.status,
                "code": self.code.value,
                "message": self.message,
                "request_id": request_id,
            }
        )

    def render(self, asked: Asked) -> Rendered:
        """The error object, its request id minted from the call: the app's own gate mints it from the store's head
        as well, which a refusal answered here has no way to read."""
        minted = request_id(f"{asked.now.isoformat()}:{asked.method}:{asked.path}")
        return Rendered(
            status=self.status,
            content_type=ERROR_TYPE,
            body=self.body(minted).encode(),
            headers=[(name.lower(), value) for name, value in self.headers.items()],
        )


def error_answer(status: int, message: str, minted: str) -> Rendered:
    """What Minutehand answers in Notion's place (501, 500), as Notion's error object with a code `notion-client`
    raises `APIResponseError` for: `invalid_request` ("this request is not supported") for an operation the fake
    does not implement, `internal_server_error` for anything else."""
    code = ErrorCode.INVALID_REQUEST if status == 501 else ErrorCode.INTERNAL_SERVER_ERROR
    body = {"object": "error", "status": status, "code": code.value, "message": message, "request_id": minted}
    return Rendered(status=status, content_type=ERROR_TYPE, body=json.dumps(body).encode())


def invalid(message: str) -> Refusal:
    return Refusal(ErrorCode.VALIDATION_ERROR, message)


class Missing:
    """What an `object_not_found` names, in its message: words in a sentence, not a vocabulary anyone matches."""

    PAGE = "page"
    BLOCK = "block"
    DATABASE = "database"
    USER = "user"
    COMMENT = "comment"


def not_found(what: str, object_id: str) -> Refusal:
    return Refusal(
        ErrorCode.OBJECT_NOT_FOUND,
        f"No {what} with ID {object_id} can be reached by this integration. Only pages and databases "
        "shared with the integration, and what is inside them, can be reached.",
    )


def unauthorized() -> Refusal:
    return Refusal(ErrorCode.UNAUTHORIZED, "The API token presented is not valid.")


def restricted(capability: str) -> Refusal:
    return Refusal(
        ErrorCode.RESTRICTED_RESOURCE,
        f"This integration lacks the capability this endpoint needs ({capability}).",
    )


def rate_limited(retry_after: int) -> Refusal:
    return Refusal(
        ErrorCode.RATE_LIMITED,
        "This integration has sent too many requests. Wait before trying again.",
        headers={"Retry-After": str(retry_after)},
    )


def conflict() -> Refusal:
    return Refusal(ErrorCode.CONFLICT_ERROR, "Another change to the same content was saved first. Try again.")


def archived(what: str) -> Refusal:
    return invalid(f"This {what} is archived and cannot be edited. Restore it first.")


# --------------------------------------------------------------------------- ids and time


def canonical_id(raw: str, where: str) -> str:
    """A Notion id in its dashed form; a caller may send it with or without dashes."""
    try:
        return str(uuid.UUID(hex=raw.replace("-", "")))
    except ValueError as error:
        raise invalid(f"{where} should be a valid UUID; got `{raw}`.") from error


def minted_id(*parts: str) -> str:
    """An id derived from what made it, so a rerun of the same calls mints the same ids."""
    return str(uuid.UUID(bytes=hashlib.sha256("\x1f".join(parts).encode()).digest()[:16], version=4))


def stamp(moment: datetime) -> str:
    """A Notion timestamp: UTC, rounded down to the minute, as the API serves page and block times."""
    at = moment.astimezone(UTC).replace(second=0, microsecond=0)
    return at.strftime("%Y-%m-%dT%H:%M:00.000Z")


def moment(stamped: str) -> datetime:
    return datetime.fromisoformat(stamped.replace("Z", "+00:00"))


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def request_id(seed: str) -> str:
    return minted_id("request", seed)


def page_url(page_id: str, title: str) -> str:
    slug = "-".join("".join(c if c.isalnum() else " " for c in title).split())
    hexed = page_id.replace("-", "")
    return f"https://www.notion.so/{slug}-{hexed}" if slug else f"https://www.notion.so/{hexed}"


# --------------------------------------------------------------------------- reading a body


def read_object(raw: bytes) -> Json:
    """A request body: a JSON object, or an empty one when none was sent."""
    if not raw.strip():
        return {}
    try:
        found: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise Refusal(ErrorCode.INVALID_JSON, "The request body is not valid JSON.") from error
    if not isinstance(found, dict):
        raise invalid("The request body should be a JSON object.")
    return _OBJECT.validate_python(found)


def as_object(value: JsonValue, where: str) -> Json:
    if not isinstance(value, dict):
        raise invalid(f"{where} should be an object.")
    return value


def as_list(value: JsonValue, where: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise invalid(f"{where} should be an array.")
    return value


def as_text(value: JsonValue, where: str) -> str:
    if not isinstance(value, str):
        raise invalid(f"{where} should be a string.")
    return value


def as_bool(value: JsonValue, where: str) -> bool:
    if not isinstance(value, bool):
        raise invalid(f"{where} should be a boolean.")
    return value


def as_int(value: JsonValue, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid(f"{where} should be an integer.")
    return value


def only_keys(body: Json, allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise invalid(f"{where} has a field Notion does not accept here: {unknown[0]}.")


def page_size(value: JsonValue | None, where: str = "page_size") -> int:
    """`page_size`, from a JSON body (an integer) or a query string (digits)."""
    if value is None:
        return MAX_PAGE_SIZE
    if isinstance(value, str):
        if not value.isdigit():
            raise invalid(f"{where} should be a number.")
        value = int(value)
    size = as_int(value, where)
    if not 1 <= size <= MAX_PAGE_SIZE:
        raise invalid(f"{where} should be between 1 and {MAX_PAGE_SIZE}; got {size}.")
    return size


# --------------------------------------------------------------------------- rich text


class RichTextType:
    """Notion's rich text item types."""

    TEXT = "text"
    MENTION = "mention"
    EQUATION = "equation"


class MentionType:
    """The mentions this simulation builds."""

    USER = "user"
    PAGE = "page"
    DATABASE = "database"
    DATE = "date"


RICH_TEXT_TYPES = (RichTextType.TEXT, RichTextType.MENTION, RichTextType.EQUATION)
MENTION_TYPES = (MentionType.USER, MentionType.PAGE, MentionType.DATABASE, MentionType.DATE)

COLORS = frozenset(
    ["default"]
    + [
        f"{c}{s}"
        for c in ["gray", "brown", "orange", "yellow", "green", "blue", "purple", "pink", "red"]
        for s in ["", "_background"]
    ]
)
ANNOTATIONS = ("bold", "italic", "strikethrough", "underline", "code")


class Names(Protocol):
    """What a mention's `plain_text` is read from."""

    def user_name(self, user_id: str) -> str | None: ...

    def title_of(self, object_id: str) -> str | None: ...


def _annotations(value: JsonValue | None, where: str) -> Json:
    found: Json = {name: False for name in ANNOTATIONS}
    found["color"] = "default"
    if value is None:
        return found
    given = as_object(value, where)
    only_keys(given, [*ANNOTATIONS, "color"], where)
    for name in ANNOTATIONS:
        if name in given:
            found[name] = as_bool(given[name], f"{where}.{name}")
    if "color" in given:
        color = as_text(given["color"], f"{where}.color")
        if color not in COLORS:
            raise invalid(f"{where}.color is not a Notion colour: {color}.")
        found["color"] = color
    return found


def _date(value: JsonValue, where: str) -> Json:
    given = as_object(value, where)
    only_keys(given, ["start", "end", "time_zone"], where)
    if "start" not in given:
        raise invalid(f"{where}.start should be defined.")
    start = read_date(as_text(given["start"], f"{where}.start"), f"{where}.start")
    end = given["end"] if "end" in given else None
    if end is not None:
        read_date(as_text(end, f"{where}.end"), f"{where}.end")
    zone = given["time_zone"] if "time_zone" in given else None
    return {"start": start, "end": end, "time_zone": zone}


def read_date(spelled: str, where: str) -> str:
    """A date or a date-time as Notion takes it; returned as it was written."""
    try:
        if len(spelled) == 10:
            date.fromisoformat(spelled)
        else:
            datetime.fromisoformat(spelled.replace("Z", "+00:00"))
    except ValueError as error:
        raise invalid(f"{where} should be an ISO 8601 date or date-time; got `{spelled}`.") from error
    return spelled


def date_moment(spelled: str) -> datetime:
    """A stored date as an instant: a date alone is its midnight in UTC."""
    if len(spelled) == 10:
        return datetime.fromisoformat(spelled).replace(tzinfo=UTC)
    found = datetime.fromisoformat(spelled.replace("Z", "+00:00"))
    return found if found.tzinfo is not None else found.replace(tzinfo=UTC)


def rich_text(value: JsonValue, where: str, names: Names) -> list[JsonValue]:
    """A caller's rich text array, checked and normalised to what Notion stores and serves."""
    items = as_list(value, where)
    if len(items) > MAX_RICH_TEXT_ITEMS:
        raise invalid(f"{where} should have at most {MAX_RICH_TEXT_ITEMS} items; got {len(items)}.")
    return [_rich_item(item, f"{where}[{i}]", names) for i, item in enumerate(items)]


def _rich_item(value: JsonValue, where: str, names: Names) -> JsonValue:
    item = as_object(value, where)
    only_keys(item, ["type", "text", "mention", "equation", "annotations", "plain_text", "href"], where)
    spelled = as_text(item["type"], f"{where}.type") if "type" in item else _inferred(item, where)
    if spelled not in RICH_TEXT_TYPES:
        raise invalid(f"{where}.type should be text, mention or equation; got `{spelled}`.")
    kind = spelled
    if kind not in item:
        raise invalid(f"{where}.{kind} should be defined.")
    annotations = _annotations(item["annotations"] if "annotations" in item else None, f"{where}.annotations")
    body = as_object(item[kind], f"{where}.{kind}")
    if kind == RichTextType.TEXT:
        only_keys(body, ["content", "link"], f"{where}.text")
        if "content" not in body:
            raise invalid(f"{where}.text.content should be defined.")
        content = as_text(body["content"], f"{where}.text.content")
        if len(content.encode("utf-16-le")) // 2 > MAX_TEXT:
            raise invalid(f"{where}.text.content should be {MAX_TEXT} characters or fewer; got {len(content)}.")
        link = body["link"] if "link" in body else None
        href: str | None = None
        if link is not None:
            linked = as_object(link, f"{where}.text.link")
            href = as_text(linked["url"], f"{where}.text.link.url") if "url" in linked else None
            if href is None:
                raise invalid(f"{where}.text.link.url should be defined.")
        return {
            "type": kind,
            "text": {"content": content, "link": {"url": href} if href else None},
            "annotations": annotations,
            "plain_text": content,
            "href": href,
        }
    if kind == RichTextType.EQUATION:
        expression = as_text(body["expression"], f"{where}.equation.expression") if "expression" in body else None
        if expression is None:
            raise invalid(f"{where}.equation.expression should be defined.")
        if len(expression) > MAX_EQUATION:
            raise invalid(f"{where}.equation.expression should be {MAX_EQUATION} characters or fewer.")
        return {
            "type": kind,
            "equation": {"expression": expression},
            "annotations": annotations,
            "plain_text": expression,
            "href": None,
        }
    return _mention(body, annotations, where, names)


def _inferred(item: Json, where: str) -> str:
    for kind in RICH_TEXT_TYPES:
        if kind in item:
            return kind
    raise invalid(f"{where} should hold one of text, mention or equation.")


def _mention(body: Json, annotations: Json, where: str, names: Names) -> JsonValue:
    spelled = as_text(body["type"], f"{where}.mention.type") if "type" in body else _inferred_mention(body, where)
    if spelled not in MENTION_TYPES:
        raise invalid(f"{where}.mention.type is not one this simulation builds: `{spelled}`.")
    kind = spelled
    if kind not in body:
        raise invalid(f"{where}.mention.{kind} should be defined.")
    if kind == MentionType.DATE:
        when = _date(body["date"], f"{where}.mention.date")
        plain = str(when["start"]) + (f" → {when['end']}" if when["end"] else "")
        return {
            "type": "mention",
            "mention": {"type": kind, "date": when},
            "annotations": annotations,
            "plain_text": plain,
            "href": None,
        }
    target = as_object(body[kind], f"{where}.mention.{kind}")
    if "id" not in target:
        raise invalid(f"{where}.mention.{kind}.id should be defined.")
    target_id = canonical_id(as_text(target["id"], f"{where}.mention.{kind}.id"), f"{where}.mention.id")
    if kind == MentionType.USER:
        name = names.user_name(target_id)
        if name is None:
            raise invalid(f"{where}.mention.user.id names nobody in this workspace: {target_id}.")
        plain = f"@{name}"
        mentioned: Json = {"object": "user", "id": target_id}
        href = None
    else:
        title = names.title_of(target_id)
        if title is None:
            raise not_found(Missing.PAGE if kind == MentionType.PAGE else Missing.DATABASE, target_id)
        plain = title or "Untitled"
        mentioned = {"id": target_id}
        href = f"https://www.notion.so/{target_id.replace('-', '')}"
    return {
        "type": "mention",
        "mention": {"type": kind, kind: mentioned},
        "annotations": annotations,
        "plain_text": plain,
        "href": href,
    }


def _inferred_mention(body: Json, where: str) -> str:
    for kind in MENTION_TYPES:
        if kind in body:
            return kind
    raise invalid(f"{where}.mention should hold one of user, page, database or date.")


def plain(items: JsonValue) -> str:
    """The `plain_text` of a stored rich text array, joined."""
    if not isinstance(items, list):
        return ""
    found: list[str] = []
    for item in items:
        if isinstance(item, dict) and "plain_text" in item and isinstance(item["plain_text"], str):
            found.append(item["plain_text"])
    return "".join(found)


def text_run(content: str) -> list[JsonValue]:
    """Plain words as one stored rich text run, as a seed or a person writes them."""
    return [
        {
            "type": "text",
            "text": {"content": content, "link": None},
            "annotations": {**{name: False for name in ANNOTATIONS}, "color": "default"},
            "plain_text": content,
            "href": None,
        }
    ]


# --------------------------------------------------------------------------- blocks


class BlockType(StrEnum):
    PARAGRAPH = "paragraph"
    HEADING_1 = "heading_1"
    HEADING_2 = "heading_2"
    HEADING_3 = "heading_3"
    BULLETED_LIST_ITEM = "bulleted_list_item"
    NUMBERED_LIST_ITEM = "numbered_list_item"
    TO_DO = "to_do"
    TOGGLE = "toggle"
    CODE = "code"
    QUOTE = "quote"
    CALLOUT = "callout"
    DIVIDER = "divider"
    TABLE = "table"
    TABLE_ROW = "table_row"
    BOOKMARK = "bookmark"
    IMAGE = "image"
    LINK_PREVIEW = "link_preview"
    CHILD_PAGE = "child_page"
    CHILD_DATABASE = "child_database"


TEXT_BLOCKS = frozenset(
    {
        BlockType.PARAGRAPH,
        BlockType.HEADING_1,
        BlockType.HEADING_2,
        BlockType.HEADING_3,
        BlockType.BULLETED_LIST_ITEM,
        BlockType.NUMBERED_LIST_ITEM,
        BlockType.TO_DO,
        BlockType.TOGGLE,
        BlockType.CODE,
        BlockType.QUOTE,
        BlockType.CALLOUT,
    }
)
HEADINGS = frozenset({BlockType.HEADING_1, BlockType.HEADING_2, BlockType.HEADING_3})
NESTS = frozenset(
    {
        BlockType.PARAGRAPH,
        BlockType.BULLETED_LIST_ITEM,
        BlockType.NUMBERED_LIST_ITEM,
        BlockType.TO_DO,
        BlockType.TOGGLE,
        BlockType.QUOTE,
        BlockType.CALLOUT,
    }
)
"""Block types that take children; a heading does when it is toggleable, a table takes only its rows."""
NOT_BUILT = frozenset(
    {
        "column_list",
        "column",
        "embed",
        "equation",
        "file",
        "pdf",
        "video",
        "audio",
        "synced_block",
        "template",
        "breadcrumb",
        "table_of_contents",
        "link_to_page",
    }
)
"""Real block types this simulation does not build; a request naming one is refused, saying so."""
CODE_LANGUAGES = frozenset(
    {
        "plain text",
        "bash",
        "c",
        "c++",
        "c#",
        "css",
        "diff",
        "go",
        "html",
        "java",
        "javascript",
        "json",
        "kotlin",
        "markdown",
        "mermaid",
        "python",
        "ruby",
        "rust",
        "shell",
        "sql",
        "swift",
        "typescript",
        "yaml",
    }
)


class NewBlock(Model):
    """A block a caller asked for, checked, with its children, before it is given an id."""

    type: BlockType
    content: Json
    children: list[NewBlock] = []


def block_type(spelled: str, where: str) -> BlockType:
    if spelled in NOT_BUILT:
        raise invalid(f"{where}: this simulation does not build `{spelled}` blocks.")
    try:
        return BlockType(spelled)
    except ValueError as error:
        raise invalid(f"{where}.type is not a block type: `{spelled}`.") from error


def new_blocks(value: JsonValue, where: str, names: Names, *, depth: int = 0, seeding: bool = False) -> list[NewBlock]:
    """`children` as a caller sends them to create a page or append to one. A seed writes what the API cannot:
    a link preview, and more than a request's hundred blocks."""
    items = as_list(value, where)
    if len(items) > MAX_CHILDREN and not seeding:
        raise invalid(f"{where} should have at most {MAX_CHILDREN} items; got {len(items)}.")
    return [_new_block(item, f"{where}[{i}]", names, depth, seeding) for i, item in enumerate(items)]


def _new_block(value: JsonValue, where: str, names: Names, depth: int, seeding: bool) -> NewBlock:
    item = as_object(value, where)
    named = [k for k in item if k not in ("object", "type")]
    spelled = as_text(item["type"], f"{where}.type") if "type" in item else (named[0] if len(named) == 1 else "")
    if not spelled:
        raise invalid(f"{where} should name its type.")
    kind = block_type(spelled, where)
    only_keys(item, ["object", "type", kind.value], where)
    if kind in (BlockType.CHILD_PAGE, BlockType.CHILD_DATABASE):
        raise invalid(f"{where}: a {kind.value} is made with the pages or databases endpoint, not as a block.")
    if kind is BlockType.LINK_PREVIEW and not seeding:
        raise invalid(f"{where}: a link_preview block cannot be created through the API.")
    if kind is BlockType.TABLE_ROW:
        raise invalid(f"{where}: a table_row can only be a child of a table.")
    if kind.value not in item:
        raise invalid(f"{where}.{kind.value} should be defined.")
    body = dict(as_object(item[kind.value], f"{where}.{kind.value}"))
    children_given = body.pop("children") if "children" in body else None
    content = block_content(kind, body, f"{where}.{kind.value}", names, creating=True)
    if children_given is None:
        if kind is BlockType.TABLE:
            raise invalid(f"{where}.table.children should hold at least one table_row.")
        return NewBlock(type=kind, content=content)
    if depth >= MAX_NESTING:
        raise invalid(f"{where}: children may be nested at most {MAX_NESTING} levels deep in one request.")
    if kind is BlockType.TABLE:
        return NewBlock(type=kind, content=content, children=_table_rows(children_given, content, where, names))
    if not takes_children(kind, content):
        raise invalid(f"{where}: a {kind.value} block cannot have children.")
    return NewBlock(
        type=kind,
        content=content,
        children=new_blocks(children_given, f"{where}.{kind.value}.children", names, depth=depth + 1, seeding=seeding),
    )


def takes_children(kind: BlockType, content: Json) -> bool:
    if kind in NESTS:
        return True
    return kind in HEADINGS and content["is_toggleable"] is True


def _table_rows(value: JsonValue, table: Json, where: str, names: Names) -> list[NewBlock]:
    rows = as_list(value, f"{where}.table.children")
    if not rows:
        raise invalid(f"{where}.table.children should hold at least one table_row.")
    return [table_row(row, table, f"{where}.table.children[{i}]", names) for i, row in enumerate(rows)]


def table_row(value: JsonValue, table: Json, where: str, names: Names) -> NewBlock:
    item = as_object(value, where)
    spelled = as_text(item["type"], f"{where}.type") if "type" in item else "table_row"
    if spelled != BlockType.TABLE_ROW.value or "table_row" not in item:
        raise invalid(f"{where}: a table holds only table_row blocks.")
    body = as_object(item["table_row"], f"{where}.table_row")
    only_keys(body, ["cells"], f"{where}.table_row")
    cells = as_list(body["cells"] if "cells" in body else None, f"{where}.table_row.cells")
    width = table["table_width"]
    if len(cells) != width:
        raise invalid(f"{where}.table_row.cells should have {width} cells, as the table is wide; got {len(cells)}.")
    return NewBlock(
        type=BlockType.TABLE_ROW,
        content={"cells": [rich_text(c, f"{where}.table_row.cells[{i}]", names) for i, c in enumerate(cells)]},
    )


def block_content(kind: BlockType, body: Json, where: str, names: Names, *, creating: bool) -> Json:
    """A block's type body checked and filled with Notion's defaults. Updating merges onto the stored body
    before this is called, so the same rules hold for both."""
    if kind in TEXT_BLOCKS:
        allowed = ["rich_text", "color"]
        if kind is BlockType.TO_DO:
            allowed.append("checked")
        if kind in HEADINGS:
            allowed.append("is_toggleable")
        if kind is BlockType.CODE:
            allowed = ["rich_text", "caption", "language"]
        if kind is BlockType.CALLOUT:
            allowed.append("icon")
        only_keys(body, allowed, where)
        if "rich_text" not in body:
            raise invalid(f"{where}.rich_text should be defined.")
        found: Json = {"rich_text": rich_text(body["rich_text"], f"{where}.rich_text", names)}
        if kind is BlockType.CODE:
            language = as_text(body["language"], f"{where}.language") if "language" in body else None
            if language is None:
                raise invalid(f"{where}.language should be defined.")
            if language not in CODE_LANGUAGES:
                raise invalid(f"{where}.language is not one this simulation knows: `{language}`.")
            found["language"] = language
            found["caption"] = rich_text(body["caption"], f"{where}.caption", names) if "caption" in body else []
            return found
        color = as_text(body["color"], f"{where}.color") if "color" in body else "default"
        if color not in COLORS:
            raise invalid(f"{where}.color is not a Notion colour: {color}.")
        found["color"] = color
        if kind is BlockType.TO_DO:
            found["checked"] = as_bool(body["checked"], f"{where}.checked") if "checked" in body else False
        if kind in HEADINGS:
            toggles = as_bool(body["is_toggleable"], f"{where}.is_toggleable") if "is_toggleable" in body else False
            found["is_toggleable"] = toggles
        if kind is BlockType.CALLOUT:
            found["icon"] = icon(body["icon"], f"{where}.icon") if "icon" in body else None
        return found
    if kind is BlockType.DIVIDER:
        only_keys(body, [], where)
        return {}
    if kind is BlockType.TABLE:
        only_keys(body, ["table_width", "has_column_header", "has_row_header"], where)
        if "table_width" not in body:
            raise invalid(f"{where}.table_width should be defined.")
        width = as_int(body["table_width"], f"{where}.table_width")
        if width < 1:
            raise invalid(f"{where}.table_width should be at least 1.")
        return {
            "table_width": width,
            "has_column_header": as_bool(body["has_column_header"], where) if "has_column_header" in body else False,
            "has_row_header": as_bool(body["has_row_header"], where) if "has_row_header" in body else False,
        }
    if kind in (BlockType.BOOKMARK, BlockType.LINK_PREVIEW):
        only_keys(body, ["url", "caption"] if kind is BlockType.BOOKMARK else ["url"], where)
        if "url" not in body:
            raise invalid(f"{where}.url should be defined.")
        found = {"url": as_text(body["url"], f"{where}.url")}
        if kind is BlockType.BOOKMARK:
            found["caption"] = rich_text(body["caption"], f"{where}.caption", names) if "caption" in body else []
        return found
    if kind is BlockType.IMAGE:
        only_keys(body, ["type", "external", "caption"], where)
        if "external" not in body:
            raise invalid(f"{where}: this simulation takes an image only as `external`, with a url.")
        external = as_object(body["external"], f"{where}.external")
        if "url" not in external:
            raise invalid(f"{where}.external.url should be defined.")
        return {
            "type": "external",
            "external": {"url": as_text(external["url"], f"{where}.external.url")},
            "caption": rich_text(body["caption"], f"{where}.caption", names) if "caption" in body else [],
        }
    if kind is BlockType.TABLE_ROW:
        raise invalid(f"{where}: a table_row is written with its table.")
    raise invalid(f"{where}: a {kind.value} block is not written this way.")


def icon(value: JsonValue, where: str) -> JsonValue:
    if value is None:
        return None
    found = as_object(value, where)
    if "emoji" in found:
        return {"type": "emoji", "emoji": as_text(found["emoji"], f"{where}.emoji")}
    if "external" in found:
        external = as_object(found["external"], f"{where}.external")
        return {"type": "external", "external": {"url": as_text(external["url"], f"{where}.external.url")}}
    raise invalid(f"{where} should be an emoji or an external file.")


def block_text(kind: BlockType, content: Json, title: str | None) -> str:
    """One block's own text, for search, the world's snapshot and a person reading it."""
    if kind in TEXT_BLOCKS:
        words = plain(content["rich_text"])
        if kind is BlockType.TO_DO:
            return f"[{'x' if content['checked'] is True else ' '}] {words}"
        return words
    if kind is BlockType.TABLE_ROW:
        cells = content["cells"]
        return " | ".join(plain(c) for c in cells) if isinstance(cells, list) else ""
    if kind in (BlockType.BOOKMARK, BlockType.LINK_PREVIEW):
        return str(content["url"])
    if kind is BlockType.IMAGE:
        return plain(content["caption"])
    if kind is BlockType.DIVIDER:
        return "---"
    return title or ""


# --------------------------------------------------------------------------- stored entities


class ParentType(StrEnum):
    PAGE_ID = "page_id"
    DATABASE_ID = "database_id"
    BLOCK_ID = "block_id"
    WORKSPACE = "workspace"


class Parent(Model):
    type: ParentType
    id: str | None = Field(default=None, description="None for the workspace")

    def render(self) -> Json:
        if self.type is ParentType.WORKSPACE:
            return {"type": "workspace", "workspace": True}
        return {"type": self.type.value, self.type.value: self.id}


class Stamps(Model):
    created_time: str
    created_by: str
    last_edited_time: str
    last_edited_by: str

    def edited(self, when: str, by: str) -> Stamps:
        return self.model_copy(update={"last_edited_time": when, "last_edited_by": by})

    def render(self) -> Json:
        return {
            "created_time": self.created_time,
            "created_by": {"object": "user", "id": self.created_by},
            "last_edited_time": self.last_edited_time,
            "last_edited_by": {"object": "user", "id": self.last_edited_by},
        }


class StoredBlock(Model):
    id: str
    type: BlockType
    content: Json = Field(description="The type body; empty for child_page and child_database, read live")
    parent: Parent
    stamps: Stamps
    archived: bool = False


class StoredPage(Model):
    """A page, or a row of a database, with every block below it (not its child pages' blocks)."""

    kind: Literal["page"] = "page"
    id: str
    workspace: str
    parent: Parent
    stamps: Stamps
    archived: bool = False
    icon: JsonValue = None
    cover: JsonValue = None
    properties: dict[str, Json] = Field(description="By name: Notion's property value, people kept as ids")
    blocks: dict[str, StoredBlock] = {}
    children: dict[str, list[str]] = Field(default={}, description="Container id to its child block ids, in order")


class StoredDatabase(Model):
    kind: Literal["database"] = "database"
    id: str
    workspace: str
    parent: Parent
    stamps: Stamps
    title: list[JsonValue]
    description: list[JsonValue] = []
    schema_: dict[str, Json] = Field(alias="schema", description="By name: Notion's property schema object")
    is_inline: bool = False
    archived: bool = False
    icon: JsonValue = None
    cover: JsonValue = None


class UserType(StrEnum):
    PERSON = "person"
    BOT = "bot"


class StoredUser(Model):
    kind: Literal["user"] = "user"
    id: str
    workspace: str
    type: UserType
    name: str
    email: str | None = None
    integration: str | None = Field(default=None, description="For a bot: the integration it acts for")
    removed: bool = Field(
        default=False,
        description="Removed from the workspace: unlisted and not found; what they wrote still names them",
    )


class Capability(StrEnum):
    READ_CONTENT = "read_content"
    UPDATE_CONTENT = "update_content"
    INSERT_CONTENT = "insert_content"
    READ_COMMENTS = "read_comments"
    INSERT_COMMENTS = "insert_comments"
    READ_USERS_WITH_EMAIL = "read_users_with_email"
    READ_USERS_WITHOUT_EMAIL = "read_users_without_email"


ALL_CAPABILITIES = list(Capability)


class IntegrationKind(StrEnum):
    INTERNAL = "internal"
    PUBLIC = "public"


class StoredIntegration(Model):
    kind: Literal["integration"] = "integration"
    id: str = Field(description="Its bot user's id")
    key: str = Field(description="Minutehand's own: the seed's key for it; never served")
    workspace: str
    name: str
    type: IntegrationKind
    capabilities: list[Capability]
    shared: list[str] = Field(description="Ids of the pages and databases shared with it")
    owner: str | None = Field(default=None, description="For a public integration: the person who installed it")
    client_id: str | None = None
    client_secret_digest: str | None = None
    redirect_uris: list[str] = []


class TokenKind(StrEnum):
    INTERNAL = "internal"
    ACCESS = "access"
    REFRESH = "refresh"
    CODE = "code"


class StoredToken(Model):
    """A secret the integration presents, kept under its digest; an authorization code is one until used."""

    kind: Literal["token"] = "token"
    type: TokenKind
    integration: str
    redirect_uri: str | None = None
    used: bool = False


class StoredWorkspace(Model):
    kind: Literal["workspace"] = "workspace"
    id: str
    name: str


class StoredComment(Model):
    kind: Literal["comment"] = "comment"
    id: str
    page: str
    discussion_id: str
    created_time: str
    created_by: str
    rich_text: list[JsonValue]


class FaultKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    CONFLICT = "conflict"


class PlannedFault(Model):
    kind: FaultKind
    times: int
    retry_after: int = 1
    method: str | None = None
    path: str | None = None
    integration: str | None = None


class StoredSchedule(Model):
    """The faults the seed arms, resolved to ids."""

    kind: Literal["schedule"] = "schedule"
    faults: list[PlannedFault] = []


class StoredCount(Model):
    """How many times a fault has fired."""

    kind: Literal["count"] = "count"
    count: int


class WebhookEvent(StrEnum):
    """The event types a webhook subscription can ask for that this provider sends."""

    PAGE_CREATED = "page.created"
    PAGE_CONTENT_UPDATED = "page.content_updated"
    PAGE_PROPERTIES_UPDATED = "page.properties_updated"
    PAGE_DELETED = "page.deleted"
    PAGE_UNDELETED = "page.undeleted"
    COMMENT_CREATED = "comment.created"


class StoredWebhook(Model):
    """Minutehand's own: a webhook subscription of one integration, set up in its settings, never on the API."""

    kind: Literal["webhook"] = "webhook"
    id: str
    workspace: str
    integration: str = Field(description="The integration's bot user id")
    url: str
    verification_token: str = Field(description="What Notion hands the subscriber once, and signs every event with")
    events: list[WebhookEvent]
    verified: bool


class StoredOwedEvent(Model):
    """Minutehand's own: an event a subscription is owed and has not been sent; deleted once sent."""

    kind: Literal["owed"] = "owed"
    id: str
    subscription: str
    type: WebhookEvent
    timestamp: str
    entity_id: str
    entity_type: Literal["page", "comment"]
    author: str
    author_type: UserType
    parent: Parent
    page_id: str | None = Field(default=None, description="For a comment: the page it is on")
    updated_blocks: list[str] = []
    updated_properties: list[str] = []


Stored = Annotated[
    StoredPage
    | StoredDatabase
    | StoredUser
    | StoredIntegration
    | StoredToken
    | StoredWorkspace
    | StoredComment
    | StoredSchedule
    | StoredCount
    | StoredWebhook
    | StoredOwedEvent,
    Field(discriminator="kind"),
]
_STORED: TypeAdapter[Stored] = TypeAdapter(Stored)


def parse(body: str) -> Stored:
    return _STORED.validate_json(body)


def dump(stored: Model) -> str:
    return stored.model_dump_json(by_alias=True)


# --------------------------------------------------------------------------- properties


class PropertyType(StrEnum):
    TITLE = "title"
    RICH_TEXT = "rich_text"
    NUMBER = "number"
    SELECT = "select"
    MULTI_SELECT = "multi_select"
    STATUS = "status"
    DATE = "date"
    PEOPLE = "people"
    CHECKBOX = "checkbox"
    URL = "url"
    EMAIL = "email"
    PHONE_NUMBER = "phone_number"
    RELATION = "relation"
    CREATED_TIME = "created_time"
    LAST_EDITED_TIME = "last_edited_time"
    CREATED_BY = "created_by"
    LAST_EDITED_BY = "last_edited_by"


READ_ONLY = frozenset(
    {PropertyType.CREATED_TIME, PropertyType.LAST_EDITED_TIME, PropertyType.CREATED_BY, PropertyType.LAST_EDITED_BY}
)
PROPERTIES_NOT_BUILT = frozenset({"formula", "rollup", "files", "unique_id", "verification", "button"})


def property_id(database_id: str, name: str, kind: PropertyType) -> str:
    """A property's id: `title` for the title, else four URL-safe characters, stable for its name."""
    if kind is PropertyType.TITLE:
        return "title"
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    number = int(hashlib.sha256(f"{database_id}\x1f{name}".encode()).hexdigest()[:12], 16)
    return "".join(alphabet[(number >> (6 * i)) % len(alphabet)] for i in range(4))


def option(database_id: str, name: str, color: str = "default") -> Json:
    return {"id": minted_id("option", database_id, name), "name": name, "color": color}


def schema_type(schema: Json) -> PropertyType:
    return PropertyType(as_text(schema["type"], "type"))


def new_schema(database_id: str, name: str, kind: PropertyType, choices: Sequence[str], relates_to: str | None) -> Json:
    """A property's schema as a database holds it."""
    config: Json = {}
    if kind is PropertyType.NUMBER:
        config = {"format": "number"}
    elif kind in (PropertyType.SELECT, PropertyType.MULTI_SELECT):
        config = {"options": [option(database_id, c) for c in choices]}
    elif kind is PropertyType.STATUS:
        names = list(choices) or ["Not started", "In progress", "Done"]
        options: list[JsonValue] = [option(database_id, c) for c in names]
        groups: list[tuple[str, list[JsonValue]]] = [
            ("To-do", options[:1]),
            ("In progress", options[1:-1] if len(options) > 2 else []),
            ("Complete", options[-1:] if len(options) > 1 else []),
        ]
        config = {
            "options": options,
            "groups": [
                {
                    "id": minted_id("group", database_id, label),
                    "name": label,
                    "color": "default",
                    "option_ids": [o["id"] for o in members if isinstance(o, dict)],
                }
                for label, members in groups
            ],
        }
    elif kind is PropertyType.RELATION:
        config = {"database_id": relates_to, "type": "single_property", "single_property": {}}
    return {"id": property_id(database_id, name, kind), "name": name, "type": kind.value, kind.value: config}


_SCHEMA_KEYS = frozenset({"name", "type", "id"})


def schema_from_request(
    database_id: str, name: str, value: JsonValue, where: str, known: Callable[[str], bool]
) -> Json:
    """One property of `databases.create` or `databases.update`: `{"<type>": {...}}`."""
    given = as_object(value, where)
    kinds = [k for k in given if k not in _SCHEMA_KEYS]
    if len(kinds) != 1:
        raise invalid(f"{where} should name exactly one property type.")
    spelled = kinds[0]
    if spelled in PROPERTIES_NOT_BUILT:
        raise invalid(f"{where}: this simulation does not build `{spelled}` properties.")
    try:
        kind = PropertyType(spelled)
    except ValueError as error:
        raise invalid(f"{where} names no property type: `{spelled}`.") from error
    config = as_object(given[spelled], f"{where}.{spelled}")
    shown = as_text(given["name"], f"{where}.name") if "name" in given else name
    choices: list[str] = []
    if "options" in config:
        for i, item in enumerate(as_list(config["options"], f"{where}.{spelled}.options")):
            listed = as_object(item, f"{where}.{spelled}.options[{i}]")
            choices.append(as_text(listed["name"], f"{where}.{spelled}.options[{i}].name"))
    relates_to: str | None = None
    if kind is PropertyType.RELATION:
        if "database_id" not in config:
            raise invalid(f"{where}.relation.database_id should be defined.")
        relates_to = canonical_id(as_text(config["database_id"], where), f"{where}.relation.database_id")
        if not known(relates_to):
            raise not_found(Missing.DATABASE, relates_to)
    return new_schema(database_id, shown, kind, choices, relates_to)


class People(Protocol):
    def is_member(self, user_id: str) -> bool: ...


def property_value(schema: Json, value: JsonValue, where: str, names: Names, people: People) -> tuple[Json, Json]:
    """A caller's value for one property: the stored value, and the schema after it (a new select option is
    added to the schema, as Notion does)."""
    kind = schema_type(schema)
    if kind in READ_ONLY:
        raise invalid(f"{where}: a {kind.value} property cannot be written.")
    given = as_object(value, where)
    given = {k: v for k, v in given.items() if k not in ("id", "type")}
    if list(given) != [kind.value]:
        raise invalid(f"{where} is a {kind.value} property; the value should be given as `{kind.value}`.")
    raw = given[kind.value]
    stored: JsonValue
    if kind in (PropertyType.TITLE, PropertyType.RICH_TEXT):
        stored = rich_text(raw, f"{where}.{kind.value}", names)
    elif kind is PropertyType.NUMBER:
        if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int | float)):
            raise invalid(f"{where}.number should be a number or null.")
        stored = raw
    elif kind is PropertyType.CHECKBOX:
        stored = as_bool(raw, f"{where}.checkbox")
    elif kind in (PropertyType.URL, PropertyType.EMAIL, PropertyType.PHONE_NUMBER):
        stored = None if raw is None else as_text(raw, f"{where}.{kind.value}")
    elif kind is PropertyType.DATE:
        stored = None if raw is None else _date(raw, f"{where}.date")
    elif kind in (PropertyType.SELECT, PropertyType.STATUS):
        if raw is None:
            stored = None
        else:
            chosen, schema = _choose(schema, kind, raw, f"{where}.{kind.value}")
            stored = chosen
    elif kind is PropertyType.MULTI_SELECT:
        picked: list[JsonValue] = []
        for i, item in enumerate(as_list(raw, f"{where}.multi_select")):
            chosen, schema = _choose(schema, kind, item, f"{where}.multi_select[{i}]")
            picked.append(chosen)
        stored = picked
    elif kind is PropertyType.PEOPLE:
        found: list[JsonValue] = []
        for i, item in enumerate(as_list(raw, f"{where}.people")):
            person = as_object(item, f"{where}.people[{i}]")
            if "id" not in person:
                raise invalid(f"{where}.people[{i}].id should be defined.")
            user_id = canonical_id(as_text(person["id"], where), f"{where}.people[{i}].id")
            if not people.is_member(user_id):
                raise invalid(f"{where}.people[{i}] names nobody in this workspace: {user_id}.")
            found.append({"object": "user", "id": user_id})
        stored = found
    else:
        related: list[JsonValue] = []
        for i, item in enumerate(as_list(raw, f"{where}.relation")):
            linked = as_object(item, f"{where}.relation[{i}]")
            if "id" not in linked:
                raise invalid(f"{where}.relation[{i}].id should be defined.")
            related.append({"id": canonical_id(as_text(linked["id"], where), f"{where}.relation[{i}].id")})
        stored = related
    return {"id": schema["id"], "type": kind.value, kind.value: stored}, schema


def _choose(schema: Json, kind: PropertyType, value: JsonValue, where: str) -> tuple[JsonValue, Json]:
    given = as_object(value, where)
    config = as_object(schema[kind.value], where)
    options = as_list(config["options"], where) if "options" in config else []
    for item in options:
        listed = as_object(item, where)
        if ("id" in given and given["id"] == listed["id"]) or ("name" in given and given["name"] == listed["name"]):
            return listed, schema
    if "name" not in given:
        raise invalid(f"{where}: no option has the id given.")
    name = as_text(given["name"], f"{where}.name")
    if kind is PropertyType.STATUS:
        raise invalid(f"{where}: `{name}` is not an option of this status property.")
    if "," in name:
        raise invalid(f"{where}.name should not contain a comma.")
    added = option(as_text(schema["id"], "id") + "/" + as_text(schema["name"], "name"), name)
    widened = {**schema, kind.value: {**config, "options": [*options, added]}}
    return added, widened


def empty_value(schema: Json) -> Json:
    """What a row holds for a property it was never given."""
    kind = schema_type(schema)
    blank: JsonValue
    if (
        kind in (PropertyType.TITLE, PropertyType.RICH_TEXT, PropertyType.MULTI_SELECT, PropertyType.PEOPLE)
        or kind is PropertyType.RELATION
    ):
        blank = []
    elif kind is PropertyType.CHECKBOX:
        blank = False
    else:
        blank = None
    return {"id": schema["id"], "type": kind.value, kind.value: blank}


def value_text(value: Json) -> str:
    """A stored property value as words: for the world's record and for matching a filter's text."""
    kind = PropertyType(as_text(value["type"], "type"))
    raw = value[kind.value]
    if kind in (PropertyType.TITLE, PropertyType.RICH_TEXT):
        return plain(raw)
    if kind in (PropertyType.SELECT, PropertyType.STATUS):
        return str(raw["name"]) if isinstance(raw, dict) else ""
    if kind is PropertyType.MULTI_SELECT:
        return ", ".join(str(o["name"]) for o in raw if isinstance(o, dict)) if isinstance(raw, list) else ""
    if kind is PropertyType.DATE:
        return str(raw["start"]) if isinstance(raw, dict) else ""
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return "yes" if raw else "no"
    if isinstance(raw, list):
        return ", ".join(str(r["id"]) for r in raw if isinstance(r, dict))
    return str(raw)


# --------------------------------------------------------------------------- rendering


def render_user(user: StoredUser, *, email: bool, owner: JsonValue = None, workspace_name: str = "") -> Json:
    found: Json = {"object": "user", "id": user.id, "name": user.name, "avatar_url": None, "type": user.type.value}
    if user.type is UserType.PERSON:
        found["person"] = {"email": user.email} if email and user.email else {}
    else:
        found["bot"] = {"owner": owner, "workspace_name": workspace_name} if owner is not None else {}
    return found


def render_list(results: Sequence[JsonValue], next_cursor: str | None, kind: str, request: str) -> Json:
    return {
        "object": "list",
        "results": list(results),
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
        "type": kind,
        kind: {},
        "request_id": request,
    }


def respond(found: JsonValue) -> str:
    return json.dumps(found, ensure_ascii=False)


# --------------------------------------------------------------------------- OAuth


class GrantType(StrEnum):
    AUTHORIZATION_CODE = "authorization_code"
    REFRESH_TOKEN = "refresh_token"


class TokenRequest(Model):
    grant_type: str
    code: str | None = None
    redirect_uri: str | None = None
    refresh_token: str | None = None
    external_account: JsonValue = None


def read_token_request(found: Json) -> TokenRequest:
    try:
        return TokenRequest.model_validate(found)
    except ValidationError as error:
        raise invalid(f"The token request is not one Notion takes: {error.errors()[0]['msg']}.") from error


def oauth_error(error: str, description: str) -> str:
    return json.dumps({"error": error, "error_description": description})
