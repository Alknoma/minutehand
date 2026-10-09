"""The agent's effects read as items of their kinds (`domain.items`): which events are chat messages, emails, tickets,
documents, calendar events, items of a declared service or records of a declared store, read by the provider that
holds each. What the item checks and the shared reviewer read (`checks/items.py`, `checks/judged/review.py`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta

from minutehand.domain.agent import AgentUnderTest, Polled
from minutehand.domain.items import BUILT_IN, ItemKind, ProvidedTypes, TypedItem, read_as
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import ProviderKey, Scenario
from minutehand.domain.transitions import move_of
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    Operation,
    RecordedCall,
    ServiceEventSnapshot,
    StoredSnapshot,
    TransitionSnapshot,
    WorldEvent,
)
from minutehand.ports.provider import TypesItems
from minutehand.ports.store import Store

WRITES = frozenset({Operation.CREATE, Operation.UPDATE, Operation.DELETE})


def typed_items(
    events: Sequence[WorldEvent],
    scenario: Scenario,
    manifests: Mapping[ProviderKey, Manifest],
    typers: Mapping[ProviderKey, TypesItems],
    world: Store | None,
    calls: Sequence[RecordedCall] = (),
) -> list[TypedItem]:
    """Every write in `events`, by anyone, that is an item of a kind its holder declares, read as that item: a
    declared service's moves and writes as its items, a declared store's writes as its records, and everything else
    by its provider: one that tells its own items apart (`typers`, with the world to read its bodies from), else the
    one item type its manifest declares for the entity's kind. Each of the agent's writes carries the seq of its last
    read, write or call naming the same item before it (`TypedItem.last_read`): the state it acted on."""
    services = {s.key for s in scenario.services}
    touched: dict[EntityRef, list[int]] = {}
    for event in events:
        if event.actor is Actor.AGENT:
            touched.setdefault(event.entity, []).append(event.seq)
    named = [(c.exchange.path, c.last_seq) for c in calls if c.exchange.inbox_call is None]
    found: list[TypedItem] = []
    states: dict[EntityRef, str] = {}
    for event in events:
        if event.operation not in WRITES or (event.after is None and event.operation is not Operation.DELETE):
            continue  # a read, or a write the log keeps no snapshot of (a copy in another mailbox)
        moved = move_of(event, states.get(event.entity))
        if moved is not None:
            states[moved.item] = moved.to_state
        after = event.after
        key = event.entity.provider
        typed: TypedItem | None = None
        if isinstance(after, TransitionSnapshot):
            if after.item.provider in services:
                typed = read_as(event, ItemKind.SERVICE_ITEM)
        elif isinstance(after, ServiceEventSnapshot):
            typed = read_as(event, ItemKind.SERVICE_ITEM)
        elif isinstance(after, StoredSnapshot) or event.entity.kind is EntityKind.STORED:
            typed = read_as(event, ItemKind.STORED_RECORD)
        elif key in typers:
            typed = typers[key].typed(event, world) if world is not None else None
        elif key in manifests:
            kinds = [t for t in manifests[key].item_types if t.entity is event.entity.kind]
            if len(kinds) == 1:
                typed = read_as(event, kinds[0].kind)
        if typed is not None:
            if moved is not None:
                typed = typed.model_copy(update={"move": moved})  # the move from the state the item's last write left
            if typed.actor is Actor.AGENT:
                seen = [s for s in touched.get(typed.item, []) if s < event.seq]
                seen += [head for path, head in named if typed.item.external_id in path and head < event.seq]
                typed = typed.model_copy(update={"last_read": max(seen, default=None)})
            found.append(typed)
    return found


def provided_types(manifests: Sequence[Manifest]) -> list[ProvidedTypes]:
    """The item types each provider declares, and the built-in ones of declared services and stores."""
    return [ProvidedTypes(provider=m.key, types=m.item_types) for m in manifests if m.item_types] + [
        ProvidedTypes(provider="", types=BUILT_IN)
    ]


def rhythm(agent: AgentUnderTest) -> timedelta | None:
    """The agent's declared rhythm: its `tick`, else its shortest polled `every`; None when it declares neither."""
    if agent.tick is not None:
        return agent.tick
    every = [w.every for w in agent.wakes if isinstance(w, Polled)]
    return min(every) if every else None
