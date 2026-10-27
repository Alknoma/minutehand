"""Playing the agent's recorded inputs into a fresh sandbox, up to a checkpoint: how a fork brings the agent back.

The run's record holds everything the agent received, since every input came through Minutehand: the clock (each
wake's moment, in the checkpoints), what every call it made was answered (`RecordedCall`), what its memory read back
(the log), and everything the world sent it (`PushSnapshot`: a pushed event, a delivery, a decision, every header
and byte). A playback starts the agent's sandbox at the run's start, then, wake by wake, moves the sandbox's clock
to the wake's moment, sends the agent what the world sent it then, and answers every call it makes from the record
(`application.trace`), until the sandbox is idle again. A wake the run began for due records ended, when it was
played, once the agent had written one of each declaration's (`application.due_records`) and the sandbox had
settled; played back, it ends at the same point: once the agent has written again each of those records it wrote in
that wake, and the sandbox has settled. Its poller looks on its own timer while the sandbox's clock runs on, and a
wake ended at the first idle would leave that look to the next wake, its reads answered as of a later moment. At the checkpoint's wake the agent stands where it stood:
the same processes, the same timers armed, the same state in its own memory and files. Nothing is written: the
world the record was made against is the one the fork shares.

What parts a playback: a call the record does not hold next, or a recorded call the agent never makes again. Both
mean the agent did not do what it did before, and the fork is refused there (`application.restore`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from minutehand.application.checkpoint import Checkpoint, checkpoints
from minutehand.application.dues import due_entries
from minutehand.application.refusals import RunRefused
from minutehand.application.restore import PlaybackParted, Played
from minutehand.application.sandbox import SandboxClock
from minutehand.application.trace import TraceReplay, Unplayable
from minutehand.application.traffic import Traffic
from minutehand.domain.clock import DueClosed, DueSource
from minutehand.domain.memory import DueRecords
from minutehand.domain.world import Actor, EntityKind, PushSnapshot, written
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

    def written(self, collection: str, key: str) -> int:
        """How many of the agent's writes of one key have landed since `playing_back`, a delete's among them."""
        ...

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
    due: Sequence[DueRecords]

    async def play(self, progress: Callable[[str], None], *, start: Callable[[], Awaitable[None]]) -> Played:
        calls = [c for c in self.parent.calls() if c.wake <= self.checkpoint.wake and c.last_seq <= self.at_seq]
        try:
            trace = TraceReplay.of(self.parent.run_id, calls)
        except Unplayable as e:
            raise RunRefused(f"the record of run {self.parent.run_id} cannot be played back: {e}") from e
        moments = self._moments()
        sends = self._sends()
        picked = self._picked()
        floor = 0
        resent = 0
        jumps = 1
        was = self.starts_at
        self.proxy.play_back(trace)
        self.memory.playing_back(self.parent, lambda: max(trace.reached_seq, floor))
        try:
            # the agent is started once the record answers, as the parent's START wake started it once the world was
            # seeded: its calls on starting are wake 1's, and its sandbox is born at the run's start
            await start()
            await self.sandbox.sync(self.starts_at)
            for wake in range(1, self.checkpoint.wake + 1):
                at = moments.get(wake, was)
                if at > was:
                    # set, not stepped: the sandbox's clock ran on by the real time the agent spent in the wake
                    # before, as it did in the parent, and the parent's record holds the moments that followed
                    await self.sandbox.sync(at)
                    jumps += 1
                    was = at
                keys = picked.get(wake, {})
                before = {k: self.memory.written(*k) for k in keys}
                for push in sends.get(wake, []):
                    await self.door.resend(push)
                    resent += 1
                await self._written_again(wake, at, keys, before)
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

    async def _written_again(
        self, wake: int, at: datetime, keys: dict[tuple[str, str], timedelta], before: dict[tuple[str, str], int]
    ) -> None:
        """Wait until the agent has written again each record `keys` names, the due records it wrote in `wake` when
        it was played: its poller looks on its own timer while the sandbox's clock runs on. Its clock one `poll` past
        `at` with one not written is the agent not doing what it did before, and parts the playback there."""
        if not keys:
            return
        quiet = self.sandbox.quiet.total_seconds()
        longest = max(keys.values()) + self.sandbox.settle_limit
        give_up = time.monotonic() + longest.total_seconds()
        while True:
            left = sorted(k for k in keys if self.memory.written(*k) == before[k])
            if not left:
                return
            now = (await self.sandbox.read()).now()
            if all(now >= at + keys[k] for k in left) or time.monotonic() >= give_up:
                named = ", ".join(f"{c}/{k}" for c, k in left)
                raise PlaybackParted(
                    f"the playback of run {self.parent.run_id} parted in wake {wake}: the agent wrote {named} in it "
                    "when it was played, the due record that wake was for, and has not written it again within one "
                    "poll",
                    sent=f"no write of {named}",
                    held=f"a write of {named} (wake {wake})",
                )
            await asyncio.sleep(quiet)

    def _picked(self) -> dict[int, dict[tuple[str, str], timedelta]]:
        """By wake, each due record the agent wrote in a wake the run began for it, with its declaration's `poll`:
        what that wake waited for (`Orchestrator._picked_up`)."""
        wrote: dict[int, set[tuple[str, str]]] = {}
        for event in self.parent.events(until=self.at_seq, kinds=[EntityKind.MEMORY], actors=[Actor.AGENT]):
            wrote.setdefault(event.wake, set()).update(written([event]))
        found: dict[int, dict[tuple[str, str], timedelta]] = {}
        for entry in due_entries(self.parent):
            if entry.source is not DueSource.RECORDED or entry.closed is not DueClosed.FIRED:
                continue
            if entry.closed_wake is None or entry.closed_wake + 1 > self.checkpoint.wake:
                continue
            wake = entry.closed_wake + 1  # the wake it began
            for r in entry.records:
                key = (r.collection, r.key)
                if r.source < len(self.due) and key in wrote.get(wake, set()):
                    keys = found.setdefault(wake, {})
                    keys[key] = max(keys.get(key, timedelta(0)), self.due[r.source].poll)
        return found

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
