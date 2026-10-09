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
- **Errors** — `Refusal`, the `ServiceRefusal` Slack answers with, and `error_answer`,
  what Minutehand answers in Slack's place.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeVar
from urllib.parse import parse_qsl, urlencode

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from minutehand.domain.errors import Asked, Rendered, ServiceRefusal
from minutehand.domain.scenario import Model

TRUNCATED_AT = 40_000
"""`chat.postMessage` keeps this many characters of `text` and drops the rest; it never refuses a long one."""
MAX_UPDATE_CHARS = 4_000
"""`chat.update` refuses `text` past this with `msg_too_long`."""
MAX_EPHEMERAL_CHARS = 40_000
"""`chat.postEphemeral` lists `msg_too_long` without naming a figure; the post's own ceiling stands in for one."""
MAX_BLOCKS = 50
PAGE_MAX = 1000
"""Pagination's ceiling where a method's page names none: "The `limit` parameter maximum is `1000`"
(https://docs.slack.dev/apis/web-api/pagination)."""


JSON = "application/json; charset=utf-8"
"""The content type of every Web API answer, refusals included."""


class UserRefused(Model):
    """One user a call could not act on and why, as `conversations.invite` lists them in `errors`."""

    user: str
    ok: Literal[False] = False
    error: str


class Refusal(ServiceRefusal):
    """Slack answered `ok: false`. `error` is Slack's own code; `errors` the per-user refusals a call that takes a
    list of users adds beside it (https://docs.slack.dev/reference/methods/conversations.invite)."""

    def __init__(self, error: str, errors: list[UserRefused] | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.errors = errors

    def answer(self) -> Failed:
        return FailedForUsers(error=self.error, errors=self.errors) if self.errors else Failed(error=self.error)

    def render(self, asked: Asked) -> Rendered:
        """`{"ok": false, "error": …}` at 200, as the Web API refuses."""
        return Rendered(status=200, content_type=JSON, body=respond(self.answer()))


class ErrorMessages(Model):
    messages: list[str]


class ErrorAnswer(Model):
    """An error with words beside its code, as Slack sends `invalid_arguments` with what was wrong."""

    ok: Literal[False] = False
    error: str
    response_metadata: ErrorMessages


def error_answer(status: int, code: str, message: str) -> Rendered:
    """What Minutehand answers in Slack's place (501, 500), as the Web API words an error: `error` the code, and
    `message` in `response_metadata.messages`, where Slack puts what it says beside a code. `slack_sdk` raises
    `SlackApiError` for it, the whole body on `.response`."""
    body = ErrorAnswer(error=code, response_metadata=ErrorMessages(messages=[message]))
    return Rendered(status=status, content_type=JSON, body=body.model_dump_json().encode())


# --------------------------------------------------------------------------- stored


class SlackProfile(Model):
    real_name: str
    display_name: str
    email: str | None = None
    title: str = ""
    bot_id: str | None = None
    status_text: str | None = None
    status_emoji: str | None = None
    status_expiration: int | None = None


class SlackUser(Model):
    id: str
    team_id: str
    name: str
    real_name: str
    deleted: bool = False
    is_admin: bool = False
    is_owner: bool = False
    is_restricted: bool = Field(default=False, description="A guest")
    is_ultra_restricted: bool = Field(default=False, description="A single-channel guest")
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
    num_members: int | None = Field(default=None, description="Its member count; set when `include_num_members` asks")
    previous_names: list[str] = Field(default=[], description="Every name a rename replaced, oldest first")
    unlinked: int | None = Field(default=None, description="Computed when served as a full conversation object")
    name_normalized: str | None = Field(default=None, description="Computed when served in full")
    is_shared: bool | None = Field(default=None, description="Computed when served in full")
    is_frozen: bool | None = Field(default=None, description="Computed when served in full")
    is_org_shared: bool | None = Field(default=None, description="Computed when served in full")
    is_pending_ext_shared: bool | None = Field(default=None, description="Computed when served in full")
    pending_shared: list[str] | None = Field(default=None, description="Computed when served in full")
    context_team_id: str | None = Field(default=None, description="Computed when served in full")
    is_ext_shared: bool | None = Field(default=None, description="Computed when served in full")
    shared_team_ids: list[str] | None = Field(default=None, description="Computed when served in full")
    pending_connected_team_ids: list[str] | None = Field(default=None, description="Computed when served in full")


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


class BotIcons(Model):
    image_36: str
    image_48: str
    image_72: str


class BotProfile(Model):
    """Who a bot's message is from, as Slack attaches it to every message an app posts."""

    id: str
    app_id: str
    name: str
    icons: BotIcons
    deleted: bool = False
    updated: int
    team_id: str


class SlackFile(Model):
    """A file as the Web API and the Events API serve it. Its content is stored apart (`SlackFileContent`)."""

    id: str
    created: int
    timestamp: int
    name: str
    title: str
    mimetype: str
    filetype: str
    pretty_type: str
    user: str
    user_team: str
    size: int
    mode: Literal["hosted"] = "hosted"
    is_external: bool = False
    is_public: bool
    url_private: str
    url_private_download: str
    permalink: str


class SlackFileContent(Model):
    """Minutehand's own: what a file holds, served only at its `url_private`."""

    file: str
    mimetype: str
    text: str


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
    subtype: Literal["file_share", "thread_broadcast"] | None = None
    root: SlackMessage | None = Field(
        default=None, description="A broadcast reply's thread parent, as Slack serves it beside the broadcast"
    )
    client_msg_id: str | None = None
    bot_profile: BotProfile | None = None
    files: list[SlackFile] | None = None
    upload: bool | None = None
    ephemeral_to: str | None = Field(
        default=None,
        description="Minutehand's own: the one member an ephemeral message was shown to. Never served, because "
        "Slack never lists an ephemeral message",
    )
    reply_count: int | None = Field(default=None, description="Thread summary; computed when served, never stored")
    reply_users: list[str] | None = None
    reply_users_count: int | None = None
    latest_reply: str | None = None
    parent_user_id: str | None = Field(
        default=None, description="A reply's thread parent's author; computed when served"
    )


class SlackPostKey(Model):
    """Minutehand's own: where the post a scenario names by key landed."""

    key: str
    channel: str
    ts: str


class TextObject(Model):
    type: Literal["plain_text", "mrkdwn"] = "plain_text"
    text: str
    emoji: bool | None = None


class ViewInputValue(Model):
    type: str
    value: str | None = None


class ViewState(Model):
    values: dict[str, dict[str, ViewInputValue]] = {}


class SlackView(Model):
    """A modal or a Home tab, as `views.*` answer it and as an interaction payload carries it."""

    id: str
    team_id: str
    type: Literal["modal", "home"]
    title: TextObject | None = None
    submit: TextObject | None = None
    close: TextObject | None = None
    blocks: list[JsonValue]
    private_metadata: str = ""
    callback_id: str = ""
    external_id: str = ""
    state: ViewState
    hash: str
    clear_on_close: bool = False
    notify_on_close: bool = False
    submit_disabled: bool = False
    previous_view_id: str | None = None
    root_view_id: str
    app_id: str
    app_installed_team_id: str
    bot_id: str


class OpenView(Model):
    """Minutehand's own: a view and who it is shown to, the press that opened it, and whether it is still open.
    Stored as the view entity's body; the view is served without the rest."""

    view: SlackView
    user: str
    trigger_id: str | None = Field(default=None, description="The trigger it was opened with; a Home tab has none")
    open: bool = True
    errors: dict[str, str] = {}


class SlackTrigger(Model):
    """Minutehand's own: a `trigger_id` handed to the agent with a press, usable once to open a view."""

    id: str
    user: str
    issued: int = Field(description="Simulated seconds since the epoch")
    on_channel: str | None = None
    on_message: str | None = None
    view: str | None = Field(default=None, description="The view opened with it; set once used")


class SlackHook(Model):
    """Minutehand's own: a `response_url`, answerable five times within thirty minutes."""

    id: str
    secret: str
    user: str
    channel: str
    message: str | None = Field(default=None, description="The ts of the message pressed on; none for a command")
    issued: int = Field(description="Simulated seconds since the epoch")
    used: int = 0


class SlackInstall(Model):
    """Minutehand's own: who installed the agent's app, the OAuth codes already exchanged, and the bot tokens the
    exchanges minted, which authenticate in this workspace from then on."""

    installer: str
    exchanged: list[str] = []
    tokens: list[str] = []


class SlackWorkspace(Model):
    """Minutehand's own: one workspace the agent's app is installed in, and who it is there."""

    id: str
    name: str
    domain: str
    bot_user_id: str
    bot_id: str
    app_id: str
    bot_name: str
    tokens: list[str] = Field(
        default=[], description="The tokens that select it; any other token is answered in the first workspace"
    )
    oauth_code: str | None = Field(default=None, description="The install code `oauth.v2.access` answers it for")
    position: int = Field(default=0, description="Its place among the world's workspaces; the first is 0")


class SlackAwayStretch(Model):
    """Minutehand's own: one of a person's absences, as the scenario gives it, in seconds."""

    on_first_ask: bool
    starts_after: int
    lasts: int
    reason: str | None = None


class SlackAway(Model):
    """Minutehand's own: a person's absences, read against the run's clock whenever their profile is."""

    user: str
    email: str
    starts_at: int = Field(description="The scenario's start, epoch seconds")
    stretches: list[SlackAwayStretch]


class SlackUnlistedEmail(Model):
    """Minutehand's own: the email of a member whose profile shows none, so the world still knows whom a message
    reached."""

    user: str
    email: str


class SlackFault(Model):
    """Minutehand's own: a call the scenario fails on purpose, and how many more times."""

    position: int
    call: str | None = None
    error: str
    retry_after: int | None = Field(default=None, description="Seconds; set only for a rate limit")
    remaining: int | None = Field(default=None, description="None: every call")
    from_time: int = Field(description="Simulated seconds since the epoch from which it applies")
    only_rich: bool = False


StoredModel = TypeVar("StoredModel", bound=Model)
"""Any of the stored bodies above."""


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


class ChannelInfoArgs(ChannelArgs):
    include_num_members: bool = False


class CreateArgs(Model):
    name: str = ""
    is_private: bool = False


class InviteArgs(ChannelArgs):
    users: str = ""
    force: bool = False


class KickArgs(ChannelArgs):
    user: str = ""


class RenameArgs(ChannelArgs):
    name: str = ""


class TopicArgs(ChannelArgs):
    topic: str | None = None


class PurposeArgs(ChannelArgs):
    purpose: str | None = None


class UsersConversationsArgs(ListArgs):
    types: str = "public_channel"
    user: str = ""
    exclude_archived: bool = False
    exclude_muted: bool = False
    team_id: str = ""


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
    reply_broadcast: bool = False
    blocks: list[JsonValue] | None = None
    attachments: list[JsonValue] | None = None
    as_user: bool = False

    _parse_blocks = field_validator("blocks", "attachments", mode="before")(_blocks_from_form)


class PostEphemeralArgs(Model):
    channel: str = ""
    user: str = ""
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


def _view_from_form(value: object) -> object:
    """A form-encoded body carries `view` as JSON text; a JSON body carries it as an object."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("not JSON") from error
    return value


class ViewSpec(Model):
    """A view as the agent writes it: Slack refuses a field it does not know."""

    type: Literal["modal", "home"]
    title: TextObject | None = None
    submit: TextObject | None = None
    close: TextObject | None = None
    blocks: list[JsonValue]
    private_metadata: str = ""
    callback_id: str = ""
    external_id: str = ""
    clear_on_close: bool = False
    notify_on_close: bool = False
    submit_disabled: bool = False


class ViewsOpenArgs(Model):
    trigger_id: str = ""
    view: ViewSpec | None = None

    _parse_view = field_validator("view", mode="before")(_view_from_form)


class ViewsUpdateArgs(Model):
    view_id: str = ""
    external_id: str = ""
    hash: str = ""
    view: ViewSpec | None = None

    _parse_view = field_validator("view", mode="before")(_view_from_form)


class ViewsPublishArgs(Model):
    user_id: str = ""
    hash: str = ""
    view: ViewSpec | None = None

    _parse_view = field_validator("view", mode="before")(_view_from_form)


class OAuthArgs(Model):
    code: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    grant_type: str = ""


MAX_VIEW_BLOCKS = 100
MAX_TITLE_CHARS = 24
MAX_METADATA_CHARS = 3000


def check_view(view: ViewSpec) -> None:
    """Refuse what Slack refuses about a view, with Slack's own code."""
    if view.type == "modal" and view.title is None:  # enum-lint: exempt Slack's own view type on the wire
        raise Refusal("invalid_arguments")
    if view.title is not None and len(view.title.text) > MAX_TITLE_CHARS:
        raise Refusal("invalid_arguments")
    if len(view.private_metadata) > MAX_METADATA_CHARS:
        raise Refusal("invalid_arguments")
    if len(view.blocks) > MAX_VIEW_BLOCKS:
        raise Refusal("invalid_arguments")
    for block in view.blocks:
        if not isinstance(block, dict) or "type" not in block or not isinstance(block["type"], str):
            raise Refusal("invalid_arguments")


Args = TypeVar("Args", bound=Model)


class Presented(Model):
    """What one call to the Web API presented: its arguments as Slack reads them, and its token."""

    token: str | None
    arguments: dict[str, JsonValue] = Field(description="Every argument from the query, form or JSON body")

    @property
    def rich(self) -> bool:
        """Whether the call carries blocks or attachments: what a fault on rich content fails."""
        return any(
            name in self.arguments and self.arguments[name] not in (None, "", "[]", [])
            for name in ("blocks", "attachments")
        )


def read_call(query: str, content_type: str, body: bytes, authorization: str | None) -> Presented:
    """Merge the query string with the body the way Slack does, and find the token.

    Slack reads a bearer token from `Authorization`, or a `token` argument. The token only picks the workspace
    (`SlackWorld.for_token`); nothing refuses it.
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


def page_size(limit: int, *, default: int | None, most: int = PAGE_MAX) -> int | None:
    """A method's page size: its documented default when `limit` is not given, its documented maximum past that.
    Slack adjusts an out-of-range limit rather than refusing it: "Invalid `limit` values are currently magically
    adjusted to something sensible" (https://docs.slack.dev/apis/web-api/pagination). None: every item, as
    `users.list` answers with no limit."""
    return default if limit <= 0 else min(limit, most)


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


def check_message(text: str, blocks: list[JsonValue] | None, *, refused_past: int | None) -> None:
    """Refuse what Slack refuses about a message's body, with Slack's own code. `refused_past` is the method's
    `msg_too_long` ceiling; None for a method that truncates instead of refusing."""
    if refused_past is not None and len(text) > refused_past:
        raise Refusal("msg_too_long")
    if blocks is None:
        return
    if len(blocks) > MAX_BLOCKS:
        raise Refusal("invalid_blocks")
    for block in blocks:
        if not isinstance(block, dict) or "type" not in block or not isinstance(block["type"], str):
            raise Refusal("invalid_blocks")


class _Foreign(BaseModel):
    """Part of a block the agent wrote: read for what Minutehand needs, everything else left as written."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class _Label(_Foreign):
    text: str = ""


class _Element(_Foreign):
    type: str
    action_id: str | None = None
    text: _Label | None = None
    placeholder: _Label | None = None
    value: str | None = None
    url: str | None = None
    style: str | None = None
    initial_user: str | None = None
    multiline: bool = False


class _Block(_Foreign):
    type: str
    block_id: str | None = None
    elements: list[JsonValue] = []
    accessory: JsonValue = None
    element: JsonValue = None
    label: _Label | None = None
    optional: bool = False


class Control(Model):
    """A button or a person picker on a message, with the block it sits in."""

    block_id: str
    action_id: str
    type: Literal["button", "users_select"]
    label: str
    value: str | None = None
    url: str | None = None
    style: str | None = None
    initial_user: str | None = None

    @property
    def label_free(self) -> bool:
        """A person picker sends no `text` with its action; a button sends its label."""
        return self.type == "users_select"  # enum-lint: exempt Slack's own block element type


class InputField(Model):
    """A text field of a view: the input block and the element in it."""

    block_id: str
    action_id: str
    label: str
    optional: bool
    type: str


def _blocks(blocks: list[JsonValue] | None) -> list[_Block]:
    found: list[_Block] = []
    for raw in blocks or []:
        try:
            found.append(_Block.model_validate(raw))
        except ValidationError:
            continue
    return found


def _element(raw: JsonValue) -> _Element | None:
    try:
        return _Element.model_validate(raw)
    except ValidationError:
        return None


def controls(blocks: list[JsonValue] | None) -> list[Control]:
    """Every button and person picker a reader can use, in order: in `actions` blocks and as section accessories."""
    found: list[Control] = []
    for block in _blocks(blocks):
        elements = block.elements if block.type == "actions" else [block.accessory] if block.type == "section" else []
        for raw in elements:
            element = _element(raw)
            if element is None or element.action_id is None or block.block_id is None:
                continue
            if element.type == "button":  # enum-lint: exempt Slack's own block element type
                found.append(
                    Control(
                        block_id=block.block_id,
                        action_id=element.action_id,
                        type="button",
                        label=element.text.text if element.text is not None else "",
                        value=element.value,
                        url=element.url,
                        style=element.style,
                    )
                )
            elif element.type == "users_select":
                found.append(
                    Control(
                        block_id=block.block_id,
                        action_id=element.action_id,
                        type="users_select",
                        label=element.placeholder.text if element.placeholder is not None else "",
                        initial_user=element.initial_user,
                    )
                )
    return found


def inputs(blocks: list[JsonValue]) -> list[InputField]:
    """Every field a person can type into on a view."""
    found: list[InputField] = []
    for block in _blocks(blocks):
        element = _element(block.element) if block.type == "input" else None
        if element is None or element.action_id is None or block.block_id is None:
            continue
        found.append(
            InputField(
                block_id=block.block_id,
                action_id=element.action_id,
                label=block.label.text if block.label is not None else "",
                optional=block.optional,
                type=element.type,
            )
        )
    return found


def with_ids(blocks: list[JsonValue] | None, seed: str) -> list[JsonValue] | None:
    """The blocks as Slack keeps them: every block given a `block_id` and every interactive element an
    `action_id` when the agent left them out, derived from `seed` and the position so they never change."""
    if blocks is None:
        return None
    kept: list[JsonValue] = []
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            kept.append(block)
            continue
        copy: dict[str, JsonValue] = dict(block)
        if "block_id" not in copy:
            copy["block_id"] = _short(seed, str(i))
        for key in ("elements", "accessory", "element"):
            if key in copy:
                copy[key] = _with_action_ids(copy[key], seed, str(i), key)
        kept.append(copy)
    return kept


_INTERACTIVE = frozenset(
    {
        "button",
        "users_select",
        "static_select",
        "conversations_select",
        "channels_select",
        "overflow",
        "datepicker",
        "timepicker",
        "plain_text_input",
        "checkboxes",
        "radio_buttons",
        "multi_users_select",
    }
)


def _with_action_ids(node: JsonValue, *parts: str) -> JsonValue:
    if isinstance(node, list):
        return [_with_action_ids(item, *parts, str(i)) for i, item in enumerate(node)]
    if isinstance(node, dict):
        kind = node["type"] if "type" in node else None
        if isinstance(kind, str) and kind in _INTERACTIVE and "action_id" not in node:
            return {**node, "action_id": _short(*parts)}
    return node


def _short(*parts: str) -> str:
    return base64.b32encode(hashlib.sha256("\x1f".join(parts).encode()).digest()[:4]).decode().rstrip("=")


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


class FailedForUsers(Failed):
    errors: list[UserRefused]


class UnknownMethod(Failed):
    """What Slack answers a method name it has none for, the name echoed in `req_method` (observed:
    `tests/providers/slack/data/observed/unknown_method.http`)."""

    error: str = "unknown_method"
    req_method: str


class ResponseMetadata(Model):
    next_cursor: str = ""


class ResponseMetadataWarnings(Model):
    warnings: list[str]


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


class Presence(Ok):
    presence: Literal["active", "away"]
    online: bool | None = None
    auto_away: bool | None = None
    manual_away: bool | None = None
    connection_count: int | None = None
    last_activity: int | None = None


class DndInfo(Ok):
    dnd_enabled: bool
    next_dnd_start_ts: int | None = None
    next_dnd_end_ts: int | None = None
    snooze_enabled: bool | None = None
    snooze_endtime: int | None = None
    snooze_remaining: int | None = None


class OneProfile(Ok):
    profile: SlackProfile


class ChannelList(Ok):
    channels: list[SlackChannel]
    response_metadata: ResponseMetadata


class OneChannel(Ok):
    channel: SlackChannel


class Joined(OneChannel):
    """`conversations.join`, which warns when the caller is in the conversation already."""

    warning: str | None = None
    response_metadata: ResponseMetadataWarnings | None = None


class Purposed(Ok):
    purpose: str


class Kicked(Ok):
    errors: dict[str, str] = {}


class NotInChannelNotice(Model):
    """What `conversations.leave` answers a caller who was not in the conversation: `ok` false and no `error`
    (https://docs.slack.dev/reference/methods/conversations.leave)."""

    ok: Literal[False] = False
    not_in_channel: Literal[True] = True


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


class PostedEphemeral(Ok):
    message_ts: str


class Updated(Ok):
    channel: str
    ts: str
    text: str
    message: SlackMessage


class Deleted(Ok):
    channel: str
    ts: str


class ViewAnswered(Ok):
    view: SlackView


class AuthedUser(Model):
    id: str


class TeamRef(Model):
    id: str
    name: str


class OAuthAccess(Ok):
    app_id: str
    authed_user: AuthedUser
    scope: str
    token_type: Literal["bot"] = "bot"
    access_token: str
    bot_user_id: str
    team: TeamRef
    is_enterprise_install: bool = False


class RateLimitedAnswer(Failed):
    """`ratelimited`, which Slack answers with HTTP 429 and `Retry-After`, not 200."""

    retry_after: int = Field(exclude=True)


Response = Ok | Failed | NotInChannelNotice


def respond(response: Response) -> bytes:
    return response.model_dump_json(exclude_none=True).encode()


# --------------------------------------------------------------------------- events API


EventChannelType = Literal["im", "mpim", "channel", "group"]
"""How the Events API names a conversation's type."""


class MessageEvent(Model):
    type: Literal["message"] = "message"
    subtype: Literal["file_share"] | None = None
    channel: str
    user: str
    text: str
    ts: str
    event_ts: str
    channel_type: EventChannelType
    team: str
    client_msg_id: str | None = None
    thread_ts: str | None = None
    files: list[SlackFile] | None = None
    upload: bool | None = None


class AppMentionEvent(Model):
    type: Literal["app_mention"] = "app_mention"
    user: str
    text: str
    ts: str
    channel: str
    event_ts: str
    team: str
    client_msg_id: str | None = None
    thread_ts: str | None = None
    files: list[SlackFile] | None = None


class MessageChangedEvent(Model):
    type: Literal["message"] = "message"
    subtype: Literal["message_changed"] = "message_changed"
    hidden: bool = True
    channel: str
    channel_type: EventChannelType
    ts: str
    event_ts: str
    message: SlackMessage
    previous_message: SlackMessage


class MessageDeletedEvent(Model):
    type: Literal["message"] = "message"
    subtype: Literal["message_deleted"] = "message_deleted"
    hidden: bool = True
    channel: str
    channel_type: EventChannelType
    ts: str
    deleted_ts: str
    event_ts: str
    previous_message: SlackMessage


class ReactionItem(Model):
    type: Literal["message"] = "message"
    channel: str
    ts: str


class ReactionAddedEvent(Model):
    type: Literal["reaction_added"] = "reaction_added"
    user: str
    reaction: str
    item_user: str
    item: ReactionItem
    event_ts: str


class MemberJoinedEvent(Model):
    type: Literal["member_joined_channel"] = "member_joined_channel"
    user: str
    channel: str
    channel_type: Literal["C", "G"]
    team: str
    inviter: str | None = Field(default=None, description="Who added `user`; absent when they joined by themselves")
    event_ts: str


class MemberLeftEvent(Model):
    type: Literal["member_left_channel"] = "member_left_channel"
    user: str
    channel: str
    channel_type: Literal["C", "G"]
    team: str


class CreatedChannel(Model):
    id: str
    name: str
    created: int
    creator: str


class ChannelCreatedEvent(Model):
    type: Literal["channel_created"] = "channel_created"
    channel: CreatedChannel


class RenamedChannel(Model):
    id: str
    name: str
    created: int


class ChannelRenameEvent(Model):
    type: Literal["channel_rename"] = "channel_rename"
    channel: RenamedChannel


class ChannelArchiveEvent(Model):
    type: Literal["channel_archive"] = "channel_archive"
    channel: str
    user: str


class ReactionRemovedEvent(Model):
    type: Literal["reaction_removed"] = "reaction_removed"
    user: str
    reaction: str
    item_user: str
    item: ReactionItem
    event_ts: str


class FileId(Model):
    id: str


class FileSharedEvent(Model):
    type: Literal["file_shared"] = "file_shared"
    channel_id: str
    file_id: str
    user_id: str
    file: FileId
    event_ts: str


class FileDeletedEvent(Model):
    type: Literal["file_deleted"] = "file_deleted"
    file_id: str
    event_ts: str


class AppHomeOpenedEvent(Model):
    type: Literal["app_home_opened"] = "app_home_opened"
    user: str
    channel: str
    tab: Literal["home"] = "home"
    event_ts: str
    view: SlackView | None = None


Event = (
    MessageEvent
    | AppMentionEvent
    | MessageChangedEvent
    | MessageDeletedEvent
    | ReactionAddedEvent
    | MemberJoinedEvent
    | MemberLeftEvent
    | ChannelCreatedEvent
    | ChannelRenameEvent
    | ChannelArchiveEvent
    | ReactionRemovedEvent
    | FileSharedEvent
    | FileDeletedEvent
    | AppHomeOpenedEvent
)


class Authorization(Model):
    team_id: str
    user_id: str
    is_bot: bool = True
    is_enterprise_install: bool = False


class EventCallback(Model):
    type: Literal["event_callback"] = "event_callback"
    team_id: str
    api_app_id: str
    event: Event
    event_id: str
    event_time: int
    authorizations: list[Authorization]
    is_ext_shared_channel: bool = False


def event_body(callback: EventCallback | UrlVerification) -> bytes:
    return callback.model_dump_json(exclude_none=True).encode()


# --------------------------------------------------------------------------- Socket Mode


class ConnectionsOpen(Ok):
    """`apps.connections.open`: where the app opens its Socket Mode connection."""

    url: str


class SocketConnectionInfo(Model):
    app_id: str


class SocketDebugInfo(Model):
    host: str
    approximate_connection_time: int = Field(description="Seconds until Slack asks the app to reconnect")


class SocketHello(Model):
    """What Slack sends first on a Socket Mode connection."""

    type: Literal["hello"] = "hello"
    num_connections: int
    debug_info: SocketDebugInfo
    connection_info: SocketConnectionInfo


class SocketEnvelope(Model):
    """An Events API callback as Socket Mode carries it; the app acknowledges it by its `envelope_id`."""

    envelope_id: str
    payload: EventCallback
    type: Literal["events_api"] = "events_api"
    accepts_response_payload: bool = False
    retry_attempt: int = 0


def envelope_body(envelope: SocketEnvelope) -> str:
    return envelope.model_dump_json(exclude_none=True)


class SocketAck(_Foreign):
    """What the app sends back on its connection to acknowledge an envelope; anything else it carries is its own."""

    envelope_id: str


class UrlVerification(Model):
    token: str
    challenge: str
    type: Literal["url_verification"] = "url_verification"


class Challenged(_Foreign):
    challenge: str


# --------------------------------------------------------------------------- interactivity


VERIFICATION_TOKEN = "minutehandverificationtoken"
"""The deprecated verification token Slack still puts in every payload; Minutehand's is fixed."""


class PayloadUser(Model):
    id: str
    username: str
    name: str
    team_id: str


class PayloadTeam(Model):
    id: str
    domain: str


class PayloadChannel(Model):
    id: str
    name: str


class MessageContainer(Model):
    type: Literal["message"] = "message"
    message_ts: str
    channel_id: str
    is_ephemeral: bool
    thread_ts: str | None = None


class PressedAction(Model):
    type: Literal["button", "users_select"]
    action_id: str
    block_id: str
    action_ts: str
    text: TextObject | None = None
    value: str | None = None
    style: str | None = None
    selected_user: str | None = None
    initial_user: str | None = None


class BlockActions(Model):
    type: Literal["block_actions"] = "block_actions"
    user: PayloadUser
    api_app_id: str
    token: str = VERIFICATION_TOKEN
    container: MessageContainer
    trigger_id: str
    team: PayloadTeam
    is_enterprise_install: bool = False
    channel: PayloadChannel
    message: SlackMessage | None = Field(default=None, description="Absent when the message was ephemeral")
    state: ViewState = ViewState()
    response_url: str
    actions: list[PressedAction]


class ViewSubmission(Model):
    type: Literal["view_submission"] = "view_submission"
    team: PayloadTeam
    user: PayloadUser
    api_app_id: str
    token: str = VERIFICATION_TOKEN
    trigger_id: str
    view: SlackView
    response_urls: list[str] = []
    is_enterprise_install: bool = False


def payload_form(payload: BlockActions | ViewSubmission) -> bytes:
    """An interaction as Slack sends it: form-encoded, its JSON in the one field `payload`."""
    return urlencode({"payload": payload.model_dump_json(exclude_none=True)}).encode()


class ViewAnswer(_Foreign):
    """What the agent may answer a view submission with, synchronously."""

    response_action: Literal["errors", "update", "push", "clear"]
    errors: dict[str, str] = {}
    view: ViewSpec | None = None


def view_answer(body: bytes) -> ViewAnswer | None:
    """None: an empty answer, which closes the view."""
    if not body.strip():
        return None
    decoded = json.loads(body)
    if isinstance(decoded, dict) and not decoded:
        return None
    return ViewAnswer.model_validate(decoded)


class ResponseUrlBody(_Foreign):
    """What the agent posts to a `response_url`."""

    text: str = ""
    blocks: list[JsonValue] | None = None
    attachments: list[JsonValue] | None = None
    response_type: Literal["ephemeral", "in_channel"] | None = None
    replace_original: bool = False
    delete_original: bool = False
    thread_ts: str | None = None


def slash_command_form(
    *,
    team: PayloadTeam,
    channel: PayloadChannel,
    user: PayloadUser,
    command: str,
    text: str,
    api_app_id: str,
    response_url: str,
    trigger_id: str,
) -> bytes:
    """A slash command as Slack sends it: form fields, not JSON."""
    return urlencode(
        {
            "token": VERIFICATION_TOKEN,
            "team_id": team.id,
            "team_domain": team.domain,
            "channel_id": channel.id,
            "channel_name": channel.name,
            "user_id": user.id,
            "user_name": user.username,
            "command": command,
            "text": text,
            "api_app_id": api_app_id,
            "is_enterprise_install": "false",
            "response_url": response_url,
            "trigger_id": trigger_id,
        }
    ).encode()


class CommandAnswer(_Foreign):
    """What the agent may answer a slash command with, synchronously; an empty answer shows nothing."""

    text: str = ""
    blocks: list[JsonValue] | None = None
    response_type: Literal["ephemeral", "in_channel"] = "ephemeral"
