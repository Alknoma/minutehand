"""Slack's own JSON: the only module that parses or builds it.

Three families of model live here:

- **Stored** — `SlackUser`, `SlackChannel`, `SlackMessage`, `SlackMembership`: the
  body of each entity in the store, in the shape the Web API returns it.
- **Arguments** — one model per method, read from the query string, a
  form-encoded body or a JSON body. Slack ignores an argument a method does not
  take, so `read_args` keeps only the fields the model declares; everything it
  keeps is validated.
- **Responses** — the `ok` envelope, error codes, cursors, and the Events API
  `event_callback` a pushed message arrives in.
"""

from __future__ import annotations

import base64
import binascii
import json
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeVar
from urllib.parse import parse_qsl

from pydantic import Field, JsonValue, ValidationError, field_validator

from minutehand.domain.scenario import Model

MAX_TEXT_CHARS = 40_000
MAX_BLOCKS = 50
PAGE_DEFAULT = 100
PAGE_MAX = 1000


class Refusal(Exception):
    """Slack answered `ok: false`. `error` is Slack's own code."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


# --------------------------------------------------------------------------- stored


class SlackProfile(Model):
    real_name: str
    display_name: str
    email: str | None = None
    title: str = ""
    bot_id: str | None = None


class SlackUser(Model):
    id: str
    team_id: str
    name: str
    real_name: str
    deleted: bool = False
    is_bot: bool = False
    is_app_user: bool = False
    tz: str = "UTC"
    tz_offset: int = 0
    profile: SlackProfile


class SlackTopic(Model):
    value: str = ""
    creator: str = ""
    last_set: int = 0


class SlackChannel(Model):
    id: str
    name: str | None = None
    is_channel: bool = False
    is_group: bool = False
    is_im: bool = False
    is_mpim: bool = False
    is_private: bool = False
    is_archived: bool = False
    is_general: bool = False
    created: int
    creator: str
    user: str | None = Field(default=None, description="The other party of an IM")
    topic: SlackTopic | None = None
    purpose: SlackTopic | None = None
    is_member: bool | None = Field(default=None, description="Whether the calling app is in it; set when served")


class SlackMembership(Model):
    channel: str
    user: str


class SlackEdited(Model):
    user: str
    ts: str


class SlackReaction(Model):
    name: str
    users: list[str]
    count: int


class SlackMessage(Model):
    type: Literal["message"] = "message"
    ts: str
    user: str
    text: str = ""
    team: str
    bot_id: str | None = None
    app_id: str | None = None
    thread_ts: str | None = None
    blocks: list[JsonValue] | None = None
    attachments: list[JsonValue] | None = None
    edited: SlackEdited | None = None
    reactions: list[SlackReaction] | None = None
    reply_count: int | None = Field(default=None, description="Thread summary; computed when served, never stored")
    reply_users: list[str] | None = None
    reply_users_count: int | None = None
    latest_reply: str | None = None


StoredModel = TypeVar("StoredModel", SlackUser, SlackChannel, SlackMessage, SlackMembership)


def parse(model: type[StoredModel], body: str) -> StoredModel:
    return model.model_validate_json(body)


def dump(entity: Model) -> str:
    return entity.model_dump_json(exclude_none=True)


# --------------------------------------------------------------------------- arguments


def _blocks_from_form(value: object) -> object:
    """A form-encoded body carries `blocks` and `attachments` as JSON text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("not JSON") from error
    return value


class NoArgs(Model):
    pass


class ListArgs(Model):
    cursor: str | None = None
    limit: int = 0


ConversationType = Literal["public_channel", "private_channel", "im", "mpim"]
"""How `conversations.list` names a conversation's type in its `types` argument."""
CONVERSATION_TYPES: frozenset[str] = frozenset({"public_channel", "private_channel", "im", "mpim"})


def conversation_type(channel: SlackChannel) -> ConversationType:
    if channel.is_im:
        return "im"
    if channel.is_mpim:
        return "mpim"
    return "private_channel" if channel.is_private else "public_channel"


def event_channel_type(channel: SlackChannel) -> EventChannelType:
    if channel.is_im:
        return "im"
    if channel.is_mpim:
        return "mpim"
    return "group" if channel.is_private else "channel"


class ConversationsListArgs(ListArgs):
    types: str = "public_channel"
    exclude_archived: bool = False


class ChannelArgs(Model):
    channel: str = ""


class ConversationsOpenArgs(Model):
    users: str = ""
    channel: str = ""
    return_im: bool = False


class MembersArgs(ListArgs):
    channel: str = ""


class HistoryArgs(ListArgs):
    channel: str = ""
    latest: str | None = None
    oldest: str | None = None
    inclusive: bool = False


class RepliesArgs(HistoryArgs):
    ts: str = ""


class UserArgs(Model):
    user: str = ""


class EmailArgs(Model):
    email: str = ""


class PostMessageArgs(Model):
    channel: str = ""
    text: str = ""
    thread_ts: str | None = None
    blocks: list[JsonValue] | None = None
    attachments: list[JsonValue] | None = None
    as_user: bool = False

    _parse_blocks = field_validator("blocks", "attachments", mode="before")(_blocks_from_form)


class UpdateArgs(Model):
    channel: str = ""
    ts: str = ""
    text: str | None = None
    blocks: list[JsonValue] | None = None
    attachments: list[JsonValue] | None = None

    _parse_blocks = field_validator("blocks", "attachments", mode="before")(_blocks_from_form)


class DeleteArgs(Model):
    channel: str = ""
    ts: str = ""


class ReactionArgs(Model):
    channel: str = ""
    timestamp: str = ""
    name: str = ""


Args = TypeVar("Args", bound=Model)


class Presented(Model):
    """What one call to the Web API presented: its arguments as Slack reads them, and its token."""

    token: str | None
    arguments: dict[str, JsonValue] = Field(description="Every argument from the query, form or JSON body")


def read_call(query: str, content_type: str, body: bytes, authorization: str | None) -> Presented:
    """Merge the query string with the body the way Slack does, and find the token.

    Slack reads a bearer token from `Authorization`, or a `token` argument.
    """
    merged: dict[str, JsonValue] = dict(parse_qsl(query, keep_blank_values=True))
    media = content_type.split(";", 1)[0].strip().lower()
    if body and media == "application/json":
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as error:
            raise Refusal("invalid_json") from error
        if not isinstance(decoded, dict):
            raise Refusal("invalid_json")
        merged.update(decoded)
    elif body:
        merged.update(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    token: str | None = None
    if authorization is not None and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip() or None
    presented = merged.pop("token", None)
    if token is None and isinstance(presented, str) and presented:
        token = presented
    return Presented(token=token, arguments=merged)


def read_args(model: type[Args], presented: Presented) -> Args:
    """The arguments `model` declares; anything else is ignored, as Slack ignores it."""
    known = {name: value for name, value in presented.arguments.items() if name in model.model_fields}
    try:
        return model.model_validate(known)
    except ValidationError as error:
        raise Refusal("invalid_arguments") from error


def page_size(limit: int) -> int:
    return PAGE_DEFAULT if limit <= 0 else min(limit, PAGE_MAX)


def encode_cursor(position: str) -> str:
    return base64.urlsafe_b64encode(f"next:{position}".encode()).decode()


def decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        text = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as error:
        raise Refusal("invalid_cursor") from error
    head, _, position = text.partition(":")
    if head != "next" or not position:
        raise Refusal("invalid_cursor")
    return position


def timestamp(value: str, error: str) -> Decimal:
    """A Slack `ts`, `latest` or `oldest` as a number, for ordering."""
    try:
        return Decimal(value)
    except InvalidOperation as failure:
        raise Refusal(error) from failure


def check_message(text: str, blocks: list[JsonValue] | None) -> None:
    """Refuse what Slack refuses about a message's body, with Slack's own code."""
    if len(text) > MAX_TEXT_CHARS:
        raise Refusal("msg_too_long")
    if blocks is None:
        return
    if len(blocks) > MAX_BLOCKS:
        raise Refusal("invalid_blocks")
    for block in blocks:
        if not isinstance(block, dict) or "type" not in block or not isinstance(block["type"], str):
            raise Refusal("invalid_blocks")


def visible_text(text: str, blocks: list[JsonValue] | None) -> str:
    """What a reader sees: the message's text, or the text inside its blocks when it has none."""
    if text or not blocks:
        return text
    found: list[str] = []

    def walk(node: JsonValue) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "text" and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blocks)
    return "\n".join(found)


# --------------------------------------------------------------------------- responses


class Ok(Model):
    ok: Literal[True] = True


class Failed(Model):
    ok: Literal[False] = False
    error: str


class ResponseMetadata(Model):
    next_cursor: str = ""


class AuthTest(Ok):
    url: str
    team: str
    user: str
    team_id: str
    user_id: str
    bot_id: str
    is_enterprise_install: bool = False


class UserList(Ok):
    members: list[SlackUser]
    response_metadata: ResponseMetadata


class OneUser(Ok):
    user: SlackUser


class ChannelList(Ok):
    channels: list[SlackChannel]
    response_metadata: ResponseMetadata


class OneChannel(Ok):
    channel: SlackChannel


class OpenedId(Model):
    id: str


class Opened(Ok):
    no_op: bool
    already_open: bool
    channel: SlackChannel | OpenedId


class Members(Ok):
    members: list[str]
    response_metadata: ResponseMetadata


class MessageList(Ok):
    messages: list[SlackMessage]
    has_more: bool
    pin_count: int = 0
    response_metadata: ResponseMetadata


class Posted(Ok):
    channel: str
    ts: str
    message: SlackMessage


class Updated(Ok):
    channel: str
    ts: str
    text: str
    message: SlackMessage


class Deleted(Ok):
    channel: str
    ts: str


Response = Ok | Failed


def respond(response: Response) -> bytes:
    return response.model_dump_json(exclude_none=True).encode()


# --------------------------------------------------------------------------- events API


EventChannelType = Literal["im", "mpim", "channel", "group"]
"""How the Events API names a conversation's type."""


class MessageEvent(Model):
    type: Literal["message"] = "message"
    channel: str
    user: str
    text: str
    ts: str
    event_ts: str
    channel_type: EventChannelType
    team: str
    thread_ts: str | None = None


class Authorization(Model):
    team_id: str
    user_id: str
    is_bot: bool = True
    is_enterprise_install: bool = False


class EventCallback(Model):
    type: Literal["event_callback"] = "event_callback"
    team_id: str
    api_app_id: str
    event: MessageEvent
    event_id: str
    event_time: int
    authorizations: list[Authorization]
    is_ext_shared_channel: bool = False


def event_body(callback: EventCallback) -> bytes:
    return callback.model_dump_json(exclude_none=True).encode()
