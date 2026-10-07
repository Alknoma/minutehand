"""The agent's memory, held by the run: `minutehand_agent.store`'s calls answered from the world's log, and what a
run can say about it (`domain.memory`).

A write is an entity version (`EntityKind.MEMORY`, actor AGENT) and a read an event with none, both in the wake and
at the simulated moment they were made. The memory as of a seq is the latest version of each key at or below it, so
a fork, sharing its parent's log up to its checkpoint, reads exactly the memory its parent had there; nothing is
copied or replayed. `digest` is that memory as one hash, kept in every checkpoint and compared once a fork exists.
"""

from __future__ import annotations

import hashlib
import json
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
    WakeMark,
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
    NextWakeSnapshot,
    Operation,
    WorldEvent,
)
from minutehand.ports.store import Store

PAGE = 500

NEXT_WAKE = EntityRef(provider=MEMORY_PROVIDER, kind=EntityKind.NEXT_WAKE, external_id="next_wake")
"""The one entity `minutehand_agent.wake` writes: the agent's next wake as it last said it."""


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


def recall(store: Store, call: MemoryGet) -> Found:
    """One key's value, as the run stands, recorded as the agent's read."""
    found = store.get(ref(call.collection, call.key))
    store.apply(
        Change(
            entity=ref(call.collection, call.key),
            operation=Operation.READ,
            actor=Actor.AGENT,
            after=MemorySnapshot(collection=call.collection, key=call.key),
        )
    )
    if found is None:
        return Found(found=False)
    return Found(found=True, value=_value(found.body))


def listing(store: Store, call: MemoryList) -> Listed:
    """Every key under a prefix, ordered by key, recorded as the agent's search."""
    items = [Item(key=key, value=_value(body)) for key, body in held(store, call.collection, call.prefix)]
    store.apply(
        Change(
            entity=ref(call.collection, call.prefix or "*"),
            operation=Operation.SEARCH,
            actor=Actor.AGENT,
            after=MemorySnapshot(collection=call.collection, key=call.prefix, listing=True),
        )
    )
    return Listed(items=items)


def remember(store: Store, call: MemoryWrite) -> Written:
    """Every write of one call, as the agent's, in one transaction."""
    changes: list[Change] = []
    for one in call.writes:
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
                    after=MemorySnapshot(collection=one.collection, key=one.key, value=text),
                )
            )
        else:
            changes.append(
                Change(
                    entity=entity,
                    operation=Operation.DELETE,
                    actor=Actor.AGENT,
                    after=MemorySnapshot(collection=one.collection, key=one.key),
                )
            )
    events = store.apply_all(changes)
    return Written(seq=events[-1].seq)


def mark(store: Store, said: WakeMark) -> WorldEvent:
    """The agent's next wake as it said it (`minutehand_agent.wake`), recorded as its own."""
    return store.apply(
        Change(
            entity=NEXT_WAKE,
            operation=Operation.UPDATE if store.get(NEXT_WAKE) is not None else Operation.CREATE,
            actor=Actor.AGENT,
            body=said.model_dump_json(),
            after=NextWakeSnapshot(at=said.at),
        )
    )


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
    """The digest of the memory the store sees at its head."""
    return digest(memory_of(store.events()))


def _value(body: str) -> JsonValue:
    loaded: JsonValue = json.loads(body)
    return loaded
