"""Playing the agent's recorded inputs into a fresh sandbox, up to a checkpoint: how a fork brings the agent back.

The run's record holds everything the agent received, since every input came through Minutehand: the clock (each
wake's moment, in the checkpoints), what every call it made was answered (`RecordedCall`), what its memory read back
(the log), and everything the world sent it (`PushSnapshot`: a pushed event, a delivery, a decision, every header
and byte). A playback starts the agent's sandbox at the run's start, then, wake by wake, moves the sandbox's clock
to the wake's moment, sends the agent what the world sent it then, and answers every call it makes from the record
(`application.trace`), until the sandbox is idle again. At the checkpoint's wake the agent stands where it stood:
the same processes, the same timers armed, the same state in its own memory and files. Nothing is written: the
world the record was made against is the one the fork shares.

What parts a playback: a call the record does not hold next, or a recorded call the agent never makes again. Both
mean the agent did not do what it did before, and the fork is refused there (`application.restore`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from minutehand.application.checkpoint import Checkpoint, checkpoints
from minutehand.application.refusals import RunRefused
from minutehand.application.restore import PlaybackParted, Played
from minutehand.application.sandbox import SandboxClock
from minutehand.application.trace import TraceReplay, Unplayable
from minutehand.application.traffic import Traffic
from minutehand.domain.scenario import ProviderKey
from minutehand.domain.world import EntityKind, InboxAct, PushSnapshot
from minutehand.ports.services import Delivered
from minutehand.ports.store import Store


class PlaysBack(Protocol):
    """Whoever answers the agent's calls (the proxy): from the record while a playback is on, from the fakes after."""

    def play_back(self, trace: TraceReplay) -> None: ...

    def live(self) -> None: ...


class HoldsMemory(Protocol):
    """Whoever answers the agent's memory (the receiver): as of `cursor()` in `parent` while a playback is on, with
    nothing it writes recorded; from the run after."""

    def playing_back(self, parent: Store, cursor: Callable[[], int]) -> None: ...

    def live(self) -> None: ...


class Resends(Protocol):
    """The door everything the world sends the agent goes through (`adapters.agent.delivery`), sending one again."""

    async def resend(self, snapshot: PushSnapshot) -> Delivered: ...


@dataclass(frozen=True)
class TracePlayback:
    """`application.restore.Playback` over a parent run's record, up to the checkpoint at `at_seq`."""

    parent: Store
    checkpoint: Checkpoint
    at_seq: int
    starts_at: datetime
    sandbox: SandboxClock
    proxy: PlaysBack
    memory: HoldsMemory
    door: Resends
    traffic: Traffic | None
    unrecorded_senders: frozenset[ProviderKey] = frozenset()
    """Providers in the run whose requests to the agent the door does not yet carry (`Manifest.sends`)."""
    unrecorded_channels: frozenset[str] = frozenset()
    """Captured channels people answer through (`outbound[].replies`), whose deliveries the door does not yet carry."""

    async def play(self, progress: Callable[[str], None]) -> Played:
        missing = sends_not_recorded(
            self.parent, self.at_seq, senders=self.unrecorded_senders, channels=self.unrecorded_channels
        )
        if missing:
            raise RunRefused(
                f"the record of run {self.parent.run_id} up to seq {self.at_seq} holds requests to the agent that were "
                f"not recorded whole, so a fork cannot send them again: {'; '.join(missing)}. Fork from a checkpoint "
                "before the first, or rerun from the beginning"
            )
        calls = [c for c in self.parent.calls() if c.wake <= self.checkpoint.wake and c.last_seq <= self.at_seq]
        try:
            trace = TraceReplay.of(self.parent.run_id, calls)
        except Unplayable as e:
            raise RunRefused(f"the record of run {self.parent.run_id} cannot be played back: {e}") from e
        moments = self._moments()
        sends = self._sends()
        floor = 0
        resent = 0
        await self.sandbox.sync(self.starts_at)
        jumps = 1
        was = self.starts_at
        self.proxy.play_back(trace)
        self.memory.playing_back(self.parent, lambda: max(trace.reached_seq, floor))
        try:
            for wake in range(1, self.checkpoint.wake + 1):
                at = moments.get(wake, was)
                if at > was:
                    await self.sandbox.advance(int((at - was).total_seconds() * 1_000_000_000))
                    jumps += 1
                    was = at
                for push in sends.get(wake, []):
                    await self.door.resend(push)
                    resent += 1
                if await self.sandbox.settle(self.traffic) is None:
                    raise RunRefused(
                        f"the agent's sandbox did not fall idle within its settle limit while wake {wake} of run "
                        f"{self.parent.run_id} was played back into it"
                    )
                if trace.parted is not None:
                    raise PlaybackParted(trace.parted, sent=_sent(trace), held=trace.parted_held)
                floor = self._seq_ending(wake)
                progress(f"playback: wake {wake} of {self.checkpoint.wake}, {trace.answered} call(s) answered")
            left = trace.remaining
            if left:
                first = left[0]
                raise PlaybackParted(
                    f"the playback of run {self.parent.run_id} parted: the agent made {len(left)} call(s) fewer than "
                    f"the record holds up to the checkpoint, the first missing {first.exchange.method} "
                    f"{first.exchange.host}{first.exchange.path.split('?', 1)[0]} (wake {first.wake})",
                    sent="nothing where the record holds a call",
                    held=f"{first.exchange.method} {first.exchange.host}{first.exchange.path.split('?', 1)[0]} "
                    f"(wake {first.wake})",
                )
        finally:
            self.proxy.live()
            self.memory.live()
        return Played(calls_answered=trace.answered, deliveries_resent=resent, jumps=jumps)

    def _moments(self) -> dict[int, datetime]:
        """The moment each wake ran at: the clock at the first checkpoint that ended it."""
        found: dict[int, datetime] = {}
        for seq, c in checkpoints(self.parent).items():
            if seq <= self.at_seq and c.wake not in found:
                found[c.wake] = c.now
        return found

    def _seq_ending(self, wake: int) -> int:
        """The seq of the checkpoint that ended `wake`: as of where the agent's memory reads are answered before its
        first call of the next wake."""
        return next((seq for seq, c in checkpoints(self.parent).items() if c.wake == wake and seq <= self.at_seq), 0)

    def _sends(self) -> dict[int, list[PushSnapshot]]:
        """Everything the world sent the agent, by the wake it was sent in, in order."""
        found: dict[int, list[PushSnapshot]] = {}
        for event in self.parent.events(until=self.at_seq, kinds=[EntityKind.PUSH]):
            if isinstance(event.after, PushSnapshot):
                found.setdefault(event.wake, []).append(event.after)
        return found


def _sent(trace: TraceReplay) -> str:
    said = trace.parted or ""
    return said.split("the agent sent ", 1)[1].split(", which", 1)[0] if "the agent sent " in said else said


def sends_not_recorded(
    parent: Store, at_seq: int, *, senders: frozenset[ProviderKey], channels: frozenset[str]
) -> list[str]:
    """What the record up to `at_seq` holds of requests to the agent that went outside the door, each named: a
    provider among `senders` that acted in the run by then (its sends leave no whole record, so nothing can be
    sent again), a decision or use Minutehand made in the agent's own product (`InboxCall`), and a person's answer
    on one of `channels`. Empty when every request the agent received is in the record."""
    found: list[str] = []
    acted = {e.entity.provider for e in parent.events(until=at_seq, providers=sorted(senders))} if senders else set()
    found += [f"{key}'s requests to the agent (pushed events, webhooks, deliveries)" for key in sorted(acted)]
    inbox = {
        c.exchange.inbox_call.inbox
        for c in parent.calls()
        if c.exchange.inbox_call is not None
        and c.exchange.inbox_call.act is not InboxAct.LIST
        and c.first_seq - 1 <= at_seq
    }
    found += [f"decisions and uses made as a person in the agent's own product ({name})" for name in sorted(inbox)]
    answered = (
        {
            e.entity.provider
            for e in parent.events(until=at_seq, providers=sorted(channels))
            if e.entity.kind is EntityKind.MESSAGE
        }
        if channels
        else set()
    )
    found += [f"people's answers delivered on the captured channel {name!r}" for name in sorted(answered)]
    return found
