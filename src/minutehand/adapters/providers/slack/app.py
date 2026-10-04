"""The Slack Web API, as an ASGI app over the run's store and clock.

Every method answers at `/api/<method>`, by GET or POST, with its arguments in the
query string, a form-encoded body or a JSON body, exactly as Slack accepts them.
Every refusal is Slack's own `{"ok": false, "error": ...}` with HTTP 200.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.state import BOT_ID, BOT_USER_ID, SlackWorld
from minutehand.domain.world import Actor, MessageSnapshot, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

_TOKEN_KINDS = ("xoxb-", "xoxp-")
_MAX_GROUP = 8

Handler = Callable[[wire.Presented], wire.Ok]


def _header(request: Request, name: str) -> str | None:
    return request.headers[name] if name in request.headers else None


class SlackApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._world = SlackWorld(store)
        self._clock = clock
        self._methods: dict[str, Handler] = {
            "auth.test": self.auth_test,
            "users.list": self.users_list,
            "users.info": self.users_info,
            "users.lookupByEmail": self.users_lookup_by_email,
            "conversations.list": self.conversations_list,
            "conversations.info": self.conversations_info,
            "conversations.open": self.conversations_open,
            "conversations.members": self.conversations_members,
            "conversations.history": self.conversations_history,
            "conversations.replies": self.conversations_replies,
            "chat.postMessage": self.chat_post_message,
            "chat.update": self.chat_update,
            "chat.delete": self.chat_delete,
            "reactions.add": self.reactions_add,
        }

    async def endpoint(self, request: Request) -> Response:
        method = request.path_params["method"]
        try:
            if method not in self._methods:
                raise wire.Refusal("unknown_method")
            presented = wire.read_call(
                request.url.query, _header(request, "content-type") or "", await request.body(),
                _header(request, "authorization"),
            )
            self._authenticate(presented)
            answer: wire.Response = self._methods[method](presented)
        except wire.Refusal as refusal:
            answer = wire.Failed(error=refusal.error)
        return Response(wire.respond(answer), media_type="application/json; charset=utf-8")

    def _authenticate(self, presented: wire.Presented) -> None:
        """Any bot or user token is accepted: the proxy holds no real credentials to check them against."""
        if presented.token is None:
            raise wire.Refusal("not_authed")
        if not presented.token.startswith(_TOKEN_KINDS) or self._world.user(BOT_USER_ID) is None:
            raise wire.Refusal("invalid_auth")

    # ------------------------------------------------------------------ lookups

    def _user(self, user: str) -> wire.SlackUser:
        found = self._world.user(user) if user else None
        if found is None:
            raise wire.Refusal("user_not_found")
        return found

    def _channel(self, channel: str) -> wire.SlackChannel:
        """A conversation the app can see: public channels, and anything private it is in."""
        found = self._world.channel(channel) if channel else None
        if found is None or (found.is_private or found.is_im or found.is_mpim) and not self._in(found):
            raise wire.Refusal("channel_not_found")
        return found

    def _joined(self, channel: str) -> wire.SlackChannel:
        found = self._channel(channel)
        if not self._in(found):
            raise wire.Refusal("not_in_channel")
        return found

    def _in(self, channel: wire.SlackChannel) -> bool:
        return self._world.is_member(channel.id, BOT_USER_ID)

    def _served(self, channel: wire.SlackChannel) -> wire.SlackChannel:
        if channel.is_im:
            return channel
        return channel.model_copy(update={"is_member": self._in(channel)})

    def _snapshot(self, channel: str, message: wire.SlackMessage) -> MessageSnapshot:
        return MessageSnapshot(
            text=wire.visible_text(message.text, message.blocks),
            channel=channel,
            recipient_emails=self._world.human_emails(channel, besides=message.user),
            thread_of=message.thread_ts,
        )

    def _summarised(self, root: wire.SlackMessage, every: list[wire.SlackMessage]) -> wire.SlackMessage:
        """A thread's parent carries its reply count, repliers and latest reply: the only sign in history that a thread exists."""
        replies = [m for m in every if m.thread_ts == root.ts and m.ts != root.ts]
        if not replies:
            return root
        users = list(dict.fromkeys(m.user for m in replies))
        return root.model_copy(update={
            "thread_ts": root.ts, "reply_count": len(replies), "reply_users": users,
            "reply_users_count": len(users), "latest_reply": replies[-1].ts,
        })

    # ------------------------------------------------------------------ auth, users

    def auth_test(self, presented: wire.Presented) -> wire.Ok:
        wire.read_args(wire.NoArgs, presented)
        self._world.saw(state.user_ref(BOT_USER_ID), Operation.READ)
        return wire.AuthTest(
            url="https://simulated.slack.com/", team=state.TEAM_NAME, user=state.BOT_NAME,
            team_id=state.TEAM_ID, user_id=BOT_USER_ID, bot_id=BOT_ID,
        )

    def users_list(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ListArgs, presented)
        limit = wire.page_size(args.limit)
        page = self._world.users(after=wire.decode_cursor(args.cursor), limit=limit + 1)
        more = len(page) > limit
        page = page[:limit]
        self._world.saw(state.team_ref(), Operation.SEARCH)
        return wire.UserList(
            members=page,
            response_metadata=wire.ResponseMetadata(next_cursor=wire.encode_cursor(page[-1].id) if more else ""),
        )

    def users_info(self, presented: wire.Presented) -> wire.Ok:
        user = self._user(wire.read_args(wire.UserArgs, presented).user)
        self._world.saw(state.user_ref(user.id), Operation.READ)
        return wire.OneUser(user=user)

    def users_lookup_by_email(self, presented: wire.Presented) -> wire.Ok:
        email = wire.read_args(wire.EmailArgs, presented).email.strip().lower()
        found = next(
            (u for u in self._world.every_user() if email and u.profile.email is not None
             and u.profile.email.lower() == email),
            None,
        )
        if found is None:
            raise wire.Refusal("users_not_found")
        self._world.saw(state.team_ref(), Operation.SEARCH)
        return wire.OneUser(user=found)

    # ------------------------------------------------------------------ conversations

    def conversations_list(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ConversationsListArgs, presented)
        types = {t.strip() for t in args.types.split(",") if t.strip()} or {"public_channel"}
        if not types <= wire.CONVERSATION_TYPES:
            raise wire.Refusal("invalid_types")
        limit = wire.page_size(args.limit)
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
        self._world.saw(state.team_ref(), Operation.SEARCH)
        return wire.ChannelList(
            channels=picked,
            response_metadata=wire.ResponseMetadata(next_cursor=wire.encode_cursor(picked[-1].id) if more else ""),
        )

    def conversations_info(self, presented: wire.Presented) -> wire.Ok:
        channel = self._channel(wire.read_args(wire.ChannelArgs, presented).channel)
        self._world.saw(state.channel_ref(channel.id), Operation.READ)
        return wire.OneChannel(channel=self._served(channel))

    def conversations_open(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ConversationsOpenArgs, presented)
        if args.channel and not args.users:
            channel = self._channel(args.channel)
            if not (channel.is_im or channel.is_mpim):
                raise wire.Refusal("channel_not_found")
            self._world.saw(state.channel_ref(channel.id), Operation.READ)
            return wire.Opened(no_op=True, already_open=True, channel=channel if args.return_im else wire.OpenedId(id=channel.id))
        wanted = [u.strip() for u in args.users.split(",") if u.strip()]
        if not wanted:
            raise wire.Refusal("users_list_not_supplied")
        if len(wanted) > _MAX_GROUP:
            raise wire.Refusal("too_many_users")
        others = [self._user(u).id for u in wanted]
        channel, opened = self._conversation(others, actor=Actor.AGENT)
        if not opened:
            self._world.saw(state.channel_ref(channel.id), Operation.READ)
        return wire.Opened(
            no_op=not opened, already_open=not opened,
            channel=channel if args.return_im else wire.OpenedId(id=channel.id),
        )

    def _conversation(self, others: list[str], *, actor: Actor) -> tuple[wire.SlackChannel, bool]:
        """The IM or group DM between the app and `others`, created when it does not exist yet."""
        members = sorted({BOT_USER_ID, *others})
        cid = state.conversation_id(members)
        existing = self._world.channel(cid)
        if existing is not None:
            return existing, False
        channel = self._world.open_conversation(members, created=int(self._clock.now().timestamp()), actor=actor)
        return channel, True

    def conversations_members(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.MembersArgs, presented)
        channel = self._channel(args.channel)
        limit = wire.page_size(args.limit)
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
        roots = [m for m in reversed(every) if m.thread_ts is None or m.thread_ts == m.ts]
        window = [
            m for m in roots
            if _within(Decimal(m.ts), oldest=oldest, latest=latest, inclusive=inclusive)
            and (resume is None or Decimal(m.ts) < wire.timestamp(resume, "invalid_cursor"))
        ]
        limit = wire.page_size(args.limit)
        page, more = window[:limit], len(window) > limit
        self._world.saw(state.channel_ref(channel.id), Operation.READ)
        return wire.MessageList(
            messages=[self._summarised(m, every) for m in page], has_more=more,
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
            m for m in every
            if m.thread_ts == root_ts and m.ts != root_ts
            and _within(Decimal(m.ts), oldest=oldest, latest=latest, inclusive=args.inclusive)
            and (resume is None or Decimal(m.ts) > wire.timestamp(resume, "invalid_cursor"))
        ]
        thread = replies if resume is not None else [self._summarised(root, every), *replies]
        limit = wire.page_size(args.limit)
        page, more = thread[:limit], len(thread) > limit
        self._world.saw(state.message_ref(root_ts), Operation.READ)
        return wire.MessageList(
            messages=page, has_more=more,
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
        wire.check_message(args.text, args.blocks)
        thread_ts: str | None = None
        if args.thread_ts:
            parent = self._world.message(channel.id, args.thread_ts)
            if parent is None:
                raise wire.Refusal("thread_not_found")
            thread_ts = parent.thread_ts or parent.ts
        message = wire.SlackMessage(
            ts=self._world.next_ts(self._clock), user=BOT_USER_ID, text=args.text, team=state.TEAM_ID,
            bot_id=BOT_ID, app_id=state.APP_ID, thread_ts=thread_ts, blocks=args.blocks, attachments=args.attachments,
        )
        self._world.write(
            state.message_ref(message.ts), message, operation=Operation.CREATE, actor=Actor.AGENT, parent=channel.id,
            after=self._snapshot(channel.id, message),
        )
        return wire.Posted(channel=channel.id, ts=message.ts, message=message)

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
        if message.user != BOT_USER_ID:
            raise wire.Refusal("cant_update_message")
        if args.text is None and args.blocks is None and args.attachments is None:
            raise wire.Refusal("no_text")
        text = message.text if args.text is None else args.text
        blocks = message.blocks if args.blocks is None else args.blocks
        wire.check_message(text, blocks)
        updated = message.model_copy(update={
            "text": text, "blocks": blocks,
            "attachments": message.attachments if args.attachments is None else args.attachments,
            "edited": wire.SlackEdited(user=BOT_USER_ID, ts=self._world.next_ts(self._clock)),
        })
        self._world.write(
            state.message_ref(updated.ts), updated, operation=Operation.UPDATE, actor=Actor.AGENT, parent=channel.id,
            after=self._snapshot(channel.id, updated),
        )
        return wire.Updated(channel=channel.id, ts=updated.ts, text=updated.text, message=updated)

    def chat_delete(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.DeleteArgs, presented)
        channel = self._channel(args.channel)
        message = self._world.message(channel.id, args.ts) if args.ts else None
        if message is None:
            raise wire.Refusal("message_not_found")
        if message.user != BOT_USER_ID:
            raise wire.Refusal("cant_delete_message")
        self._world.delete(state.message_ref(message.ts), actor=Actor.AGENT, parent=channel.id)
        return wire.Deleted(channel=channel.id, ts=message.ts)

    def reactions_add(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ReactionArgs, presented)
        channel = self._channel(args.channel)
        message = self._world.message(channel.id, args.timestamp) if args.timestamp else None
        if message is None:
            raise wire.Refusal("message_not_found")
        name = args.name.strip(":")
        if not name:
            raise wire.Refusal("invalid_name")
        reactions = list(message.reactions or [])
        same = next((r for r in reactions if r.name == name), None)
        if same is not None and BOT_USER_ID in same.users:
            raise wire.Refusal("already_reacted")
        if same is None:
            reactions.append(wire.SlackReaction(name=name, users=[BOT_USER_ID], count=1))
        else:
            reactions[reactions.index(same)] = wire.SlackReaction(
                name=name, users=[*same.users, BOT_USER_ID], count=same.count + 1
            )
        self._world.write(
            state.message_ref(message.ts), message.model_copy(update={"reactions": reactions}),
            operation=Operation.UPDATE, actor=Actor.AGENT, parent=channel.id,
        )
        return wire.Ok()


def _within(ts: Decimal, *, oldest: Decimal | None, latest: Decimal | None, inclusive: bool) -> bool:
    if oldest is not None and (ts < oldest if inclusive else ts <= oldest):
        return False
    if latest is not None and (ts > latest if inclusive else ts >= latest):
        return False
    return True


def build_app(store: Store, clock: Clock) -> Starlette:
    api = SlackApi(store, clock)
    endpoint: Callable[[Request], Awaitable[Response]] = api.endpoint
    return Starlette(routes=[Route("/api/{method}", endpoint, methods=["GET", "POST"])])
