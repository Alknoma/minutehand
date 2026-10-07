"""How the run reaches a database of the agent's that Minutehand fronts (`domain.database`): whoever relays the
agent's connections tells the run each record as it happens, and takes the base and puts the database back when
asked."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from minutehand.domain.database import BaseTaken, Committed, Database, DatabaseRecord, Replayed, SequencesMoved


class HearsWrites(Protocol):
    """Whoever keeps what the relays see (`application.databases.Recorder`)."""

    def heard(self, record: DatabaseRecord) -> None:
        """One record, as it happens: a transaction the agent committed, before the agent is told it committed;
        sequences a transaction that did not commit drew from; a point after which the database cannot be replayed."""
        ...


class FrontsDatabase(Protocol):
    """One fronted database: its relay, and Minutehand's own connections to the real database."""

    @property
    def database(self) -> Database: ...

    async def take_base(self, suggested: str) -> BaseTaken:
        """Make the base the run starts from, named `suggested` where the base's kind lets Minutehand name it.
        No connection of the agent's may be open. Raises `RunRefused` saying why when it cannot be made."""
        ...

    async def put_back(self, base: BaseTaken, history: Sequence[Committed | SequencesMoved]) -> Replayed:
        """Make the agent's database the base again and replay `history` on it, in order, comparing what each
        statement answers with what it answered when the agent ran it. Every connection of the agent's is closed
        first. Answers what it did, and the first difference when the replay parted from the record
        (`Replayed.diverged`); raises `RunRefused` when the database could not be made again at all."""
        ...
