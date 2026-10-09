"""What every Web API call shares: the workspace it is answered in, the run's clock, the conversations it can see, and the
events it sets off (`pushing`)."""

from __future__ import annotations

from collections.abc import Callable

from minutehand.adapters.providers.slack import inbound, state, wire
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.domain.world import Actor, Operation, RecordSnapshot, WorldEvent
from minutehand.ports.clock import Clock
from minutehand.ports.provider import Wakes
from minutehand.ports.store import Store


class Calls:
    def __init__(self, store: Store, clock: Clock, booking: Callable[[], Wakes | None]) -> None:
        self._store = store
        self._booking = booking
        """The dispatch table the run books what falls due later in, when the provider is bound to one."""
        self._world = SlackWorld(store)
        """The workspace the call being answered is in: set for each call before its method runs."""
        self._clock = clock
        self._emitted: list[wire.EventCallback] = []
        """The events the call being answered has set off, sent once it is answered."""

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

    def _now(self) -> int:
        return int(self._clock.now().timestamp())

    # ------------------------------------------------------------------ events

    def _emit(self, event: wire.Event, seq: int) -> None:
        """The agent's own change, at log position `seq`, sets `event` off: it is sent once the call is answered,
        and carries that position as its `event_id`."""
        self._emitted.append(inbound.callback(self._world, event, seq=seq, clock=self._clock))

    def _recorded(self, text: str, resource: str = "channels") -> RecordSnapshot:
        return RecordSnapshot(resource=resource, text=text)

    def _write_channel(self, channel: wire.SlackChannel, operation: Operation, text: str) -> WorldEvent:
        return self._world.write(
            state.channel_ref(channel.id),
            channel,
            operation=operation,
            actor=Actor.AGENT,
            parent=self._world.team.id,
            after=self._recorded(text),
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
