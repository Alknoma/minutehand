"""The agent's memory, as the run holds it: what `minutehand.agent.store` writes and reads under Minutehand.

Each write is an entity version in the world's log (`EntityKind.MEMORY`, actor AGENT, the wake and simulated moment
it was made in), so the memory as of any seq is a query of the log, and a fork, which shares its parent's log up to
its checkpoint, starts from exactly the memory the parent had there. Each read is counted in its wake, and kept as an
event with no version when it finds something new (`application.memory.Reads`). A run
starts from the scenario's `memory:` (`SeededMemory`), written as actor SCENARIO before the first wake.

The receiver takes the agent's calls (`StoreCall`) as JSON on `/minutehand/agent/store`; `docs/agent-contract.md`
is the wire, for an agent not written in Python.

A put or delete may carry a `version`, a JSON integer: it lands only when no version the key already has is newer
(an equal one lands), and the answer says, write by write, whether it did. Several processes of one agent that each
mirror a record after their own commit can send two writes out of order; the version keeps the newer. A write with no
version always lands, and leaves the key's version as it was.

The agent file may name where its due records are (`DueRecords`): keys of its memory whose field holds an instant
its own scheduler acts on. The run reads them as they stand whenever it decides its next jump
(`application.due_records`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BeforeValidator, Field, JsonValue, StrictInt, model_validator

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


def _integer_version(version: object) -> object:
    if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
        raise ValueError(
            f"a write's version is a JSON integer (a row's version, or its update time in microseconds), not "
            f"{version!r}: an ISO 8601 instant or any other text is refused, since two writes in one millisecond "
            "would tie"
        )
    return version


Version = Annotated[StrictInt, BeforeValidator(_integer_version)]
"""A write's version: a JSON integer (a row's version, or its update time in microseconds), never a string or a
float. An ISO 8601 instant is refused: two writes in one millisecond would tie."""


class MemoryPut(Model):
    op: Literal["put"] = "put"
    collection: Collection = DEFAULT_COLLECTION
    key: Key
    value: JsonValue
    version: Version | None = Field(
        default=None, description="Lands only when no version the key holds is newer; None: always lands"
    )


class MemoryDelete(Model):
    op: Literal["delete"] = "delete"
    collection: Collection = DEFAULT_COLLECTION
    key: Key
    version: Version | None = Field(
        default=None, description="Lands only when no version the key holds is newer; None: always lands"
    )


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
    """The answer to a write: the seq of its last entry in the run's log (the log's head when none landed), and
    for each write in order whether it landed."""

    seq: int
    applied: list[bool]


Path = Annotated[
    str, Field(pattern=r"^[^.\s]+(\.[^.\s]+)*$", description="A field, or a dotted path into nested objects")
]


class DueRecords(Model):
    """Where the agent keeps due records in its memory: every key of `collection` under `prefix` whose value is an
    object with each field of `where` equal to the value given there, holding in `field` an ISO 8601 instant with an
    offset. Absent or null in `field` is not due, and nor is a record one of whose `later` fields holds anything but
    absent, null or an instant strictly later than the moment it would fall due. The agent's own scheduler polls its
    own records for what has come due; this is the agent's projection of them, which the run reads to know when the
    next one falls due."""

    collection: Collection = DEFAULT_COLLECTION
    prefix: str = ""
    where: dict[Path, JsonValue] = Field(default={}, description="Equality filters, by field or dotted path")
    field: Path = Field(description="The field holding when the record falls due")
    poll: timedelta = Field(
        default=timedelta(0),
        ge=timedelta(0),
        description="How long the agent's own poller may take to find a record after it falls due: the wake is that "
        "long after the record's instant, the latest moment its poller would have acted, or that long after the "
        "decision that reads it once its instant has passed, as the poller looks within it of its own work ending",
    )
    later: list[Path] = Field(
        default=[],
        description="Fields each of which must be absent, null, or an ISO 8601 instant strictly later than the moment "
        "the record would fall due (a poller that takes a record only while another of its times has not passed)",
    )

    @model_validator(mode="after")
    def _field_is_not_filtered(self) -> Self:
        if self.field in self.where:
            raise ValueError(
                f"due record field {self.field!r} is also in `where`: a record whose due time must equal one value "
                "is not a due time; name the field it falls due by in `field` and filter on another"
            )
        for named in self.later:
            if named == self.field or named in self.where:
                said = "`field`" if named == self.field else "in `where`"
                raise ValueError(
                    f"due record `later` field {named!r} is also {said}: a field the record must hold later than the "
                    "moment it falls due is another of its times, not the one it falls due by or one it is filtered on"
                )
        return self


class RecordedDue(Model):
    """One due record the run read: which declaration it was read by, where it is, and the instant it held."""

    source: int = Field(ge=0, description="Its declaration's position in the agent file's `due`")
    collection: str
    key: str
    at: AwareDatetime = Field(description="The instant its field held")


def field_at(value: JsonValue, path: str) -> tuple[bool, JsonValue]:
    """(True, the value at the dotted `path`), or (False, None) when it is not there."""
    here = value
    for part in path.split("."):
        if not isinstance(here, dict) or part not in here:
            return False, None
        here = here[part]
    return True, here


def matches(value: JsonValue, where: dict[str, JsonValue]) -> bool:
    """Whether `value` is an object with each field of `where` equal to the value given there (`store.query`)."""
    return isinstance(value, dict) and all(field_at(value, f) == (True, w) for f, w in where.items())


def later_bound(value: JsonValue, fields: Sequence[str]) -> tuple[bool, datetime | None]:
    """What `fields` of `value` say of when it may fall due: (True, the earliest instant any holds, or None when none
    holds one) while each is absent, null or an instant with an offset; (False, None) when one holds anything else,
    which is later than no moment."""
    earliest: datetime | None = None
    for path in fields:
        there, written = field_at(value, path)
        if not there or written is None:
            continue
        moment = instant(written)
        if moment is None:
            return False, None
        earliest = moment if earliest is None else min(earliest, moment)
    return True, earliest


_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def instant(found: JsonValue) -> datetime | None:
    """The instant an ISO 8601 text with an offset names; None for anything else (a date alone, a time with no
    offset, a number)."""
    if not isinstance(found, str) or not _INSTANT.match(found):
        return None
    try:
        moment = datetime.fromisoformat(found)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None
