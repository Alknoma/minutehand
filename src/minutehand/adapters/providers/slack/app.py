"""Slack's HTTP surface, as an ASGI app over the run's store and clock.

Every Web API method answers at `/api/<method>`, by GET or POST, with its arguments in the
query string, a form-encoded body or a JSON body, exactly as Slack accepts them. Every
refusal is Slack's own `{"ok": false, "error": ...}` with HTTP 200, except `ratelimited`,
which is HTTP 429 with `Retry-After`, as Slack sends it.

A method Slack has and this fake does not serve (`methods.UNSERVED`), or an argument of a served method it does not
model (`UNSERVED_ARGUMENTS`), is refused 501 naming it; a name Slack has no method by is `unknown_method`, as Slack
answers it. No call is ever refused for its credential: the token only picks the workspace (`SlackWorld.for_token`).

Beside the Web API, on the hosts Slack serves them from (`*.slack.com`, so the same app):
`files.slack.com/files-pri/...`, a file's `url_private` and `url_private_download`, served to any request; and
`hooks.slack.com/actions/...` and `/commands/...`, the `response_url` of a press or a slash command.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

from pydantic import JsonValue
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from minutehand.adapters import answering
from minutehand.adapters.providers.slack import socket_mode, state, wire
from minutehand.adapters.providers.slack.methods import UNSERVED
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.domain.errors import NotServed
from minutehand.domain.world import (
    Actor,
    ControlKind,
    EntityKind,
    MessageAction,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
)
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

_MAX_GROUP = 8
_UNAUTHENTICATED = frozenset({"oauth.v2.access"})
"""Methods an app calls before it holds a token: the workspace they answer for is not the token's."""
_TRIGGER_LIFETIME = 3
"""Seconds a `trigger_id` can open a view, on the run's clock."""
HOOK_LIFETIME = 30 * 60
HOOK_USES = 5
SCOPES = (
    "app_mentions:read,channels:history,channels:read,chat:write,commands,files:read,groups:history,groups:read,"
    "im:history,im:read,im:write,mpim:history,mpim:read,users:read,users:read.email"
)

Handler = Callable[[wire.Presented], wire.Ok]

_ABSENT = frozenset({""})
_OFF = frozenset({"", "false", "0"})
_ON = frozenset({"", "true", "1"})
_UNPARSED = frozenset({"", "none"})
UNSERVED_ARGUMENTS: dict[str, dict[str, frozenset[str]]] = {
    "chat.postMessage": {
        "as_user": _OFF,
        "icon_emoji": _ABSENT,
        "icon_url": _ABSENT,
        "username": _ABSENT,
        "link_names": _OFF,
        "parse": _UNPARSED,
        "mrkdwn": _ON,
        "markdown_text": _ABSENT,
        "metadata": _ABSENT,
        "unfurl_links": _OFF,
        "unfurl_app_links": _ABSENT,
        "current_draft_last_updated_ts": _ABSENT,
    },
    "chat.postEphemeral": {
        "as_user": _OFF,
        "icon_emoji": _ABSENT,
        "icon_url": _ABSENT,
        "username": _ABSENT,
        "link_names": _OFF,
        "parse": _UNPARSED,
        "markdown_text": _ABSENT,
        "metadata": _ABSENT,
    },
    "chat.update": {
        "as_user": _OFF,
        "link_names": _OFF,
        "parse": _UNPARSED,
        "markdown_text": _ABSENT,
        "metadata": _ABSENT,
        "file_ids": _ABSENT,
        "reply_broadcast": _OFF,
        "unfurled_attachments": _ABSENT,
    },
    "chat.delete": {"as_user": _OFF},
    "users.list": {"include_locale": _OFF},
    "users.info": {"include_locale": _OFF},
    "conversations.info": {"include_locale": _OFF},
    "conversations.open": {"prevent_creation": _OFF},
    "views.open": {"interactivity_pointer": _ABSENT},
    "views.publish": {"interactivity_pointer": _ABSENT},
    "oauth.v2.access": {
        "grant_type": frozenset({"", "authorization_code"}),
        "refresh_token": _ABSENT,
        "code_verifier": _ABSENT,
    },
}
"""Arguments Slack documents for a served method (its page under https://docs.slack.dev/reference/methods/) that
this fake serves only at the values given, the ones that leave what Slack stores and answers as the other arguments
make it. Any other value is refused 501, naming the method and the argument, rather than ignored: ignoring it would
answer as if the agent had sent something else."""


def _refuse_unserved_arguments(method: str, presented: wire.Presented) -> None:
    if method not in UNSERVED_ARGUMENTS:
        return
    for name, served in UNSERVED_ARGUMENTS[method].items():
        if name not in presented.arguments:
            continue
        value = presented.arguments[name]
        said = "" if value is None else str(value).lower() if isinstance(value, bool | int | str) else "{}"
        if said not in served:
            raise NotServed(f"{method} with the argument {name}={value!r}, which this fake does not serve")


def _header(request: Request, name: str) -> str | None:
    return request.headers[name] if name in request.headers else None


class SlackApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._world = SlackWorld(store)
        """The workspace the call being answered is in: set for each call before its method runs."""
        self._clock = clock
        self._methods: dict[str, Handler] = {
            "auth.test": self.auth_test,
            "users.list": self.users_list,
            "users.info": self.users_info,
            "users.lookupByEmail": self.users_lookup_by_email,
            "users.profile.get": self.users_profile_get,
            "users.getPresence": self.users_get_presence,
            "dnd.info": self.dnd_info,
            "conversations.list": self.conversations_list,
            "conversations.info": self.conversations_info,
            "conversations.open": self.conversations_open,
            "conversations.members": self.conversations_members,
            "conversations.history": self.conversations_history,
            "conversations.replies": self.conversations_replies,
            "chat.postMessage": self.chat_post_message,
            "chat.postEphemeral": self.chat_post_ephemeral,
            "chat.update": self.chat_update,
            "chat.delete": self.chat_delete,
            "reactions.add": self.reactions_add,
            "views.open": self.views_open,
            "views.update": self.views_update,
            "views.publish": self.views_publish,
            "oauth.v2.access": self.oauth_v2_access,
            "apps.connections.open": self.apps_connections_open,
        }

    @property
    def served(self) -> frozenset[str]:
        """Every Web API method this fake answers."""
        return frozenset(self._methods)

    async def endpoint(self, request: Request) -> Response:
        method = request.path_params["method"]
        if method in UNSERVED:
            raise NotServed(f"{method}, a Slack Web API method this fake does not serve")
        try:
            if method not in self._methods:
                answer: wire.Response = wire.UnknownMethod(req_method=method)
                answering.refused()
                return Response(wire.respond(answer), media_type=wire.JSON)
            presented = wire.read_call(
                request.url.query,
                _header(request, "content-type") or "",
                await request.body(),
                _header(request, "authorization"),
            )
            self._world = SlackWorld(self._store)
            if method not in _UNAUTHENTICATED:
                self._world = self._world.for_token(presented.token)
            _refuse_unserved_arguments(method, presented)
            faulted = self._fault(method, presented)
            answer = faulted if faulted is not None else self._methods[method](presented)
        except wire.Refusal as refusal:
            answering.refused()
            answer = wire.Failed(error=refusal.error)
        if isinstance(answer, wire.RateLimitedAnswer):
            return Response(
                wire.respond(answer),
                status_code=429,
                headers={"Retry-After": str(answer.retry_after)},
                media_type=wire.JSON,
            )
        return Response(wire.respond(answer), media_type=wire.JSON)

    def _fault(self, method: str, presented: wire.Presented) -> wire.Failed | None:
        """The first fault the scenario declares for this call that still has calls to fail, used up by one."""
        now = int(self._clock.now().timestamp())
        for fault in self._world.bodies(EntityKind.RECORD, state.FAULTS, wire.SlackFault):
            if fault.call is not None and fault.call != method:
                continue
            if (fault.remaining is not None and fault.remaining < 1) or now < fault.from_time:
                continue
            if fault.only_rich and not presented.rich:
                continue
            left = None if fault.remaining is None else fault.remaining - 1
            self._world.write(
                state.fault_ref(fault.position),
                fault.model_copy(update={"remaining": left}),
                operation=Operation.UPDATE,
                actor=Actor.SCENARIO,
                parent=state.FAULTS,
                after=RecordSnapshot(resource="faults", text=f"{method} failed on purpose: {fault.error}"),
            )
            answering.injected()
            if fault.retry_after is not None:
                return wire.RateLimitedAnswer(error=fault.error, retry_after=fault.retry_after)
            return wire.Failed(error=fault.error)
        return None

    def apps_connections_open(self, presented: wire.Presented) -> wire.Ok:
        wire.read_args(wire.NoArgs, presented)
        return socket_mode.open_connection(self._world)

    # ------------------------------------------------------------------ lookups

    def _user(self, user: str) -> wire.SlackUser:
        found = self._world.user(user) if user else None
        if found is None:
            raise wire.Refusal("user_not_found")
        return found

    def _channel(self, channel: str) -> wire.SlackChannel:
        """A conversation the app can see: public channels, and anything private it is in."""
        found = self._world.channel(channel) if channel else None
        if found is None or ((found.is_private or found.is_im or found.is_mpim) and not self._in(found)):
            raise wire.Refusal("channel_not_found")
        return found

    def _joined(self, channel: str) -> wire.SlackChannel:
        found = self._channel(channel)
        if not self._in(found):
            raise wire.Refusal("not_in_channel")
        return found

    def _in(self, channel: wire.SlackChannel) -> bool:
        return self._world.is_member(channel.id, self._world.bot)

    def _served(self, channel: wire.SlackChannel) -> wire.SlackChannel:
        if channel.is_im:
            return channel
        return channel.model_copy(update={"is_member": self._in(channel)})

    def _snapshot(self, channel: str, message: wire.SlackMessage) -> MessageSnapshot:
        return self._snapshot_in(self._world, channel, message)

    def _snapshot_in(self, world: SlackWorld, channel: str, message: wire.SlackMessage) -> MessageSnapshot:
        return MessageSnapshot(
            text=wire.visible_text(message.text, message.blocks),
            channel=channel,
            recipient_emails=world.human_emails(channel, besides=message.user),
            thread_of=message.thread_ts,
            actions=message_actions(message),
        )

    def _summarised(self, root: wire.SlackMessage, every: list[wire.SlackMessage]) -> wire.SlackMessage:
        """A message as a listing serves it. A thread's parent carries its reply count, repliers and latest reply: the
        only sign in history that a thread exists. A reply carries its parent's author (`parent_user_id`), and a reply
        broadcast to the channel its parent (`root`), as Slack computes them
        (https://docs.slack.dev/messaging/retrieving-messages#threading,
        https://docs.slack.dev/reference/events/message/thread_broadcast)."""
        if root.thread_ts is not None and root.thread_ts != root.ts:
            return self._as_reply(root, every)
        replies = [m for m in every if m.thread_ts == root.ts and m.ts != root.ts]
        if not replies:
            return root
        users = list(dict.fromkeys(m.user for m in replies))
        return root.model_copy(
            update={
                "thread_ts": root.ts,
                "reply_count": len(replies),
                "reply_users": users,
                "reply_users_count": len(users),
                "latest_reply": replies[-1].ts,
            }
        )

    def _as_reply(self, reply: wire.SlackMessage, every: list[wire.SlackMessage]) -> wire.SlackMessage:
        parent = next((m for m in every if m.ts == reply.thread_ts), None)
        if parent is None:
            return reply
        served = reply.model_copy(update={"parent_user_id": parent.user})
        if served.subtype == "thread_broadcast":  # enum-lint: exempt Slack's own message subtype on the wire
            return served.model_copy(update={"root": self._summarised(parent, every)})
        return served

    def _thread_of(self, channel: str, thread_ts: str | None) -> str | None:
        """The thread a post with `thread_ts` goes in: the parent it names. A `thread_ts` naming a reply, or naming
        no message, is refused by name: Slack's pages say only "Avoid using a reply's ts value; use its parent
        instead" (https://docs.slack.dev/reference/methods/chat.postMessage) and list no error for either."""
        if not thread_ts:
            return None
        parent = self._world.message(channel, thread_ts)
        if parent is None:
            raise NotServed(f"a thread_ts ({thread_ts}) that names no message in {channel}")
        if parent.thread_ts is not None and parent.thread_ts != parent.ts:
            raise NotServed(f"a thread_ts ({thread_ts}) that names a reply rather than its thread's parent")
        return parent.ts

    # ------------------------------------------------------------------ auth, users

    def auth_test(self, presented: wire.Presented) -> wire.Ok:
        wire.read_args(wire.NoArgs, presented)
        self._world.saw(state.user_ref(self._world.bot), Operation.READ)
        return wire.AuthTest(
            url=f"https://{self._world.team.domain}.slack.com/",
            team=self._world.team.name,
            user=self._world.team.bot_name,
            team_id=self._world.team.id,
            user_id=self._world.bot,
            bot_id=self._world.team.bot_id,
        )

    def _now(self) -> int:
        return int(self._clock.now().timestamp())

    def _shown(self, user: wire.SlackUser) -> wire.SlackUser:
        """The user as others see them now: an absence the scenario gives a reason for shows that reason as their
        status, until it ends. Nothing else is written there: the status is the person's, and the scenario says
        only why they are away."""
        away = self._world.away(user.id, self._now())
        if away is None or not away.reason:
            return user
        status = {"status_text": away.reason, "status_expiration": away.ends}
        return user.model_copy(update={"profile": user.profile.model_copy(update=status)})

    def _asked_about(self, presented: wire.Presented) -> wire.SlackUser:
        """The user a call names, or the caller's own bot user when it names none."""
        named = wire.read_args(wire.UserArgs, presented).user
        return self._user(named or self._world.bot)

    def users_profile_get(self, presented: wire.Presented) -> wire.Ok:
        user = self._asked_about(presented)
        self._world.saw(state.user_ref(user.id), Operation.READ)
        return wire.OneProfile(profile=self._shown(user).profile)

    def users_get_presence(self, presented: wire.Presented) -> wire.Ok:
        """`away` while the person is away; `active` otherwise. The bot's own presence is always `active`."""
        user = self._asked_about(presented)
        self._world.saw(state.user_ref(user.id), Operation.READ)
        away = self._world.away(user.id, self._now()) is not None
        return wire.Presence(presence="away" if away else "active")

    def dnd_info(self, presented: wire.Presented) -> wire.Ok:
        """Do not disturb, snoozed for the whole of an absence; off otherwise, with no schedule, since the world holds
        none for the person."""
        user = self._asked_about(presented)
        self._world.saw(state.user_ref(user.id), Operation.READ)
        away = self._world.away(user.id, self._now())
        if away is None:
            return wire.DndInfo(dnd_enabled=False, snooze_enabled=False)
        return wire.DndInfo(
            dnd_enabled=True,
            next_dnd_start_ts=away.starts,
            next_dnd_end_ts=away.ends,
            snooze_enabled=True,
            snooze_endtime=away.ends,
            snooze_remaining=away.ends - self._now(),
        )

    def users_list(self, presented: wire.Presented) -> wire.Ok:
        """Every member, Slackbot among them in its place by id: it is in every workspace, though nobody seeds it."""
        args = wire.read_args(wire.ListArgs, presented)
        limit = wire.page_size(args.limit, default=None)
        after = wire.decode_cursor(args.cursor)
        page = list(self._world.every_user(after)) if limit is None else self._world.users(after=after, limit=limit + 1)
        if after is None or after < state.SLACKBOT_ID:
            page = sorted([*page, state.slackbot(self._world.team.id, self._clock.now())], key=lambda u: u.id)
        more = limit is not None and len(page) > limit
        page = page if limit is None else page[:limit]
        self._world.saw(state.team_ref(self._world.team.id), Operation.SEARCH)
        return wire.UserList(
            members=[self._shown(u) for u in page],
            response_metadata=wire.ResponseMetadata(next_cursor=wire.encode_cursor(page[-1].id) if more else ""),
        )

    def users_info(self, presented: wire.Presented) -> wire.Ok:
        named = wire.read_args(wire.UserArgs, presented).user
        if named == state.SLACKBOT_ID:
            self._world.saw(state.team_ref(self._world.team.id), Operation.READ)
            return wire.OneUser(user=state.slackbot(self._world.team.id, self._clock.now()))
        user = self._user(named)
        self._world.saw(state.user_ref(user.id), Operation.READ)
        return wire.OneUser(user=self._shown(user))

    def users_lookup_by_email(self, presented: wire.Presented) -> wire.Ok:
        email = wire.read_args(wire.EmailArgs, presented).email
        found = next((u for u in self._world.every_user() if email and u.profile.email == email), None)
        if found is None:
            raise wire.Refusal("users_not_found")
        self._world.saw(state.team_ref(self._world.team.id), Operation.SEARCH)
        return wire.OneUser(user=self._shown(found))

    # ------------------------------------------------------------------ conversations

    def conversations_list(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ConversationsListArgs, presented)
        types = {t.strip() for t in args.types.split(",") if t.strip()} or {"public_channel"}
        if not types <= wire.CONVERSATION_TYPES:
            raise wire.Refusal("invalid_types")
        limit = wire.page_size(args.limit, default=100, most=999)
        picked: list[wire.SlackChannel] = []
        more = False
        for channel in self._world.channels_after(wire.decode_cursor(args.cursor)):
            if wire.conversation_type(channel) not in types or (args.exclude_archived and channel.is_archived):
                continue
            if (channel.is_private or channel.is_im or channel.is_mpim) and not self._in(channel):
                continue
            if len(picked) == limit:
                more = True
                break
            picked.append(self._served(channel))
        self._world.saw(state.team_ref(self._world.team.id), Operation.SEARCH)
        return wire.ChannelList(
            channels=picked,
            response_metadata=wire.ResponseMetadata(next_cursor=wire.encode_cursor(picked[-1].id) if more else ""),
        )

    def conversations_info(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ChannelInfoArgs, presented)
        channel = self._served(self._channel(args.channel))
        self._world.saw(state.channel_ref(channel.id), Operation.READ)
        if args.include_num_members:
            channel = channel.model_copy(update={"num_members": len(self._world.every_member(channel.id))})
        return wire.OneChannel(channel=channel)

    def conversations_open(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ConversationsOpenArgs, presented)
        if args.channel and not args.users:
            channel = self._channel(args.channel)
            if not (channel.is_im or channel.is_mpim):
                raise wire.Refusal("channel_not_found")
            self._world.saw(state.channel_ref(channel.id), Operation.READ)
            return wire.Opened(
                no_op=True, already_open=True, channel=channel if args.return_im else wire.OpenedId(id=channel.id)
            )
        wanted = [u.strip() for u in args.users.split(",") if u.strip()]
        if not wanted:
            raise wire.Refusal("users_list_not_supplied")
        if len(wanted) > _MAX_GROUP:
            raise wire.Refusal("too_many_users")
        found = [self._user(u) for u in wanted]
        for user in found:
            if user.deleted:
                raise wire.Refusal("user_disabled")
            if user.is_bot and len(found) == 1:
                raise wire.Refusal("cannot_dm_bot")
        others = [u.id for u in found]
        channel, opened = self._conversation(others, actor=Actor.AGENT)
        if not opened:
            self._world.saw(state.channel_ref(channel.id), Operation.READ)
        return wire.Opened(
            no_op=not opened,
            already_open=not opened,
            channel=channel if args.return_im else wire.OpenedId(id=channel.id),
        )

    def _conversation(self, others: list[str], *, actor: Actor) -> tuple[wire.SlackChannel, bool]:
        """The IM or group DM between the app and `others`, created when it does not exist yet."""
        members = sorted({self._world.bot, *others})
        cid = state.conversation_id(members)
        existing = self._world.channel(cid)
        if existing is not None:
            return existing, False
        channel = self._world.open_conversation(members, created=int(self._clock.now().timestamp()), actor=actor)
        return channel, True

    def conversations_members(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.MembersArgs, presented)
        channel = self._channel(args.channel)
        limit = wire.page_size(args.limit, default=100) or 100
        page = self._world.members(channel.id, after=wire.decode_cursor(args.cursor), limit=limit + 1)
        more = len(page) > limit
        page = page[:limit]
        self._world.saw(state.channel_ref(channel.id), Operation.READ)
        return wire.Members(
            members=[m.user for _, m in page],
            response_metadata=wire.ResponseMetadata(next_cursor=wire.encode_cursor(page[-1][0]) if more else ""),
        )

    def conversations_history(self, presented: wire.Presented) -> wire.Ok:
        """Roots only, newest first, inside `oldest`/`latest` with `inclusive` deciding the bounds."""
        args = wire.read_args(wire.HistoryArgs, presented)
        channel = self._joined(args.channel)
        latest = wire.timestamp(args.latest, "invalid_ts_latest") if args.latest else None
        oldest = wire.timestamp(args.oldest, "invalid_ts_oldest") if args.oldest else None
        inclusive = args.inclusive
        resume = wire.decode_cursor(args.cursor)
        every = self._world.messages(channel.id)
        roots = [
            m
            for m in reversed(every)
            if m.thread_ts is None
            or m.thread_ts == m.ts
            or m.subtype == "thread_broadcast"  # enum-lint: exempt Slack's own message subtype on the wire
        ]
        window = [
            m
            for m in roots
            if _within(Decimal(m.ts), oldest=oldest, latest=latest, inclusive=inclusive)
            and (resume is None or Decimal(m.ts) < wire.timestamp(resume, "invalid_cursor"))
        ]
        limit = wire.page_size(args.limit, default=100, most=999) or 100
        page, more = window[:limit], len(window) > limit
        self._world.saw(state.channel_ref(channel.id), Operation.READ)
        return wire.MessageList(
            messages=[self._summarised(m, every) for m in page],
            has_more=more,
            response_metadata=wire.ResponseMetadata(next_cursor=wire.encode_cursor(page[-1].ts) if more else ""),
        )

    def conversations_replies(self, presented: wire.Presented) -> wire.Ok:
        """A thread: its parent first, whatever the window, then its replies oldest first."""
        args = wire.read_args(wire.RepliesArgs, presented)
        channel = self._joined(args.channel)
        asked = self._world.message(channel.id, args.ts) if args.ts else None
        if asked is None:
            raise wire.Refusal("thread_not_found")
        root_ts = asked.thread_ts or asked.ts
        root = self._world.message(channel.id, root_ts)
        if root is None:
            raise wire.Refusal("thread_not_found")
        latest = wire.timestamp(args.latest, "invalid_ts_latest") if args.latest else None
        oldest = wire.timestamp(args.oldest, "invalid_ts_oldest") if args.oldest else None
        resume = wire.decode_cursor(args.cursor)
        every = self._world.messages(channel.id)
        replies = [
            m
            for m in every
            if m.thread_ts == root_ts
            and m.ts != root_ts
            and _within(Decimal(m.ts), oldest=oldest, latest=latest, inclusive=args.inclusive)
            and (resume is None or Decimal(m.ts) > wire.timestamp(resume, "invalid_cursor"))
        ]
        thread = replies if resume is not None else [self._summarised(root, every), *replies]
        limit = wire.page_size(args.limit, default=1000) or 1000
        page, more = thread[:limit], len(thread) > limit
        self._world.saw(state.message_ref(root_ts), Operation.READ)
        return wire.MessageList(
            messages=[self._summarised(m, every) if m.ts != root_ts else m for m in page],
            has_more=more,
            response_metadata=wire.ResponseMetadata(next_cursor=wire.encode_cursor(page[-1].ts) if more else ""),
        )

    # ------------------------------------------------------------------ chat, reactions

    def chat_post_message(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.PostMessageArgs, presented)
        channel = self._destination(args.channel)
        if not self._in(channel):
            raise wire.Refusal("not_in_channel")
        if channel.is_archived:
            raise wire.Refusal("is_archived")
        if not args.text and not args.blocks and not args.attachments:
            raise wire.Refusal("no_text")
        wire.check_message(args.text, args.blocks, refused_past=None)
        thread_ts = self._thread_of(channel.id, args.thread_ts)
        text = args.text[: wire.TRUNCATED_AT]
        message = self._from_bot(text, args.blocks, args.attachments, thread_ts)
        if thread_ts is not None and args.reply_broadcast:
            message = message.model_copy(update={"subtype": "thread_broadcast"})
        self._world.write(
            state.message_ref(message.ts),
            message,
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            parent=channel.id,
            after=self._snapshot(channel.id, message),
        )
        return wire.Posted(
            channel=channel.id, ts=message.ts, message=self._summarised(message, self._world.messages(channel.id))
        )

    def chat_post_ephemeral(self, presented: wire.Presented) -> wire.Ok:
        """A message shown to one member of a conversation, once. It is in no history and cannot be found again;
        it is recorded as a message to that member alone, so it asks nothing of anyone else in the channel."""
        args = wire.read_args(wire.PostEphemeralArgs, presented)
        channel = self._joined(args.channel)
        user = self._user(args.user)
        if not self._world.is_member(channel.id, user.id):
            raise wire.Refusal("user_not_in_channel")
        if channel.is_archived:
            raise wire.Refusal("is_archived")
        if not args.text and not args.blocks and not args.attachments:
            raise wire.Refusal("no_text")
        wire.check_message(args.text, args.blocks, refused_past=wire.MAX_EPHEMERAL_CHARS)
        thread_ts = self._thread_of(channel.id, args.thread_ts)
        message = self._from_bot(args.text, args.blocks, args.attachments, thread_ts, ephemeral_to=user.id)
        self._write_ephemeral(channel.id, message, user)
        return wire.PostedEphemeral(message_ts=message.ts)

    def _from_bot(
        self,
        text: str,
        blocks: list[JsonValue] | None,
        attachments: list[JsonValue] | None,
        thread_ts: str | None,
        *,
        ephemeral_to: str | None = None,
    ) -> wire.SlackMessage:
        return self._from_bot_in(self._world, text, blocks, attachments, thread_ts, ephemeral_to=ephemeral_to)

    def _from_bot_in(
        self,
        world: SlackWorld,
        text: str,
        blocks: list[JsonValue] | None,
        attachments: list[JsonValue] | None,
        thread_ts: str | None,
        *,
        ephemeral_to: str | None = None,
    ) -> wire.SlackMessage:
        """A message the app posts, as Slack keeps it: its blocks given ids, and the app's bot profile on it."""
        ts = world.next_ts(self._clock)
        return wire.SlackMessage(
            ts=ts,
            user=world.bot,
            text=text,
            team=world.team.id,
            bot_id=world.team.bot_id,
            app_id=world.team.app_id,
            thread_ts=thread_ts,
            blocks=wire.with_ids(blocks, ts),
            attachments=attachments,
            bot_profile=state.bot_profile(int(self._clock.now().timestamp()), world.team),
            ephemeral_to=ephemeral_to,
        )

    def _write_ephemeral(self, channel: str, message: wire.SlackMessage, user: wire.SlackUser) -> None:
        self._write_ephemeral_in(self._world, channel, message, user)

    def _write_ephemeral_in(
        self, world: SlackWorld, channel: str, message: wire.SlackMessage, user: wire.SlackUser
    ) -> None:
        seen_by = [e for e in [world.email_of(user)] if e is not None]
        world.write(
            state.message_ref(message.ts),
            message,
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            parent=channel,
            after=MessageSnapshot(
                text=wire.visible_text(message.text, message.blocks),
                channel=channel,
                recipient_emails=seen_by,
                thread_of=message.thread_ts,
                actions=message_actions(message),
            ),
        )

    def _destination(self, channel: str) -> wire.SlackChannel:
        """A channel id, or a member id, which Slack answers with that member's IM with the app."""
        if channel and self._world.channel(channel) is None and self._world.user(channel) is not None:
            return self._conversation([channel], actor=Actor.AGENT)[0]
        return self._channel(channel)

    def chat_update(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.UpdateArgs, presented)
        channel = self._channel(args.channel)
        message = self._world.message(channel.id, args.ts) if args.ts else None
        if message is None:
            raise wire.Refusal("message_not_found")
        if message.user != self._world.bot:
            raise wire.Refusal("cant_update_message")
        if args.text is None and args.blocks is None and args.attachments is None:
            raise wire.Refusal("no_text")
        text = message.text if args.text is None else args.text
        if args.blocks is not None:
            blocks = wire.with_ids(args.blocks, message.ts)
        else:
            # Slack keeps the old blocks only when neither blocks nor text is given; new text alone replaces them.
            blocks = message.blocks if args.text is None else None
        wire.check_message(text, blocks, refused_past=wire.MAX_UPDATE_CHARS if args.text is not None else None)
        updated = message.model_copy(
            update={
                "text": text,
                "blocks": blocks,
                "attachments": message.attachments if args.attachments is None else args.attachments,
                "edited": wire.SlackEdited(user=self._world.bot, ts=self._world.next_ts(self._clock)),
            }
        )
        self._world.write(
            state.message_ref(updated.ts),
            updated,
            operation=Operation.UPDATE,
            actor=Actor.AGENT,
            parent=channel.id,
            after=self._snapshot(channel.id, updated),
        )
        return wire.Updated(channel=channel.id, ts=updated.ts, text=updated.text, message=updated)

    def chat_delete(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.DeleteArgs, presented)
        channel = self._channel(args.channel)
        message = self._world.message(channel.id, args.ts) if args.ts else None
        if message is None:
            raise wire.Refusal("message_not_found")
        if message.user != self._world.bot:
            raise wire.Refusal("cant_delete_message")
        self._world.delete(
            state.message_ref(message.ts),
            actor=Actor.AGENT,
            parent=channel.id,
            before=self._snapshot(channel.id, message),
        )
        return wire.Deleted(channel=channel.id, ts=message.ts)

    def reactions_add(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ReactionArgs, presented)
        channel = self._channel(args.channel)
        message = self._world.message(channel.id, args.timestamp) if args.timestamp else None
        if message is None:
            raise wire.Refusal("message_not_found")
        name = args.name
        if not name:
            raise wire.Refusal("invalid_name")
        reactions = list(message.reactions or [])
        same = next((r for r in reactions if r.name == name), None)
        if same is not None and self._world.bot in same.users:
            raise wire.Refusal("already_reacted")
        if same is None:
            reactions.append(wire.SlackReaction(name=name, users=[self._world.bot], count=1))
        else:
            reactions[reactions.index(same)] = wire.SlackReaction(
                name=name, users=[*same.users, self._world.bot], count=same.count + 1
            )
        self._world.write(
            state.message_ref(message.ts),
            message.model_copy(update={"reactions": reactions}),
            operation=Operation.UPDATE,
            actor=Actor.AGENT,
            parent=channel.id,
        )
        return wire.Ok()

    # ------------------------------------------------------------------ views

    def views_open(self, presented: wire.Presented) -> wire.Ok:
        """A modal, opened with the `trigger_id` of a press or a command: once, and within three seconds."""
        args = wire.read_args(wire.ViewsOpenArgs, presented)
        if args.view is None:
            raise wire.Refusal("invalid_arguments")
        trigger = self._world.body(state.trigger_ref(args.trigger_id), wire.SlackTrigger) if args.trigger_id else None
        if trigger is None:
            raise wire.Refusal("invalid_trigger_id")
        if trigger.view is not None:
            raise wire.Refusal("exchanged_trigger_id")
        if int(self._clock.now().timestamp()) > trigger.issued + _TRIGGER_LIFETIME:
            raise wire.Refusal("expired_trigger_id")
        if args.view.type != "modal":  # enum-lint: exempt Slack's own view type on the wire
            raise wire.Refusal("invalid_arguments")
        wire.check_view(args.view)
        self._refuse_taken_external_id(args.view.external_id, None)
        view_id = state.view_id(self._world.next_seq())
        shown = self._view(view_id, args.view, root=view_id, version=0)
        self._world.write(
            state.trigger_ref(trigger.id),
            trigger.model_copy(update={"view": view_id}),
            operation=Operation.UPDATE,
            actor=Actor.AGENT,
            parent=state.TRIGGERS,
        )
        self._write_view(wire.OpenView(view=shown, user=trigger.user, trigger_id=trigger.id), Operation.CREATE)
        return wire.ViewAnswered(view=shown)

    def views_update(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ViewsUpdateArgs, presented)
        if args.view is None or not (args.view_id or args.external_id):
            raise wire.Refusal("invalid_arguments")
        found = self._open_view(args.view_id, args.external_id)
        if found is None:
            raise wire.Refusal("not_found")
        if args.hash and args.hash != found.view.hash:
            raise wire.Refusal("hash_conflict")
        if args.view.type != found.view.type:
            raise wire.Refusal("invalid_arguments")
        wire.check_view(args.view)
        self._refuse_taken_external_id(args.view.external_id, found.view.id)
        shown = self._view(found.view.id, args.view, root=found.view.root_view_id, version=self._version(found))
        self._write_view(found.model_copy(update={"view": shown, "errors": {}}), Operation.UPDATE)
        return wire.ViewAnswered(view=shown)

    def views_publish(self, presented: wire.Presented) -> wire.Ok:
        """A member's Home tab: one per member, replaced by every publish."""
        args = wire.read_args(wire.ViewsPublishArgs, presented)
        if args.view is None or not args.user_id:
            raise wire.Refusal("invalid_arguments")
        if args.view.type != "home":  # enum-lint: exempt Slack's own view type on the wire
            raise wire.Refusal("invalid_arguments")
        user = self._user(args.user_id)
        wire.check_view(args.view)
        view_id = state.home_view_id(user.id)
        existing = self._world.body(state.view_ref(view_id), wire.OpenView)
        if existing is not None and args.hash and args.hash != existing.view.hash:
            raise wire.Refusal("hash_conflict")
        version = self._version(existing) if existing is not None else 0
        shown = self._view(view_id, args.view, root=view_id, version=version)
        self._write_view(
            wire.OpenView(view=shown, user=user.id), Operation.CREATE if existing is None else Operation.UPDATE
        )
        return wire.ViewAnswered(view=shown)

    def _view(self, view_id: str, spec: wire.ViewSpec, *, root: str, version: int) -> wire.SlackView:
        blocks = wire.with_ids(spec.blocks, view_id) or []
        return wire.SlackView(
            id=view_id,
            team_id=self._world.team.id,
            type=spec.type,
            title=spec.title,
            submit=spec.submit,
            close=spec.close,
            blocks=blocks,
            private_metadata=spec.private_metadata,
            callback_id=spec.callback_id,
            external_id=spec.external_id,
            state=wire.ViewState(),
            hash=state.view_hash(view_id, version),
            clear_on_close=spec.clear_on_close,
            notify_on_close=spec.notify_on_close,
            submit_disabled=spec.submit_disabled,
            root_view_id=root,
            app_id=self._world.team.app_id,
            app_installed_team_id=self._world.team.id,
            bot_id=self._world.team.bot_id,
        )

    def _version(self, found: wire.OpenView) -> int:
        return int(found.view.hash.split(".", 1)[0]) + 1

    def _open_view(self, view_id: str, external_id: str) -> wire.OpenView | None:
        if view_id:
            found = self._world.body(state.view_ref(view_id), wire.OpenView)
            return found if found is not None and found.open else None
        return next(
            (
                v
                for v in self._world.bodies(EntityKind.RECORD, state.VIEWS, wire.OpenView)
                if v.open and v.view.external_id == external_id
            ),
            None,
        )

    def _refuse_taken_external_id(self, external_id: str, besides: str | None) -> None:
        if not external_id:
            return
        for found in self._world.bodies(EntityKind.RECORD, state.VIEWS, wire.OpenView):
            if found.open and found.view.external_id == external_id and found.view.id != besides:
                raise wire.Refusal("duplicate_external_id")

    def _write_view(self, shown: wire.OpenView, operation: Operation) -> None:
        write_view(self._world, shown, operation, Actor.AGENT)

    # ------------------------------------------------------------------ oauth

    def oauth_v2_access(self, presented: wire.Presented) -> wire.Ok:
        """The install's code exchanged for a new bot token of the workspace the code is for (the first when it is
        none's). Every exchange passes: whatever client id, secret and code are presented, any number of times, since
        Minutehand never refuses a credential and no browser ever signed in to make a code. The installer is the
        scenario's owner."""
        args = wire.read_args(wire.OAuthArgs, presented)
        every = self._world.workspaces()
        self._world = self._world.as_team(next((w for w in every if w.oauth_code == args.code), every[0]))
        install = self._world.body(state.install_ref(self._world.team.id), wire.SlackInstall)
        if install is None:
            raise LookupError(f"the workspace {self._world.team.id} was seeded with no install")
        token = f"xoxb-{self._world.team.id}-{self._world.bot}-{state.minted('token', len(install.exchanged), 0)}"
        self._world.write(
            state.install_ref(self._world.team.id),
            install.model_copy(
                update={"exchanged": [*install.exchanged, args.code], "tokens": [*install.tokens, token]}
            ),
            operation=Operation.UPDATE,
            actor=Actor.AGENT,
            parent=state.APP,
        )
        return wire.OAuthAccess(
            app_id=self._world.team.app_id,
            authed_user=wire.AuthedUser(id=install.installer),
            scope=SCOPES,
            access_token=token,
            bot_user_id=self._world.bot,
            team=wire.TeamRef(id=self._world.team.id, name=self._world.team.name),
        )

    # ------------------------------------------------------------------ files

    async def file(self, request: Request) -> Response:
        """A file's `url_private`: its content, to any request. Slack sends a caller without a token to its sign-in
        page; Minutehand never refuses a credential, so the file's workspace is the one its URL names."""
        team, _, file_id = request.path_params["key"].partition("-")
        world = SlackWorld(self._store).team_of(team)
        if world is None:
            return HTMLResponse(_NOT_FOUND, status_code=404)
        found = world.file(file_id)
        content = world.body(state.content_ref(file_id), wire.SlackFileContent) if found is not None else None
        if found is None or content is None or request.path_params["name"] != found.name:
            return HTMLResponse(_NOT_FOUND, status_code=404)
        world.saw(state.file_ref(found.id), Operation.READ)
        headers = (
            {"Content-Disposition": f'attachment; filename="{found.name}"'}
            if "download" in request.url.path.split("/")
            else {}
        )
        return Response(content.text.encode(), media_type=content.mimetype, headers=headers)

    # ------------------------------------------------------------------ response_url

    async def response_url(self, request: Request) -> Response:
        """What the agent posts to the `response_url` of a press or a command: a new message, ephemeral unless it
        says `in_channel`; with `replace_original` the pressed message rewritten; with `delete_original` removed.
        Five uses, thirty minutes, as Slack allows."""
        found_team = SlackWorld(self._store).team_of(request.path_params["team"])
        hook = found_team.body(state.hook_ref(request.path_params["hook"]), wire.SlackHook) if found_team else None
        if found_team is None or hook is None:
            return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=404)
        if int(self._clock.now().timestamp()) > hook.issued + HOOK_LIFETIME:
            return JSONResponse({"ok": False, "error": "expired_url"}, status_code=404)
        if hook.used >= HOOK_USES:
            return JSONResponse({"ok": False, "error": "used_url"}, status_code=404)
        world = found_team
        try:
            body = wire.ResponseUrlBody.model_validate_json(await request.body())
        except ValueError:
            return JSONResponse({"ok": False, "error": "invalid_payload"}, status_code=400)
        world.write(
            state.hook_ref(hook.id),
            hook.model_copy(update={"used": hook.used + 1}),
            operation=Operation.UPDATE,
            actor=Actor.AGENT,
            parent=state.HOOKS,
        )
        original = world.located(hook.message) if hook.message is not None else None
        if body.delete_original or body.replace_original:
            if original is None:
                return JSONResponse({"ok": False, "error": "message_not_found"}, status_code=404)
            channel, message = original
            if body.delete_original:
                world.delete(
                    state.message_ref(message.ts),
                    actor=Actor.AGENT,
                    parent=channel,
                    before=self._snapshot_in(world, channel, message),
                )
                return JSONResponse({"ok": True})
            replaced = message.model_copy(
                update={
                    "text": body.text,
                    "blocks": wire.with_ids(body.blocks, message.ts),
                    "attachments": body.attachments,
                }
            )
            snapshot = (
                self._snapshot_in(world, channel, replaced)
                if replaced.ephemeral_to is None
                else self._snapshot_in(world, channel, replaced).model_copy(
                    update={"recipient_emails": self._emails_in(world, [replaced.ephemeral_to])}
                )
            )
            world.write(
                state.message_ref(message.ts),
                replaced,
                operation=Operation.UPDATE,
                actor=Actor.AGENT,
                parent=channel,
                after=snapshot,
            )
            return JSONResponse({"ok": True})
        thread_ts = body.thread_ts or (original[1].thread_ts if original is not None else None)
        if body.response_type == "in_channel":  # enum-lint: exempt Slack's own response type on the wire
            message = self._from_bot_in(world, body.text, body.blocks, body.attachments, thread_ts)
            world.write(
                state.message_ref(message.ts),
                message,
                operation=Operation.CREATE,
                actor=Actor.AGENT,
                parent=hook.channel,
                after=self._snapshot_in(world, hook.channel, message),
            )
            return JSONResponse({"ok": True})
        user = world.user(hook.user)
        if user is None:
            return JSONResponse({"ok": False, "error": "user_not_found"}, status_code=404)
        message = self._from_bot_in(world, body.text, body.blocks, body.attachments, thread_ts, ephemeral_to=user.id)
        self._write_ephemeral_in(world, hook.channel, message, user)
        return JSONResponse({"ok": True})

    def _emails_in(self, world: SlackWorld, users: list[str]) -> list[str]:
        found = [world.user(u) for u in users]
        emails = [world.email_of(u) for u in found if u is not None]
        return [e for e in emails if e is not None]


_NOT_FOUND = "<!DOCTYPE html><html><head><title>Not found | Slack</title></head><body></body></html>"


def message_actions(message: wire.SlackMessage) -> list[MessageAction]:
    """The controls a reader can use on a message, as the domain names them."""
    kinds = {"button": ControlKind.BUTTON, "users_select": ControlKind.USER_SELECT}
    return [
        MessageAction(
            action_id=c.action_id,
            label=c.label,
            control=ControlKind.LINK if c.url is not None else kinds[c.type],
            value=c.value,
        )
        for c in wire.controls(message.blocks)
    ]


def write_view(world: SlackWorld, shown: wire.OpenView, operation: Operation, actor: Actor) -> None:
    """A view in the store, recorded with the text it shows so the checks can read what the agent wrote in it."""
    title = shown.view.title.text if shown.view.title is not None else ""
    world.write(
        state.view_ref(shown.view.id),
        shown,
        operation=operation,
        actor=actor,
        parent=state.VIEWS,
        after=RecordSnapshot(
            resource="views", text="\n".join(t for t in (title, wire.visible_text("", shown.view.blocks)) if t)
        ),
    )


def _within(ts: Decimal, *, oldest: Decimal | None, latest: Decimal | None, inclusive: bool) -> bool:
    if oldest is not None and (ts < oldest if inclusive else ts <= oldest):
        return False
    return not (latest is not None and (ts > latest if inclusive else ts >= latest))


def build_app(store: Store, clock: Clock) -> Starlette:
    api = SlackApi(store, clock)
    endpoint: Callable[[Request], Awaitable[Response]] = api.endpoint
    return Starlette(
        routes=[
            Route("/api/{method}", endpoint, methods=["GET", "POST"]),
            Route("/files-pri/{key}/{name}", api.file, methods=["GET"]),
            Route("/files-pri/{key}/download/{name}", api.file, methods=["GET"]),
            Route("/actions/{team}/{hook}/{secret}", api.response_url, methods=["POST"]),
            Route("/commands/{team}/{hook}/{secret}", api.response_url, methods=["POST"]),
        ]
    )
