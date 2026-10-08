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
from typing import Protocol

from minutehand.domain.conversation import PersonCall
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import ProviderKey
from minutehand.domain.storage import Freed, RunUsage
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

    def apply_all(self, changes: Sequence[Change]) -> list[WorldEvent]:
        """Record every change in one transaction, in order: a reader sees all of them or none."""
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

    def record_person_call(self, call: PersonCall) -> None:
        """Keep one call made to a model for a person, answered, replayed or failed."""
        ...

    def person_calls(self) -> list[PersonCall]:
        """This run's calls for its people, in order; a fork's own, not its parent's."""
        ...

    def written(self, key: str) -> str | None:
        """The answer a model gave to exactly this ask (`PersonCall.key`) in any run of this world's file, as its
        JSON text; None when none was ever answered."""
        ...

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
        """Remove this run and everything it wrote, and every stored body nothing else refers to: a fork refused
        before it ran leaves nothing behind. Refused for a run that has children, whose logs
        read through it."""
        ...

    def sweep(self) -> Freed:
        """Remove every stored body no row in any run of the file refers to."""
        ...

    def usage(self) -> RunUsage:
        """What this run costs on disk."""
        ...
