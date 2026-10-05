"""A further seed landed on a world already open (`minutehand serve`): more people, tickets, documents, channels,
spaces, sign-ins, or a fragment of a provider's own seed, written with the same models and the same seeding code
that opened the world.

No provider is asked to seed incrementally. For each provider the world already holds, its seeding is run twice in
a scratch store, once over the scenario the world was seeded from and once over that scenario with the addition,
each from the position in the log the world seeded it at; what the second wrote that the first did not is what the
addition means for that provider, and that is what the world is given, as actor SCENARIO at the world's now. A
provider the world holds nothing of yet is seeded from the merged scenario the first time it is had, as any
provider is.

Every provider names what it seeds by what the thing is (its key, name, parent and kind), never by where seeding
reached in the log, so the same thing seeded from the grown scenario has the id it has in the world, and an addition
is what the grown scenario writes that the world's seed did not. What is refused, with nothing written, because the
addition contradicts what is there:

- the merged scenario is not a scenario (a person's key taken, a title seeded twice, an unknown person named);
- the grown scenario no longer seeds something the world's seed did (the addition gives an existing key other
  content);
- it would rewrite something that has changed since it was seeded (by the agent, a person or the test): the world
  no longer says what the seed said, so the seed has no standing to say it again;
- it would create something under an id the world already uses.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import ProviderKey, Scenario
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, Snapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import Provider
from minutehand.ports.store import Store

Scratch = Callable[[Path, Clock], AbstractContextManager[Store]]
"""An empty store at a path, closed when the context ends: where seeding is run to see what it writes."""

PADDING = "padding"
"""The provider key of the events a scratch store is padded with, so a provider seeded there starts at the same
position in the log as it did in the world."""


class AdditionRefused(ValueError):
    """The addition contradicts what the world holds; nothing was written."""


@dataclass(frozen=True)
class Written:
    """One entity as a scratch seeding left it."""

    entity: EntityRef
    body: str
    parent: str | None
    after: Snapshot | None
    order: int


def _seeded(
    provider: Provider, scenario: Scenario, at: int, directory: Path, scratch: Scratch
) -> dict[EntityRef, Written]:
    """What `provider` seeds from `scenario` into an empty store whose log already holds `at` events."""
    clock = RunClock(scenario.starts_at)
    with scratch(directory / f"scratch-{at}-{id(scenario)}.db", clock) as store:
        for n in range(at):
            store.apply(
                Change(
                    entity=EntityRef(provider=PADDING, kind=EntityKind.RECORD, external_id=str(n)),
                    operation=Operation.CREATE,
                    actor=Actor.SCENARIO,
                    body="{}",
                )
            )
        provider.seed(scenario, store)
        found: dict[EntityRef, Written] = {}
        for event in store.events(since=at):
            if event.entity.provider != provider.manifest.key:
                continue
            stored = store.get(event.entity)
            if stored is None:
                found.pop(event.entity, None)
                continue
            first = found[event.entity].order if event.entity in found else event.seq
            found[event.entity] = Written(event.entity, stored.body, stored.parent, event.after, first)
        return found


def additions(
    provider: Provider, before: Scenario, after: Scenario, world: Store, directory: Path, scratch: Scratch
) -> list[Written]:
    """What `after` seeds for `provider` that `before` did not, in the order seeding wrote it, refused when the world
    cannot take it as it stands."""
    key = provider.manifest.key
    held = [e for e in world.events() if e.entity.provider == key]
    at = held[0].seq - 1 if held else 0
    room = Path(tempfile.mkdtemp(prefix="further-seed-", dir=directory))
    try:
        was = _seeded(provider, before, at, room, scratch)
        will = _seeded(provider, after, at, room, scratch)
    finally:
        shutil.rmtree(room, ignore_errors=True)
    gone = sorted((r for r in was if r not in will), key=lambda r: was[r].order)
    if gone:
        named = ", ".join(f"{r.kind.value} {r.external_id}" for r in gone[:3])
        raise AdditionRefused(
            f"with this addition {key} would no longer seed {len(gone)} thing(s) its seed holds ({named}): the "
            "addition gives something already seeded other content"
        )
    found: list[Written] = []
    for written in sorted(will.values(), key=lambda w: w.order):
        ref = written.entity
        if ref not in was:
            if world.get(ref) is not None or world.versions(ref):
                raise AdditionRefused(
                    f"{key} would seed {ref.kind.value} {ref.external_id}, an id something made in this world since it "
                    "opened already has"
                )
            found.append(written)
            continue
        previous = was[ref]
        if (written.body, written.parent) == (previous.body, previous.parent):
            continue
        current = world.get(ref)
        if current is None or (current.body, current.parent) != (previous.body, previous.parent):
            raise AdditionRefused(
                f"the addition changes {key} {ref.kind.value} {ref.external_id}, which has changed since it was "
                "seeded; the seed no longer says what the world does"
            )
        found.append(written)
    return found


def land(
    provider_for: Callable[[ProviderKey], Provider],
    held: Sequence[ProviderKey],
    before: Scenario,
    after: Scenario,
    world: Store,
    directory: Path,
    scratch: Scratch,
) -> Mapping[ProviderKey, int]:
    """Work out every held provider's additions first, so a refusal from any leaves the world untouched; then write
    them, as actor SCENARIO. How many entities each provider was given."""
    planned = {key: additions(provider_for(key), before, after, world, directory, scratch) for key in held}
    for found in planned.values():
        for written in found:
            existing = world.get(written.entity) is not None
            world.apply(
                Change(
                    entity=written.entity,
                    operation=Operation.UPDATE if existing else Operation.CREATE,
                    actor=Actor.SCENARIO,
                    body=written.body,
                    parent=written.parent,
                    after=written.after,
                )
            )
    return {key: len(found) for key, found in planned.items()}
