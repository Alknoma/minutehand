"""One run's world: an append-only log, read as of the run's head.

Nothing is updated in place. A change is an event with a sequence number; an
entity's current state is its latest version at or below the head. A fork shares
its parent's log up to a sequence number and writes its own rows after it.
"""

from __future__ import annotations

from typing import Protocol

from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import ProviderKey
from minutehand.domain.world import Change, EntityKind, EntityRef, Exchange, RecordedCall, Stored, WorldEvent
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

    def attach(self, exchange: Exchange, *, first_seq: int, last_seq: int, provider: ProviderKey | None = None) -> None:
        """Record one HTTP call and tie it to the events it produced.

        `first_seq > last_seq` means it produced none. `provider` is None for a host nobody claimed.
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

    def fork(self, run_id: str, *, at_seq: int, clock: Clock) -> Store:
        """A child run that sees this log up to `at_seq` and nothing after.

        The child stamps from `clock`, its own: a fork starts at an earlier moment than its
        parent has reached, and a clock does not run backwards.
        """
        ...

    def discard(self) -> None:
        """Remove this run and everything it wrote: a fork refused before it ran leaves nothing behind.
        Refused for a run that has children, whose logs read through it."""
        ...
