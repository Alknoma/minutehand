"""The agent's memory, as the run holds it: what `minutehand_agent.store` writes and reads under Minutehand.

Each write is an entity version in the world's log (`EntityKind.MEMORY`, actor AGENT, the wake and simulated moment
it was made in), so the memory as of any seq is a query of the log, and a fork, which shares its parent's log up to
its checkpoint, starts from exactly the memory the parent had there. Each read is an event with no version. A run
starts from the scenario's `memory:` (`SeededMemory`), written as actor SCENARIO before the first wake.

The receiver takes the agent's calls (`StoreCall`, `WakeMark`) as JSON on `/minutehand/agent/store` and
`/minutehand/agent/wake`; `docs/agent-contract.md` is the wire, for an agent not written in Python.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue

from minutehand.domain.model import Model

MEMORY_PROVIDER = "memory"
"""The provider key every memory entity is recorded under: the agent's own, not a service's."""

DEFAULT_COLLECTION = "default"

Collection = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
Key = Annotated[str, Field(min_length=1, max_length=1024)]


def entity_id(collection: str, key: str) -> str:
    """The entity's external id: the collection, a slash, the key (a collection holds no slash)."""
    return f"{collection}/{key}"


def split_id(external_id: str) -> tuple[str, str]:
    collection, _, key = external_id.partition("/")
    return collection, key


def canonical(value: JsonValue) -> str:
    """One spelling of a JSON value: keys sorted, no spaces, so equal values are equal text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class SeededMemory(Model):
    """One key the agent's memory holds when a run starts (`Scenario.memory`)."""

    collection: Collection = DEFAULT_COLLECTION
    key: Key
    value: JsonValue


class MemoryGet(Model):
    op: Literal["get"] = "get"
    collection: Collection = DEFAULT_COLLECTION
    key: Key


class MemoryList(Model):
    op: Literal["list"] = "list"
    collection: Collection = DEFAULT_COLLECTION
    prefix: str = ""


class MemoryPut(Model):
    op: Literal["put"] = "put"
    collection: Collection = DEFAULT_COLLECTION
    key: Key
    value: JsonValue


class MemoryDelete(Model):
    op: Literal["delete"] = "delete"
    collection: Collection = DEFAULT_COLLECTION
    key: Key


MemoryWriteOne = Annotated[MemoryPut | MemoryDelete, Field(discriminator="op")]


class MemoryWrite(Model):
    """Every write, applied together: one transaction in the run's log."""

    op: Literal["write"] = "write"
    writes: list[MemoryWriteOne] = Field(min_length=1)


StoreCall = Annotated[MemoryGet | MemoryList | MemoryWrite, Field(discriminator="op")]


class Found(Model):
    """The answer to a get."""

    found: bool
    value: JsonValue = None


class Item(Model):
    key: str
    value: JsonValue


class Listed(Model):
    """The answer to a list: every key under the prefix, ordered by key."""

    items: list[Item]


class Written(Model):
    """The answer to a write: the seq of its last entry in the run's log."""

    seq: int


class WakeMark(Model):
    """`minutehand_agent.wake`: the moment the agent asks to be woken next, or none."""

    at: AwareDatetime | None
