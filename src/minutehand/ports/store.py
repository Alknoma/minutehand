"""One run's world: an append-only log, read as of the run's head.

Nothing is updated in place. A change is an event with a sequence number; an
entity's current state is its latest version at or below the head. A fork shares
its parent's log up to a sequence number and writes its own rows after it.

How bodies are kept (inline, or once per distinct content) is the store's own business: every read returns
exactly the text that was written. The agent's snapshots are kept by the same store, beside the log they belong
to, as a manifest of files each stored once.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import ProviderKey
from minutehand.domain.storage import AgentSnapshot, Freed, RunUsage
from minutehand.domain.telemetry import ForwardFailure, ReceivedSpan, Signal, SpanSource, StoredSpan
from minutehand.domain.world import (
    CallBegan,
    Change,
    EntityKind,
    EntityRef,
    Exchange,
    RecordedCall,
    Stored,
    WorldEvent,
)
from minutehand.ports.clock import Clock


class Store(Protocol):
    run_id: str

    def apply(self, change: Change) -> WorldEvent:
        """Record one change and, when it carries a body or is a delete, a new version of the entity."""
        ...

    def get(self, entity: EntityRef) -> Stored | None:
        """The entity at the head; None when it never existed or was deleted."""
        ...

    def children(
        self, provider: ProviderKey, kind: EntityKind, parent: str | None, *, after: str | None = None, limit: int = 100
    ) -> list[Stored]:
        """Live entities under one parent, ordered by external id, starting after `after`."""
        ...

    def events(self, *, since: int = 0) -> list[WorldEvent]:
        """Every event with seq greater than `since`, this run's and those it inherited."""
        ...

    def head(self) -> int:
        """The latest sequence number this run can see."""
        ...

    def attach(
        self,
        exchange: Exchange,
        *,
        first_seq: int,
        last_seq: int,
        provider: ProviderKey | None = None,
        began: CallBegan | None = None,
    ) -> None:
        """Record one HTTP call and tie it to the events it produced.

        `first_seq > last_seq` means it produced none. `provider` is None for a host nobody claimed. `began` is
        the wake and simulated time in progress when the call began, for a call recorded only after it ended
        (a burst on a tunnel); None means the clock's, now.
        """
        ...

    def calls(self) -> list[RecordedCall]:
        """Every recorded call this run can see, in order, including those that produced no event."""
        ...

    def remember(self, reply: PersonReply) -> None:
        """Keep a person's reply so a rerun plays the same one."""
        ...

    def replies(self) -> list[PersonReply]: ...

    def versions(self, entity: EntityRef) -> list[Stored]:
        """Every version of one entity this run can see, oldest first: the history `get` answers the end of.
        A delete is not a version."""
        ...

    def wake_began(self, wake: int) -> None:
        """Record the real moment a wake began, which opens its window for placing the agent's spans."""
        ...

    def wake_ended(self, wake: int) -> None:
        """Record the real moment a wake ended, after its checkpoint, which closes its window."""
        ...

    def receive(self, spans: Sequence[ReceivedSpan], *, source: SpanSource) -> list[StoredSpan]:
        """Keep one batch of the agent's spans, each stamped with the wake in progress, the simulated time and
        the head of the log as it arrives, and placed in the wake whose real-time window holds its start, or,
        when none does, the wake it arrived in."""
        ...

    def spans(self, *, trace_id: str | None = None, wake: int | None = None) -> list[StoredSpan]:
        """The spans this run can see, in the order they arrived, of one trace and placed in one wake when
        either is given. A fork sees its parent's spans placed in the wakes up to the one it was forked after."""
        ...

    def forward_failed(self, signal: Signal, endpoint: str, reason: str) -> ForwardFailure:
        """Record that a payload the agent exported could not be passed on to `endpoint`."""
        ...

    def forward_failures(self) -> list[ForwardFailure]:
        """This run's failures to pass the agent's telemetry on, in order."""
        ...

    def fork(self, run_id: str, *, at_seq: int, clock: Clock) -> Store:
        """A child run that sees this log up to `at_seq` and nothing after.

        The child stamps from `clock`, its own: a fork starts at an earlier moment than its
        parent has reached, and a clock does not run backwards.
        """
        ...

    def discard(self) -> None:
        """Remove this run and everything it wrote, and every stored body and snapshot file nothing else refers
        to: a fork refused before it ran leaves nothing behind. Refused for a run that has children, whose logs
        read through it."""
        ...

    def keep_snapshot(self, wake: int, directory: Path) -> AgentSnapshot:
        """Keep what the snapshot command wrote into `directory` as this run's snapshot at the end of `wake`: each
        file stored once, whatever other snapshot in the file already holds it. `directory` is left as it was;
        the caller removes it. Raises for anything in it that is neither a regular file nor a directory."""
        ...

    def snapshot(self, run_id: str, wake: int) -> AgentSnapshot | None:
        """One snapshot, kept or pruned; None when none was ever kept for that run and wake."""
        ...

    def snapshots(self) -> list[AgentSnapshot]:
        """Every snapshot this run can restore from, kept or pruned: its own and its ancestors' up to the wake
        it was forked after, oldest first."""
        ...

    def materialise(self, run_id: str, wake: int, into: Path) -> None:
        """Write one snapshot out as the plain directory the snapshot command filled: the same paths, bytes and
        modes. `into` must not exist. Raises `LookupError` for a snapshot never kept or pruned."""
        ...

    def pin(self, run_id: str, wake: int, *, pinned: bool) -> AgentSnapshot:
        """Pin a snapshot so pruning keeps it, or unpin it. Refused for one already pruned."""
        ...

    def prune(self, keep: int) -> list[AgentSnapshot]:
        """Let go of this run's snapshots beyond the newest `keep`, except a pinned one, the one at the run's
        start, and one a fork was taken from; their files go once nothing refers to them. Answers those pruned."""
        ...

    def sweep(self) -> Freed:
        """Remove every stored body and snapshot file no row in any run of the file refers to."""
        ...

    def usage(self) -> RunUsage:
        """What this run costs on disk."""
        ...
