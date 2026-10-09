"""The Web API methods about one message: `chat.getPermalink`, `reactions.remove`, `.get` and `.list`, and `pins.add`,
`.remove` and `.list`.

Each is Slack's as its page under https://docs.slack.dev/reference/methods/ documents it (`CLAIMS.md` lists the claims
and their tests); the arguments for a file or a file comment, and the cases a page leaves open, are refused 501 by name.
"""

from __future__ import annotations

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.conversation_calls import ConversationCalls
from minutehand.domain.errors import NotServed
from minutehand.domain.world import Actor, EntityKind, Operation


class MessageCalls(ConversationCalls):
    def _permalink(self, channel: str, message: wire.SlackMessage) -> str:
        """`https://<domain>.slack.com/archives/<channel>/p<ts without its dot>`, and for a reply the thread's parent
        and channel in the query, as the examples of https://docs.slack.dev/reference/methods/chat.getPermalink show."""
        base = f"https://{self._world.team.domain}.slack.com/archives/{channel}/p{message.ts.replace('.', '')}"
        if message.thread_ts is not None and message.thread_ts != message.ts:
            return f"{base}?thread_ts={message.thread_ts}&cid={channel}"
        return base

    def _message_at(self, channel: wire.SlackChannel, ts: str) -> wire.SlackMessage:
        """The message a `channel` and `timestamp` name: `no_item_specified` for none, `bad_timestamp` for one that is no
        stamp, `message_not_found` for one nobody posted."""
        if not ts:
            raise wire.Refusal("no_item_specified")
        wire.timestamp(ts, "bad_timestamp")
        message = self._world.message(channel.id, ts)
        if message is None:
            raise wire.Refusal("message_not_found")
        return message

    def _refuse_files(self, file: str, file_comment: str, method: str) -> None:
        if file or file_comment:
            raise NotServed(f"{method} on a file or a file comment, which this fake does not serve")

    # ------------------------------------------------------------------ chat.getPermalink

    def chat_get_permalink(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.PermalinkArgs, presented)
        channel = self._channel(args.channel)
        if not args.message_ts:
            raise wire.Refusal("invalid_arguments")
        message = self._world.message(channel.id, args.message_ts)
        if message is None:
            raise wire.Refusal("message_not_found")
        self._world.saw(state.message_ref(message.ts), Operation.READ)
        return wire.Permalink(channel=channel.id, permalink=self._permalink(channel.id, message))

    # ------------------------------------------------------------------ reactions

    def reactions_remove(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ReactionRemoveArgs, presented)
        self._refuse_files(args.file, args.file_comment, "reactions.remove")
        if not args.name:
            raise wire.Refusal("invalid_name")
        if not args.channel and not args.timestamp:
            raise wire.Refusal("no_item_specified")
        channel = self._channel(args.channel)
        message = self._message_at(channel, args.timestamp)
        bot = self._world.bot
        reactions = list(message.reactions or [])
        same = next((r for r in reactions if r.name == args.name), None)
        if same is None or bot not in same.users:
            raise wire.Refusal("no_reaction")
        kept = [u for u in same.users if u != bot]
        if kept:
            reactions[reactions.index(same)] = wire.SlackReaction(name=same.name, users=kept, count=same.count - 1)
        else:
            reactions.remove(same)
        written = self._world.write(
            state.message_ref(message.ts),
            message.model_copy(update={"reactions": reactions or None}),
            operation=Operation.UPDATE,
            actor=Actor.AGENT,
            parent=channel.id,
        )
        self._emit(
            wire.ReactionRemovedEvent(
                user=bot,
                reaction=args.name,
                item_user=message.user,
                item=wire.ReactionItem(channel=channel.id, ts=message.ts),
                event_ts=self._stamp(written.seq),
            ),
            written.seq,
        )
        return wire.Ok()

    def reactions_get(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ReactionGetArgs, presented)
        self._refuse_files(args.file, args.file_comment, "reactions.get")
        if not args.channel and not args.timestamp:
            raise wire.Refusal("no_item_specified")
        channel = self._channel(args.channel)
        message = self._message_at(channel, args.timestamp)
        self._world.saw(state.message_ref(message.ts), Operation.READ)
        served = self._summarised(message, self._world.messages(channel.id))
        return wire.ReactedMessage(
            message=served.model_copy(update={"permalink": self._permalink(channel.id, message)}), channel=channel.id
        )

    def reactions_list(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.ReactionsListArgs, presented)
        member = self._user(args.user).id if args.user else self._world.bot
        wire.decode_cursor(args.cursor)
        found: list[wire.ReactedItem] = []
        for channel in self._world.channels_after(None):
            if (channel.is_private or channel.is_im or channel.is_mpim) and not self._in(channel):
                continue
            every = self._world.messages(channel.id)
            for message in every:
                for reaction in message.reactions or []:
                    if member in reaction.users:
                        found.append(wire.ReactedItem(channel=channel.id, message=self._summarised(message, every)))
        if len(found) > 1:
            raise NotServed(
                "reactions.list of more than one item: its page does not say in what order, nor whether the message "
                "of an item lists every reaction or the one it is listed for"
            )
        self._world.saw(state.team_ref(self._world.team.id), Operation.SEARCH)
        return wire.ReactionsListed(items=found, response_metadata=wire.ResponseMetadata())

    # ------------------------------------------------------------------ pins

    def _pinnable(self, channel: wire.SlackChannel, ts: str) -> wire.SlackMessage:
        if not self._in(channel):
            raise NotServed("a pin by an app that is not in the channel, which the pages of pins.* do not speak of")
        message = self._message_at(channel, ts)
        if message.files:
            raise NotServed("pinning a message that shares a file: pins.add says files cannot be pinned, not messages")
        return message

    def pins_add(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.PinArgs, presented)
        channel = self._channel(args.channel)
        message = self._pinnable(channel, args.timestamp)
        if self._world.body(state.pin_ref(channel.id, message.ts), wire.SlackPin) is not None:
            raise wire.Refusal("already_pinned")
        self._world.write(
            state.pin_ref(channel.id, message.ts),
            wire.SlackPin(channel=channel.id, ts=message.ts, created=self._now(), created_by=self._world.bot),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            parent=state.pins_of(channel.id),
            after=self._recorded(f"pinned {message.ts} in {channel.name or channel.id}", "pins"),
        )
        return wire.Ok()

    def pins_remove(self, presented: wire.Presented) -> wire.Ok:
        args = wire.read_args(wire.PinArgs, presented)
        channel = self._channel(args.channel)
        message = self._pinnable(channel, args.timestamp)
        if self._world.body(state.pin_ref(channel.id, message.ts), wire.SlackPin) is None:
            raise wire.Refusal("no_pin")
        self._world.delete(
            state.pin_ref(channel.id, message.ts),
            actor=Actor.AGENT,
            parent=state.pins_of(channel.id),
            before=self._recorded(f"unpinned {message.ts} in {channel.name or channel.id}", "pins"),
        )
        return wire.Ok()

    def pins_list(self, presented: wire.Presented) -> wire.Ok:
        channel = self._channel(wire.read_args(wire.ChannelArgs, presented).channel)
        pins = self._world.bodies(EntityKind.RECORD, state.pins_of(channel.id), wire.SlackPin)
        every = self._world.messages(channel.id)
        items: list[wire.PinnedItem] = []
        for pin in sorted(pins, key=lambda p: (p.created, p.ts), reverse=True):
            message = self._world.message(channel.id, pin.ts)
            if message is None:
                continue
            served = self._summarised(message, every).model_copy(
                update={"permalink": self._permalink(channel.id, message), "pinned_to": [channel.id]}
            )
            items.append(
                wire.PinnedItem(channel=channel.id, created=pin.created, created_by=pin.created_by, message=served)
            )
        self._world.saw(state.channel_ref(channel.id), Operation.READ)
        return wire.PinsListed(items=items)
