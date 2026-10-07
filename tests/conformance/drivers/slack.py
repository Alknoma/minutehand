"""Slack, driven through its Web API (https://docs.slack.dev/reference/methods) as an app's client calls it: every
method at `https://slack.com/api/<method>`, the token as a bearer in `Authorization`, reads form-encoded, writes as
JSON, and every answer read as Slack documents it: `{"ok": true, ...}`, or `{"ok": false, "error": <code>}`, which
this driver raises as `SlackRefused` naming the method and the code.

A world is one workspace, its own by `tag`: the bot token `xoxb-<tag>`, and for each person a user token
`xoxp-<tag>-<key>` (Slack's user token, which acts as the person who authorized it:
https://docs.slack.dev/authentication/tokens). Every token is declared as a Slack sign-in (the person's own for a
user token), so the workspace answers exactly these and refuses any other as `invalid_auth`. The workspace itself
lists no tokens (`WorkspaceSeed.tokens`): one that lists them takes only those, which would refuse every sign-in.
"""

from __future__ import annotations

import json
import re
import ssl
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import httpx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from minutehand.adapters.control.wire import Claims, CreateWorld, WorldView
from minutehand.domain.scenario import Seed
from tests.conformance.contract import (
    STAMP_CHANNEL,
    Api,
    ChannelSeen,
    DeclaredId,
    Driver,
    FaultCase,
    IdKind,
    MessageSeen,
    Messaging,
    PersonSeen,
    Session,
    ok,
)

PROVIDER = "slack"
API = "https://slack.com/api/"
NAMED_CHANNELS = "public_channel,private_channel"
"""`conversations.list` `types` for every named channel; IMs and group DMs are left out."""
LISTING = 200
"""The page size this driver lists with, where it reads everything: Slack recommends no more than 200."""
UNSEEDED = "xoxb-0000000000000-0000000000000-unseededtoken00000000"
"""A bot token of Slack's shape that no world's seed names."""

Doc = Mapping[str, Any]
"""A JSON object as Slack answered it, read only inside this driver."""


class SlackRefused(Exception):
    """Slack answered `{"ok": false}`: the method and its error code."""

    def __init__(self, method: str, error: str, response: httpx.Response) -> None:
        super().__init__(f"{method}: {error} ({response.status_code} {response.text[:300]})")
        self.method = method
        self.error = error


def bot_token(tag: str) -> str:
    return f"xoxb-{tag}"


def user_token(tag: str, person: str) -> str:
    return f"xoxp-{tag}-{person}"


def _text(field: object) -> str:
    """A channel's `topic` or `purpose` object's `value`."""
    if isinstance(field, Mapping) and "value" in field:
        value = field["value"]
        return value if isinstance(value, str) else ""
    return ""


TS = re.compile(r"^(\d+)\.(\d{6})$")


def at_of(ts: str) -> datetime:
    """A message `ts` as UTC: its epoch seconds. The six-digit fraction is what makes a `ts` unique in its channel
    ("essentially the ID of the message": https://docs.slack.dev/messaging/retrieving-messages), no part of the time.
    Anything not of that shape is refused."""
    found = TS.match(ts)
    if found is None:
        raise ValueError(f"{ts!r} is not a Slack ts")
    return datetime.fromtimestamp(int(found.group(1)), UTC)


class SlackSession(Messaging):
    def __init__(self, api: Api, token: str) -> None:
        self._token = token
        self._http = api.http({"Authorization": f"Bearer {token}"}, base_url=API)
        self.secrets = (token, UNSEEDED)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ the Web API

    def _answer(self, method: str, response: httpx.Response) -> Doc:
        ok(method, response)
        body = response.json()
        if not isinstance(body, dict):
            raise SlackRefused(method, "answer is not a JSON object", response)
        if body["ok"] is not True:
            raise SlackRefused(method, str(body["error"]) if "error" in body else "no error code", response)
        return body

    def read(self, method: str, **arguments: str | int) -> Doc:
        """A read method, form-encoded as Slack documents every method accepts."""
        return self._answer(method, self._http.post(method, data={k: str(v) for k, v in arguments.items()}))

    def write(self, method: str, body: Mapping[str, object]) -> Doc:
        """A write method, as JSON (https://docs.slack.dev/apis/web-api/#posting-json)."""
        return self._answer(method, self._http.post(method, json=dict(body)))

    def _pages(self, method: str, key: str, **arguments: str | int) -> list[list[Doc]]:
        """Every page of a cursor-paged method (https://docs.slack.dev/apis/web-api/pagination)."""
        pages: list[list[Doc]] = []
        cursor = ""
        while True:
            answered = self.read(method, **arguments, **({"cursor": cursor} if cursor else {}))
            pages.append(list(answered[key]))
            meta = answered["response_metadata"] if "response_metadata" in answered else {}
            cursor = meta["next_cursor"] if "next_cursor" in meta else ""
            if not cursor:
                return pages

    def auth(self) -> Doc:
        return self.read("auth.test")

    # ------------------------------------------------------------------ accounts

    def whoami(self) -> str:
        return str(self.auth()["user"])

    def _members(self) -> list[Doc]:
        return [m for page in self._pages("users.list", "members", limit=LISTING) for m in page]

    def _emails(self) -> dict[str, str | None]:
        return {str(m["id"]): _email(m) for m in self._members()}

    def people(self) -> list[PersonSeen]:
        return [
            PersonSeen(
                id=str(m["id"]),
                name=str(m["profile"]["real_name"]),
                email=_email(m),
                active=m["deleted"] is not True,
                bot=m["is_bot"] is True,
                guest=_flag(m, "is_restricted") or _flag(m, "is_ultra_restricted"),
                title=(str(m["profile"]["title"]) or None) if "title" in m["profile"] else None,
            )
            for m in self._members()
        ]

    def people_pages(self, page_size: int) -> list[list[str]]:
        return [[str(m["id"]) for m in page] for page in self._pages("users.list", "members", limit=page_size)]

    def unknown_credential(self) -> httpx.Response:
        return self._http.post("auth.test", headers={"Authorization": f"Bearer {UNSEEDED}"})

    def observe(self) -> str:
        launch = self.channel(STAMP_CHANNEL)
        answered: list[str] = []
        for method, arguments in (
            ("users.list", {"limit": str(LISTING)}),
            ("conversations.list", {"types": NAMED_CHANNELS, "limit": str(LISTING)}),
            ("conversations.history", {"channel": launch, "limit": str(LISTING)}),
        ):
            response = self._http.post(method, data=arguments)
            self._answer(method, response)
            answered.append(response.text)
        return "\n".join(answered)

    def change(self, label: str) -> None:
        self.post(self.channel(STAMP_CHANNEL), label)

    # ------------------------------------------------------------------ conversations

    def _channels(self) -> list[Doc]:
        return [
            c
            for page in self._pages("conversations.list", "channels", types=NAMED_CHANNELS, limit=LISTING)
            for c in page
        ]

    def channel(self, name: str) -> str:
        found = [str(c["id"]) for c in self._channels() if "name" in c and c["name"] == name]
        if len(found) != 1:
            raise LookupError(f"conversations.list answered {len(found)} channels named {name!r}")
        return found[0]

    def direct(self, person: str) -> str:
        return str(self.write("conversations.open", {"users": person})["channel"]["id"])

    def channels(self) -> list[ChannelSeen]:
        emails = self._emails()
        found: list[ChannelSeen] = []
        for c in self._channels():
            members = [
                str(m)
                for page in self._pages("conversations.members", "members", channel=str(c["id"]), limit=LISTING)
                for m in page
            ]
            found.append(
                ChannelSeen(
                    id=str(c["id"]),
                    name=str(c["name"]) if "name" in c else None,
                    private=_flag(c, "is_private"),
                    archived=_flag(c, "is_archived"),
                    topic=_text(c["topic"]) if "topic" in c else "",
                    purpose=_text(c["purpose"]) if "purpose" in c else "",
                    member_emails=frozenset(e for m in members if (e := emails[m] if m in emails else None)),
                )
            )
        return found

    def post(self, channel: str, text: str) -> str:
        return str(self.write("chat.postMessage", {"channel": channel, "text": text})["ts"])

    def post_button(self, channel: str, text: str, action_id: str, label: str) -> str:
        button = {"type": "button", "action_id": action_id, "text": {"type": "plain_text", "text": label}}
        blocks = [{"type": "actions", "elements": [button]}]
        return str(self.write("chat.postMessage", {"channel": channel, "text": text, "blocks": blocks})["ts"])

    def reply(self, channel: str, thread: str, text: str) -> str:
        return str(self.write("chat.postMessage", {"channel": channel, "text": text, "thread_ts": thread})["ts"])

    def edit(self, channel: str, message: str, text: str) -> None:
        self.write("chat.update", {"channel": channel, "ts": message, "text": text})

    def react(self, channel: str, message: str, reaction: str) -> None:
        self.write("reactions.add", {"channel": channel, "timestamp": message, "name": reaction})

    def delete_message(self, channel: str, message: str) -> None:
        self.write("chat.delete", {"channel": channel, "ts": message})

    def history(self, channel: str) -> list[MessageSeen]:
        """Roots from `conversations.history`, each thread's replies from `conversations.replies` (history holds
        only roots: https://docs.slack.dev/messaging/retrieving-messages), oldest first by `ts`."""
        emails = self._emails()
        roots = [
            m for page in self._pages("conversations.history", "messages", channel=channel, limit=LISTING) for m in page
        ]
        every = list(roots)
        for root in roots:
            if "reply_count" in root and int(root["reply_count"]) > 0:
                thread = [
                    m
                    for page in self._pages(
                        "conversations.replies", "messages", channel=channel, ts=str(root["ts"]), limit=LISTING
                    )
                    for m in page
                ]
                every += [m for m in thread if m["ts"] != root["ts"]]
        every.sort(key=lambda m: Decimal(str(m["ts"])))
        return [_message(m, emails) for m in every]

    def history_pages(self, channel: str, page_size: int) -> list[list[str]]:
        return [
            [str(m["ts"]) for m in page]
            for page in self._pages("conversations.history", "messages", channel=channel, limit=page_size)
        ]


def _flag(found: Doc, key: str) -> bool:
    return key in found and found[key] is True


def _email(member: Doc) -> str | None:
    profile = member["profile"]
    email = profile["email"] if "email" in profile else None
    return str(email) if email else None


def _message(m: Doc, emails: Mapping[str, str | None]) -> MessageSeen:
    ts = str(m["ts"])
    user = str(m["user"]) if "user" in m else None
    parent = str(m["thread_ts"]) if "thread_ts" in m and m["thread_ts"] != ts else None
    return MessageSeen(
        id=ts,
        text=str(m["text"]),
        author_email=emails[user] if user is not None and user in emails else None,
        thread_of=parent,
        at=at_of(ts),
        reactions=tuple(str(r["name"]) for r in m["reactions"]) if "reactions" in m else (),
        files=tuple(str(f["name"]) for f in m["files"]) if "files" in m else (),
    )


def _slack(session: Session) -> SlackSession:
    if not isinstance(session, SlackSession):
        raise TypeError(f"the Slack driver reads a SlackSession, not {type(session).__name__}")
    return session


# ---------------------------------------------------------------------------------------------- faults


def _sdk(api: Api, world: WorldView) -> WebClient:
    """slack_sdk as a service runs it: the proxy and the CA bundle the server hands out, the world's bot token."""
    return WebClient(token=world.claims.tokens[0], proxy=api.proxy, ssl=ssl.create_default_context(cafile=api.bundle))


def _raised(call: Callable[[], object]) -> BaseException | None:
    try:
        call()
    except SlackApiError as refused:
        return refused
    return None


def _launch(sdk: WebClient) -> str:
    listed = sdk.conversations_list(types=NAMED_CHANNELS, limit=LISTING).data
    if not isinstance(listed, dict):
        raise TypeError("conversations.list answered no JSON object")
    return next(str(c["id"]) for c in listed["channels"] if c["name"] == STAMP_CHANNEL)


CARD = [{"type": "section", "text": {"type": "mrkdwn", "text": "*A card*"}}]


def _rich_post(api: Api, world: WorldView) -> None:
    sdk = _sdk(api, world)
    sdk.chat_postMessage(channel=_launch(sdk), text="A card", blocks=CARD)


def _rich_post_raised(api: Api, world: WorldView) -> BaseException | None:
    sdk = _sdk(api, world)
    channel = _launch(sdk)
    return _raised(lambda: sdk.chat_postMessage(channel=channel, text="A card", blocks=CARD))


def _fault(call: str, answer: Mapping[str, object], **more: object) -> Mapping[str, object]:
    return {"faults": [{"call": call, "answer": dict(answer), **more}]}


# ---------------------------------------------------------------------------------------------- the driver


class SlackDriver(Driver):
    provider: ClassVar[str] = PROVIDER
    session: ClassVar[type[Session]] = SlackSession
    absent: ClassVar[Mapping[str, str]] = {
        "accounts.vendor_login": "Slack retired usernames: the user object's `name` is documented as 'Don't use "
        "this. It once indicated the preferred username for a user, but that behavior has fundamentally changed "
        "since' (https://docs.slack.dev/reference/objects/user-object); an account is its member id and display name",
    }
    id_formats: ClassVar[Mapping[IdKind, re.Pattern[str]]] = {
        # A member id is U (or W on Enterprise Grid) and uppercase letters and digits: Slack's user object
        # (https://docs.slack.dev/reference/objects/user-object), the seed's `bot_user_id` pattern, and Slackbot's
        # `USLACKBOT` (CLAIMS.md).
        IdKind.PERSON: re.compile(r"^[UW][A-Z0-9]{2,}$"),
        # A conversation id is C (channel), G (private channel or group DM) or D (IM), then uppercase letters and
        # digits: the conversation object (https://docs.slack.dev/reference/objects/conversation-object), and
        # CLAIMS.md ("an IM (`D…`) id").
        IdKind.CHANNEL: re.compile(r"^[CGD][A-Z0-9]{2,}$"),
        # A message is named by its `ts`, epoch seconds and a six-digit fraction, unique in its channel
        # (https://docs.slack.dev/messaging/retrieving-messages, https://docs.slack.dev/reference/methods/chat.postMessage).
        IdKind.MESSAGE: re.compile(r"^\d{10}\.\d{6}$"),
    }
    page_floor: ClassVar[Mapping[str, int]] = {
        # `limit` is "the maximum number of items to return", with no smaller bound documented
        # (https://docs.slack.dev/reference/methods/users.list, .../conversations.history).
        "people": 1,
        "history": 1,
    }
    unknown_refusal: ClassVar[tuple[int, str]] = (200, "invalid_auth")
    """Slack answers every refusal but a rate limit with HTTP 200 and `{"ok": false, "error": "invalid_auth"}` for a
    token it never issued (https://docs.slack.dev/reference/methods/auth.test)."""

    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        if logins:
            raise NotImplementedError("Slack has no logins (accounts.vendor_login is declared absent)")
        people = [str(p["key"]) for p in _objects(seed, "people")]
        ours = [
            {"provider": PROVIDER, "credential": bot_token(tag)},
            *({"provider": PROVIDER, "credential": user_token(tag, k), "person": k} for k in people),
        ]
        given = seed["sign_ins"] if "sign_ins" in seed else []
        if not isinstance(given, list):
            raise TypeError("the seed's sign_ins is not a list")
        theirs = [s for s in _objects(seed, "sign_ins") if s["provider"] == PROVIDER]
        provider_seeds = _objects(seed, "provider_seeds")
        slack = next((s for s in provider_seeds if s["provider"] == PROVIDER), None)
        body: dict[str, Any] = {} if slack is None else _body(slack["body"])
        workspaces: list[dict[str, Any]] = [dict(w) for w in body["workspaces"]] if "workspaces" in body else [{}]
        first = {"team_id": f"T{tag.upper()}", "domain": tag, **workspaces[0]}
        body["workspaces"] = [first, *workspaces[1:]]
        merged = dict(seed) | {
            "sign_ins": [*given, *ours],
            "provider_seeds": [
                *(s for s in provider_seeds if s["provider"] != PROVIDER),
                {"provider": PROVIDER, "body": json.dumps(body)},
            ],
        }
        tokens = [bot_token(tag), *(user_token(tag, k) for k in people), *(str(s["credential"]) for s in theirs)]
        return CreateWorld(seed=Seed.model_validate(merged), claims=Claims(tokens=tokens))

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        credential = self.credential_of(world, person)
        assert credential is not None
        return SlackSession(api, credential)

    def signed_in(self, api: Api, world: WorldView, credential: str) -> Session:
        return SlackSession(api, credential)

    def sign_in_credential(self, unique: str) -> str:
        """A user token, the shape Slack issues to a person who installs an app (`xoxp-`)."""
        return f"xoxp-{unique}"

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        """The bot token (the world's first claim, by `world`), or the person's user token."""
        bot = world.claims.tokens[0]
        return bot if person is None else user_token(bot.removeprefix("xoxb-"), person)

    def faults(self) -> list[FaultCase]:
        return [
            FaultCase(
                name="ratelimited",
                fragment=_fault("users.list", {"kind": "rate_limited", "retry_after": "PT30S"}),
                trigger=lambda api, world: _raised(lambda: _sdk(api, world).users_list(limit=LISTING)),
                typed=SlackApiError,
                status=429,
                holds="ratelimited",
                then=lambda api, world: _ignore(_sdk(api, world).users_list(limit=LISTING)),
            ),
            FaultCase(
                name="refused",
                fragment=_fault("auth.test", {"kind": "refused", "error": "token_revoked"}),
                trigger=lambda api, world: _raised(lambda: _sdk(api, world).auth_test()),
                typed=SlackApiError,
                status=200,
                holds="token_revoked",
                then=lambda api, world: _ignore(_sdk(api, world).auth_test()),
            ),
            FaultCase(
                name="refused_twice",
                fragment=_fault("conversations.list", {"kind": "refused", "error": "missing_scope"}, times=2),
                trigger=lambda api, world: _raised(lambda: _sdk(api, world).conversations_list(types=NAMED_CHANNELS)),
                typed=SlackApiError,
                status=200,
                holds="missing_scope",
                uses=2,
                then=lambda api, world: _ignore(_sdk(api, world).conversations_list(types=NAMED_CHANNELS)),
            ),
            FaultCase(
                name="only_rich",
                fragment=_fault("chat.postMessage", {"kind": "refused", "error": "invalid_blocks"}, only_rich=True),
                trigger=_rich_post_raised,
                typed=SlackApiError,
                status=200,
                holds="invalid_blocks",
                then=_rich_post,
            ),
        ]

    def declared_ids(self) -> list[DeclaredId]:
        return [
            DeclaredId(
                name=field,
                seed={"provider_seeds": [{"provider": PROVIDER, "body": {"workspaces": [{field: value}]}}]},
                read=lambda session, answered=answered: str(_slack(session).auth()[answered]),
                expected=value,
            )
            for field, value, answered in (
                ("team_id", "TDECLARED7", "team_id"),
                ("bot_user_id", "UDECLARED7", "user_id"),
                ("bot_id", "BDECLARED7", "bot_id"),
            )
        ]


def _ignore(_: object) -> None:
    return None


def _objects(seed: Mapping[str, object], key: str) -> list[Mapping[str, Any]]:
    found = seed[key] if key in seed else []
    if not isinstance(found, list):
        raise TypeError(f"the seed's {key} is not a list")
    return [x for x in found if isinstance(x, Mapping)]


def _body(body: object) -> dict[str, Any]:
    """A provider seed's body, given as structure or as its JSON text."""
    parsed = json.loads(body) if isinstance(body, str) else body
    if not isinstance(parsed, dict):
        raise TypeError("the Slack seed is not a JSON object")
    return dict(parsed)


DRIVER = SlackDriver()
