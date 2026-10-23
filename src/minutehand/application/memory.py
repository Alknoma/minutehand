"""The agent's memory, held by the run: `minutehand.agent.store`'s calls answered from the world's log, and what a
run can say about it (`domain.memory`).

A write is an entity version (`EntityKind.MEMORY`, actor AGENT), in the wake and at the simulated moment it was
made. A read is counted in its wake (`Reads`, `WakeRecord.memory_reads`) and kept in the log as an event with no
version only when it is the first of its key in the wake or finds something new: an agent polling its memory would
otherwise fill the log with reads, as many as the machine's speed allowed, and move the ids a provider derives from
the log's seq. The memory as of a seq is the latest
version of each key at or below it, so a fork, sharing its parent's log up to its checkpoint, reads exactly the
memory its parent had there; nothing is copied or replayed. `digest` is that memory as one hash, kept in every
checkpoint and compared once a fork exists.

A versioned write lands only when no version the key holds is newer (`Versions`): each key's latest version, a
delete's among them, is read from the log the first time a write needs it and kept as writes land, so a fork's
receiver knows the versions its parent left at the checkpoint.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence

from pydantic import JsonValue

from minutehand.domain.memory import (
    MEMORY_PROVIDER,
    Found,
    Item,
    Listed,
    MemoryGet,
    MemoryList,
    MemoryPut,
    MemoryWrite,
    SeededMemory,
    Written,
    canonical,
    entity_id,
    split_id,
)
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    MemorySnapshot,
    Operation,
    WorldEvent,
)
from minutehand.ports.store import Store

PAGE = 500


def ref(collection: str, key: str) -> EntityRef:
    return EntityRef(provider=MEMORY_PROVIDER, kind=EntityKind.MEMORY, external_id=entity_id(collection, key))


def seed(store: Store, memory: Sequence[SeededMemory]) -> None:
    """The scenario's memory, written as actor SCENARIO before the first wake: the only thing a run's memory starts
    from."""
    store.apply_all(
        [
            Change(
                entity=ref(m.collection, m.key),
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
                body=canonical(m.value),
                parent=m.collection,
                after=MemorySnapshot(collection=m.collection, key=m.key, value=canonical(m.value)),
            )
            for m in memory
        ]
    )


def edit(store: Store, put: Sequence[SeededMemory], delete: Sequence[tuple[str, str]]) -> None:
    """A fork's change to the agent's memory (`domain.experiment.MemoryEdit`), as actor SCENARIO, in one
    transaction: the agent did not write it."""
    changes = [
        Change(
            entity=ref(m.collection, m.key),
            operation=Operation.UPDATE if store.get(ref(m.collection, m.key)) is not None else Operation.CREATE,
            actor=Actor.SCENARIO,
            body=canonical(m.value),
            parent=m.collection,
            after=MemorySnapshot(collection=m.collection, key=m.key, value=canonical(m.value)),
        )
        for m in put
    ] + [
        Change(
            entity=ref(collection, key),
            operation=Operation.DELETE,
            actor=Actor.SCENARIO,
            after=MemorySnapshot(collection=collection, key=key),
        )
        for collection, key in delete
    ]
    store.apply_all(changes)


class Reads:
    """The agent's reads of its memory in one run: every one counted in the wake it was made in, and kept in the log
    only when it is the first of its key (a get) or prefix (a listing) in that wake, or finds something other than
    the last one kept did. A read not kept found what the log already says was found, so a worker that lists its
    queue every 50 ms keeps a listing each time the queue changes, not twenty a second, and how many reads are kept
    no longer depends on how fast the machine ran."""

    def __init__(self) -> None:
        self._counted: Counter[int] = Counter()
        self._found: dict[tuple[Operation, str, str], str | None] = {}
        """What the last read kept of each key or prefix in `_wake` found: its value's text, or None."""
        self._wake = -1

    def count(self, wake: int) -> int:
        """Every get and listing of the memory made in `wake`, kept or not."""
        return self._counted[wake]

    def recall(self, store: Store, wake: int, call: MemoryGet) -> Found:
        """One key's value, as the run stands, counted as the agent's read."""
        found = store.get(ref(call.collection, call.key))
        self._read(
            store,
            wake,
            (Operation.READ, call.collection, call.key),
            found.body if found is not None else None,
            Change(
                entity=ref(call.collection, call.key),
                operation=Operation.READ,
                actor=Actor.AGENT,
                after=MemorySnapshot(collection=call.collection, key=call.key),
            ),
        )
        if found is None:
            return Found(found=False)
        return Found(found=True, value=value_of(found.body))

    def listing(self, store: Store, wake: int, call: MemoryList) -> Listed:
        """Every key under a prefix, ordered by key, counted as the agent's search."""
        found = held(store, call.collection, call.prefix)
        self._read(
            store,
            wake,
            (Operation.SEARCH, call.collection, call.prefix),
            json.dumps(found),
            Change(
                entity=ref(call.collection, call.prefix or "*"),
                operation=Operation.SEARCH,
                actor=Actor.AGENT,
                after=MemorySnapshot(collection=call.collection, key=call.prefix, listing=True),
            ),
        )
        return Listed(items=[Item(key=key, value=value_of(body)) for key, body in found])

    def _read(
        self, store: Store, wake: int, read: tuple[Operation, str, str], found: str | None, change: Change
    ) -> None:
        self._counted[wake] += 1
        if wake != self._wake:
            self._wake = wake
            self._found.clear()
        if read in self._found and self._found[read] == found:
            return
        store.apply(change)
        self._found[read] = found


class Versions:
    """Each key's latest version, as the log holds it: the version of the last versioned put or delete that landed
    there. Read from the log once, the first time a versioned write asks, then kept as writes land; a write with no
    version leaves it as it was."""

    def __init__(self) -> None:
        self._held: dict[tuple[str, str], int] | None = None

    def held(self, store: Store) -> dict[tuple[str, str], int]:
        if self._held is None:
            self._held = versions_of(store.events(kinds=[EntityKind.MEMORY]))
        return self._held

    def keep(self, held: dict[tuple[str, str], int]) -> None:
        """The versions once a call's writes have landed in the log."""
        self._held = held


def versions_of(events: Sequence[WorldEvent], *, until: int | None = None) -> dict[tuple[str, str], int]:
    """Each key's version as the events up to seq `until` (every one without it) leave it."""
    held: dict[tuple[str, str], int] = {}
    for event in events:
        if until is not None and event.seq > until:
            break
        after = event.after
        if not isinstance(after, MemorySnapshot) or after.version is None:
            continue
        if event.operation in (Operation.CREATE, Operation.UPDATE, Operation.DELETE):
            held[(after.collection, after.key)] = after.version
    return held


def remember(store: Store, call: MemoryWrite, versions: Versions) -> Written:
    """Every write of one call that lands, as the agent's, in one transaction; one whose version is older than the
    key's is left out, and the answer says which landed. The caller awaits nothing between this and the next call,
    so no other write comes between reading a key's version and writing it."""
    changes: list[Change] = []
    held = dict(versions.held(store)) if any(one.version is not None for one in call.writes) else None
    applied = landed_in(held, call) if held is not None else [True] * len(call.writes)
    for one, lands in zip(call.writes, applied, strict=True):
        if not lands:
            continue
        entity = ref(one.collection, one.key)
        if isinstance(one, MemoryPut):
            text = canonical(one.value)
            changes.append(
                Change(
                    entity=entity,
                    operation=Operation.UPDATE if store.get(entity) is not None else Operation.CREATE,
                    actor=Actor.AGENT,
                    body=text,
                    parent=one.collection,
                    after=MemorySnapshot(collection=one.collection, key=one.key, value=text, version=one.version),
                )
            )
        else:
            changes.append(
                Change(
                    entity=entity,
                    operation=Operation.DELETE,
                    actor=Actor.AGENT,
                    after=MemorySnapshot(collection=one.collection, key=one.key, version=one.version),
                )
            )
    if not changes:
        return Written(seq=store.head(), applied=applied)
    events = store.apply_all(changes)
    if held is not None:
        versions.keep(held)
    return Written(seq=events[-1].seq, applied=applied)


def landed_in(held: dict[tuple[str, str], int], call: MemoryWrite) -> list[bool]:
    """Which writes of `call` land on keys holding the versions `held` says, in order, a write landing before
    another in the same call counting for it; `held` is left as the landed writes leave it."""
    applied: list[bool] = []
    for one in call.writes:
        key = (one.collection, one.key)
        lands = one.version is None or key not in held or held[key] <= one.version
        applied.append(lands)
        if lands and one.version is not None:
            held[key] = one.version
    return applied


def held(store: Store, collection: str, prefix: str = "") -> list[tuple[str, str]]:
    """(key, value as JSON text) for every key of `collection` under `prefix` the run can see, ordered by key."""
    found: list[tuple[str, str]] = []
    start = entity_id(collection, prefix)
    after: str | None = start[:-1]
    while after is not None:
        page = store.children(MEMORY_PROVIDER, EntityKind.MEMORY, collection, after=after, limit=PAGE)
        after = page[-1].entity.external_id if len(page) == PAGE else None
        for stored in page:
            _, key = split_id(stored.entity.external_id)
            if key.startswith(prefix):
                found.append((key, stored.body))
            elif key > prefix:
                return found
    return found


def memory_of(events: Sequence[WorldEvent], *, until: int | None = None) -> dict[tuple[str, str], str]:
    """The memory as the events up to seq `until` (every one without it) leave it: (collection, key) to the value
    as JSON text."""
    memory: dict[tuple[str, str], str] = {}
    for event in events:
        if until is not None and event.seq > until:
            break
        after = event.after
        if event.entity.kind is not EntityKind.MEMORY or not isinstance(after, MemorySnapshot):
            continue
        if event.operation is Operation.DELETE:
            memory.pop((after.collection, after.key), None)
        elif event.operation in (Operation.CREATE, Operation.UPDATE) and after.value is not None:
            memory[(after.collection, after.key)] = after.value
    return memory


def digest(memory: dict[tuple[str, str], str]) -> str:
    """The memory as one SHA-256: equal memories, however they were written, digest the same."""
    canonical_text = json.dumps(sorted([c, k, v] for (c, k), v in memory.items()), separators=(",", ":"))
    return hashlib.sha256(canonical_text.encode()).hexdigest()


def store_digest(store: Store) -> str:
    """The digest of the memory the store sees at its head: the live memory entities, read in pages, never the log
    of every write (a checkpoint takes one, and a long run has thousands of writes behind a few hundred keys)."""
    memory: dict[tuple[str, str], str] = {}
    after: str | None = None
    while True:
        page = store.live(MEMORY_PROVIDER, EntityKind.MEMORY, after=after, limit=PAGE)
        for stored in page:
            memory[split_id(stored.entity.external_id)] = stored.body
        if len(page) < PAGE:
            return digest(memory)
        after = page[-1].entity.external_id


def value_of(body: str) -> JsonValue:
    """A memory value as the agent wrote it, from the JSON text the log keeps."""
    loaded: JsonValue = json.loads(body)
    return loaded
