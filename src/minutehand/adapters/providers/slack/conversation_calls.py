"""The Web API methods that make, change and populate conversations: `conversations.create`, `.join`, `.invite`,
`.kick`, `.leave`, `.archive`, `.unarchive`, `.rename`, `.setTopic`, `.setPurpose`, and `users.conversations`.

Each method is Slack's as its page under https://docs.slack.dev/reference/methods/ documents it (`CLAIMS.md` lists the
claims and their tests). Where a page leaves a case open the call is refused 501 by name (`NotServed`) rather than
answered by a guess: the conversation types a method cannot be used on, a name Slack "will modify", a force that
meets an invalid user.
"""

from __future__ import annotations

import string

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.calls import Calls
from minutehand.domain.errors import NotServed
from minutehand.domain.world import Actor, Operation

MAX_NAME = 80
"""A channel name is "80 characters or less" (https://docs.slack.dev/reference/methods/conversations.create)."""
MAX_TEXT = 250
"""A topic or a purpose longer than this is `too_long` (https://docs.slack.dev/reference/methods/conversations.setTopic)."""
MAX_INVITED = 1000
"""`conversations.invite` takes "up to 1000 users" (https://docs.slack.dev/reference/methods/conversations.invite)."""
_ALLOWED = frozenset(string.ascii_lowercase + string.digits + "-_")
BOT_TOKEN_PREFIX = "xoxb-"
"""Slack's own shape of a bot token."""


class ConversationCalls(Calls):
    # ------------------------------------------------------------------ shapes

    def _full(self, channel: wire.SlackChannel) -> wire.SlackChannel:
        """The conversation object as `conversations.info` serves a channel: the fields Slack computes beside the ones
        the scenario or the agent gave. A direct message carries none of them in Slack's examples."""
        if channel.is_im:
            return channel
        team = self._world.team.id
        conforming = bool(channel.name) and all(c in _ALLOWED for c in channel.name or "")
        return channel.model_copy(
            update={
                "unlinked": 0,
                "name_normalized": channel.name if conforming else None,
                "is_shared": False,
                "is_frozen": False,
                "is_org_shared": False,
                "is_pending_ext_shared": False,
                "pending_shared": [],
                "context_team_id": team,
                "is_ext_shared": False,
                "shared_team_ids": [team],
                "pending_connected_team_ids": [],
            }
        )

    def _stamp(self, written_seq: int) -> str:
        return f"{self._now()}.{written_seq:06d}"

    def _refuse_direct(self, channel: wire.SlackChannel, method: str) -> None:
        """Slack says "not all types of conversations" (or "some") can be used with these methods and names no type."""
        if channel.is_im or channel.is_mpim:
            raise NotServed(f"{method} on a direct message, which its page does not say it serves or refuses")

    def _joined_event(self, channel: wire.SlackChannel, user: str, seq: int, inviter: str | None) -> None:
        self._emit(
            wire.MemberJoinedEvent(
                user=user,
                channel=channel.id,
                channel_type="C",
                team=self._world.team.id,
                inviter=inviter,
                event_ts=self._stamp(seq),
            ),
            seq,
        )

    def _add_member(self, channel: wire.SlackChannel, user: str, inviter: str | None) -> None:
        written = self._world.write(
            state.membership_ref(channel.id, user),
            wire.SlackMembership(channel=channel.id, user=user),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            parent=channel.id,
            after=self._recorded(f"{user} joined {channel.name or channel.id}", "memberships"),
        )
        self._joined_event(channel, user, written.seq, inviter)

    # ------------------------------------------------------------------ names

    def _valid_name(self, name: str) -> str:
        """A channel name as `conversations.create` validates it: "Channel names may only contain lowercase letters,
        numbers, hyphens, and underscores, and must be 80 characters or less", refused with the code its page gives
        for each way to break that."""
        if not name:
            raise wire.Refusal("invalid_name_required")
        if len(name) > MAX_NAME:
            raise wire.Refusal("invalid_name_maxlength")
        if all(c in string.punctuation for c in name):
            raise wire.Refusal("invalid_name_punctuation")
        if not any(c.isalnum() for c in name):
            raise NotServed("a channel name of no letter, no number and no punctuation, which no page gives a code for")
        if any(c not in _ALLOWED for c in name):
            raise wire.Refusal("invalid_name_specials")
        return name

    def _refuse_taken(self, name: str, besides: str | None) -> None:
        for other in self._world.channels_after(None):
            if other.name != name or other.id == besides or other.is_im:
                continue
            if other.is_archived:
                raise NotServed(f"a channel name ({name}) that an archived channel holds, which no page speaks of")
            raise wire.Refusal("name_taken")

    # ------------------------------------------------------------------ methods

    def conversations_create(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.CreateArgs, presented)
        name = self._valid_name(args.name)
        self._refuse_taken(name, None)
        bot = self._world.bot
        now = self._now()
        cid = state.named_channel_id(name, self._world.team.id)
        if self._world.channel(cid) is not None or self._store.versions(state.channel_ref(cid)):
            cid = state.created_channel_id(self._world.next_seq(), self._world.team.id)
        nothing = wire.SlackTopic()
        channel = wire.SlackChannel(
            id=cid,
            name=name,
            is_channel=not args.is_private,
            is_group=args.is_private,
            is_private=args.is_private,
            created=now,
            creator=bot,
            topic=nothing,
            purpose=nothing,
        )
        written = self._write_channel(channel, Operation.CREATE, f"created {name}")
        if not args.is_private:
            self._emit(
                wire.ChannelCreatedEvent(channel=wire.CreatedChannel(id=cid, name=name, created=now, creator=bot)),
                written.seq,
            )
        self._add_member(channel, bot, None)
        return wire.OneChannel(channel=self._full(self._served(channel)))

    def conversations_join(self, presented: wire.Presented) -> wire.Ok:
        channel = self._channel(wire.read_args(wire.ChannelArgs, presented).channel)
        self._refuse_direct(channel, "conversations.join")
        if channel.is_archived:
            raise wire.Refusal("is_archived")
        if self._in(channel):
            return wire.Joined(
                channel=self._full(self._served(channel)),
                warning="already_in_channel",
                response_metadata=wire.ResponseMetadataWarnings(warnings=["already_in_channel"]),
            )
        self._add_member(channel, self._world.bot, None)
        return wire.Joined(channel=self._full(self._served(channel)))

    def _invitable(self, channel: wire.SlackChannel, wanted: list[str]) -> list[wire.UserRefused]:
        refused: list[wire.UserRefused] = []
        for given in wanted:
            user = self._world.user(given)
            if user is None:
                refused.append(wire.UserRefused(user=given, error="user_not_found"))
            elif user.id == self._world.bot:
                refused.append(wire.UserRefused(user=given, error="cant_invite_self"))
            elif self._world.is_member(channel.id, user.id):
                refused.append(wire.UserRefused(user=given, error="already_in_channel"))
            elif user.deleted or user.is_bot or (user.is_restricted and not user.is_ultra_restricted):
                raise NotServed(
                    f"inviting {given}, a deactivated member, another app's bot or a guest: the page names no code "
                    "for them"
                )
            elif user.is_ultra_restricted and self._channels_of(user.id):
                refused.append(wire.UserRefused(user=given, error="ura_max_channels"))
        return refused

    def _channels_of(self, user: str) -> list[str]:
        """The channels a member is in, not counting direct messages."""
        return [
            c.id
            for c in self._world.channels_after(None)
            if not (c.is_im or c.is_mpim) and self._world.is_member(c.id, user)
        ]

    def conversations_invite(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.InviteArgs, presented)
        channel = self._channel(args.channel)
        self._refuse_direct(channel, "conversations.invite")
        if not self._in(channel):
            raise wire.Refusal("not_in_channel")
        if channel.is_archived:
            raise wire.Refusal("is_archived")
        wanted = list(dict.fromkeys(u.strip() for u in args.users.split(",") if u.strip()))
        if not wanted:
            raise wire.Refusal("no_user")
        if len(wanted) > MAX_INVITED:
            raise NotServed(f"an invitation of more than {MAX_INVITED} users, which the page says is not allowed")
        refused = self._invitable(channel, wanted)
        if refused and args.force:
            raise NotServed("force with an invalid user, whose answer the page does not give")
        if refused:
            raise wire.Refusal(refused[0].error, refused)
        for user in wanted:
            self._add_member(channel, user, self._world.bot)
        return wire.OneChannel(channel=self._full(self._served(channel)))

    def conversations_kick(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.KickArgs, presented)
        channel = self._channel(args.channel)
        self._refuse_direct(channel, "conversations.kick")
        if not args.user:
            raise wire.Refusal("no_user")
        user = self._user(args.user)
        if user.id == self._world.bot:
            raise wire.Refusal("cant_kick_self")
        if channel.is_general:
            raise wire.Refusal("cant_kick_from_general")
        if not self._in(channel):
            raise NotServed("conversations.kick by an app that is not in the channel, which its page does not speak of")
        if channel.is_archived:
            raise NotServed("conversations.kick in an archived channel, which its page does not speak of")
        if not self._world.is_member(channel.id, user.id):
            raise wire.Refusal("not_in_channel")
        self._world.delete(
            state.membership_ref(channel.id, user.id),
            actor=Actor.AGENT,
            parent=channel.id,
            before=self._recorded(f"{user.id} removed from {channel.name or channel.id}", "memberships"),
        )
        return wire.Kicked()

    def conversations_leave(self, presented: wire.Presented) -> wire.Ok | wire.NotInChannelNotice:
        channel = self._channel(wire.read_args(wire.ChannelArgs, presented).channel)
        self._refuse_direct(channel, "conversations.leave")
        if channel.is_archived:
            raise wire.Refusal("is_archived")
        if not self._in(channel):
            return wire.NotInChannelNotice()
        if channel.is_general:
            raise wire.Refusal("cant_leave_general")
        if self._world.every_member(channel.id) == [self._world.bot]:
            raise wire.Refusal("last_member")
        bot = self._world.bot
        written = self._world.delete(
            state.membership_ref(channel.id, bot),
            actor=Actor.AGENT,
            parent=channel.id,
            before=self._recorded(f"{bot} left {channel.name or channel.id}", "memberships"),
        )
        self._emit(
            wire.MemberLeftEvent(user=bot, channel=channel.id, channel_type="C", team=self._world.team.id),
            written.seq,
        )
        return wire.Ok()

    def conversations_archive(self, presented: wire.Presented) -> wire.Ok:
        channel = self._channel(wire.read_args(wire.ChannelArgs, presented).channel)
        self._refuse_direct(channel, "conversations.archive")
        if channel.is_general:
            raise wire.Refusal("cant_archive_general")
        if channel.is_archived:
            raise wire.Refusal("already_archived")
        if not self._in(channel):
            raise wire.Refusal("not_in_channel")
        written = self._write_channel(
            channel.model_copy(update={"is_archived": True}), Operation.UPDATE, f"archived {channel.name}"
        )
        if not channel.is_private:
            self._emit(wire.ChannelArchiveEvent(channel=channel.id, user=self._world.bot), written.seq)
        return wire.Ok()

    def conversations_unarchive(self, presented: wire.Presented) -> wire.Ok:
        if presented.token is not None and presented.token.startswith(BOT_TOKEN_PREFIX):
            raise NotServed(
                "conversations.unarchive with a bot token, which its page says cannot currently unarchive and gives "
                "no error for"
            )
        channel = self._channel(wire.read_args(wire.ChannelArgs, presented).channel)
        self._refuse_direct(channel, "conversations.unarchive")
        if not channel.is_archived:
            raise wire.Refusal("not_archived")
        reopened = channel.model_copy(update={"is_archived": False})
        self._write_channel(reopened, Operation.UPDATE, f"unarchived {channel.name}")
        if not self._in(channel):
            self._add_member(reopened, self._world.bot, None)
        return wire.Ok()

    def conversations_rename(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.RenameArgs, presented)
        channel = self._channel(args.channel)
        self._refuse_direct(channel, "conversations.rename")
        if not self._in(channel):
            raise wire.Refusal("not_in_channel")
        if channel.is_archived:
            raise wire.Refusal("is_archived")
        if not args.name or len(args.name) > MAX_NAME or any(c not in _ALLOWED for c in args.name):
            raise NotServed(
                "conversations.rename to a name outside Slack's naming rule, which its page says Slack validates and "
                "also modifies, without saying how"
            )
        user = self._user(self._world.bot)
        if channel.creator != self._world.bot and not (user.is_admin or user.is_owner):
            raise wire.Refusal("not_authorized")
        if args.name == channel.name:
            raise NotServed("conversations.rename to the channel's own name, which its page does not speak of")
        self._refuse_taken(args.name, channel.id)
        renamed = channel.model_copy(
            update={
                "name": args.name,
                "previous_names": [*channel.previous_names, *([channel.name] if channel.name else [])],
            }
        )
        written = self._write_channel(renamed, Operation.UPDATE, f"renamed {channel.name} to {args.name}")
        if not channel.is_private:
            self._emit(
                wire.ChannelRenameEvent(
                    channel=wire.RenamedChannel(id=channel.id, name=args.name, created=channel.created)
                ),
                written.seq,
            )
        return wire.OneChannel(channel=self._full(self._served(renamed)))

    def _described(self, channel: wire.SlackChannel, method: str, text: str | None) -> wire.SlackTopic:
        self._refuse_direct(channel, method)
        if text is None:
            raise wire.Refusal("invalid_arguments")
        if not self._in(channel):
            raise wire.Refusal("not_in_channel")
        if channel.is_archived:
            raise wire.Refusal("is_archived")
        if len(text) > MAX_TEXT:
            raise wire.Refusal("too_long")
        return wire.SlackTopic(value=text, creator=self._world.bot, last_set=self._now())

    def conversations_set_topic(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.TopicArgs, presented)
        channel = self._channel(args.channel)
        topic = self._described(channel, "conversations.setTopic", args.topic)
        changed = channel.model_copy(update={"topic": topic})
        self._write_channel(changed, Operation.UPDATE, f"topic of {channel.name}: {topic.value}")
        return wire.OneChannel(channel=self._full(self._served(changed)))

    def conversations_set_purpose(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.PurposeArgs, presented)
        channel = self._channel(args.channel)
        purpose = self._described(channel, "conversations.setPurpose", args.purpose)
        self._write_channel(
            channel.model_copy(update={"purpose": purpose}),
            Operation.UPDATE,
            f"purpose of {channel.name}: {purpose.value}",
        )
        return wire.Purposed(purpose=purpose.value)

    def users_conversations(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.UsersConversationsArgs, presented)
        types = {t.strip() for t in args.types.split(",") if t.strip()} or {"public_channel"}
        if not types <= wire.CONVERSATION_TYPES:
            raise wire.Refusal("invalid_types")
        limit = wire.page_size(args.limit, default=100, most=999)
        member = self._user(args.user).id if args.user else self._world.bot
        picked: list[wire.SlackChannel] = []
        more = False
        for channel in self._world.channels_after(wire.decode_cursor(args.cursor)):
            if wire.conversation_type(channel) not in types or (args.exclude_archived and channel.is_archived):
                continue
            if not self._world.is_member(channel.id, member):
                continue
            if (channel.is_private or channel.is_im or channel.is_mpim) and not self._in(channel):
                continue
            if len(picked) == limit:
                more = True
                break
            picked.append(channel)
        self._world.saw(state.team_ref(self._world.team.id), Operation.SEARCH)
        return wire.ChannelList(
            channels=picked,
            response_metadata=wire.ResponseMetadata(next_cursor=wire.encode_cursor(picked[-1].id) if more else ""),
        )
