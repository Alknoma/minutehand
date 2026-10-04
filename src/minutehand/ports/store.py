"""One run's world: an append-only log, read as of the run's head.

Nothing is updated in place. A change is an event with a sequence number; an
entity's current state is its latest version at or below the head. A fork shares
its parent's log up to a sequence number and writes its own rows after it.
"""

from __future__ import annotations

from typing import Protocol

from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import ProviderKey
from minutehand.domain.world import Change, EntityKind, EntityRef, Exchange, Stored, WorldEvent


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

    def attach(self, exchange: Exchange, *, first_seq: int, last_seq: int) -> None:
        """Tie one HTTP call to the events it produced. `first_seq > last_seq` means it produced none."""
        ...

    def remember(self, reply: PersonReply) -> None:
        """Keep a person's reply so a rerun plays the same one."""
        ...

    def replies(self) -> list[PersonReply]:
        ...

    def fork(self, run_id: str, *, at_seq: int) -> "Store":
        """A child run that sees this log up to `at_seq` and nothing after."""
        ...
