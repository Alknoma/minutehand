"""Reading the world's neutral view and opening a world that may be refused: what several properties share."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

import pydantic

from minutehand.domain.world import (
    DocumentSnapshot,
    EntityKind,
    GrantSnapshot,
    MessageSnapshot,
    Operation,
    TicketSnapshot,
    WorldEvent,
)
from minutehand.testing.client import Refused
from minutehand.testing.world import OpenWorld
from tests.conformance.contract import Driver
from tests.conformance.harness import Harness


def snapshots[S](events: Sequence[WorldEvent], provider: str, kind: type[S]) -> dict[str, S]:
    """Each entity's latest snapshot of type `kind` (deleted ones dropped), by entity id."""
    found: dict[str, S] = {}
    for event in events:
        if event.entity.provider != provider or event.operation in (Operation.READ, Operation.SEARCH):
            continue
        if event.operation is Operation.DELETE:
            found.pop(event.entity.external_id, None)
        elif isinstance(event.after, kind):
            found[event.entity.external_id] = event.after
    return found


def ticket_titled(events: Sequence[WorldEvent], provider: str, title: str) -> TicketSnapshot:
    found = [t for t in snapshots(events, provider, TicketSnapshot).values() if t.title == title]
    assert len(found) == 1, f"the world's neutral view holds {len(found)} tickets titled {title!r}"
    return found[0]


def document_titled(events: Sequence[WorldEvent], provider: str, title: str) -> DocumentSnapshot:
    found = [d for d in snapshots(events, provider, DocumentSnapshot).values() if d.title == title]
    assert len(found) == 1, f"the world's neutral view holds {len(found)} documents titled {title!r}"
    return found[0]


def grants(events: Sequence[WorldEvent], provider: str) -> list[GrantSnapshot]:
    return [e.after for e in events if e.entity.provider == provider and isinstance(e.after, GrantSnapshot)]


def messages(events: Sequence[WorldEvent], provider: str) -> dict[str, MessageSnapshot]:
    return snapshots(events, provider, MessageSnapshot)


def merged(base: Mapping[str, object], more: Mapping[str, object]) -> dict[str, object]:
    """`base` with each list in `more` appended to the list of the same name, and any other value set."""
    found = dict(base)
    for name, value in more.items():
        held = found[name] if name in found else None
        if isinstance(held, list) and isinstance(value, list):
            found[name] = [*held, *value]
        else:
            found[name] = value
    return found


@contextmanager
def opened_or_refused(
    harness: Harness, driver: Driver, seed: dict[str, object], *, names: Sequence[str], claiming: Sequence[str] = ()
) -> Iterator[OpenWorld | None]:
    """The world opened from `seed`, or None when creating it was refused with a message naming the provider and
    each of `names`. Any other refusal, or one naming neither, fails: a refusal must say which provider cannot hold
    which field."""
    try:
        spec = harness.spec(driver, seed)
        if claiming:
            claims = spec.claims.model_copy(update={"tokens": [*spec.claims.tokens, *claiming]})
            spec = spec.model_copy(update={"claims": claims})
    except pydantic.ValidationError as refused:
        raise AssertionError(
            f"the shared seed model refused the seed before any provider was asked, naming no provider: "
            f"{_first_line(str(refused))}"
        ) from None
    try:
        view = harness.client.create_world(spec)
    except Refused as refused:
        missing = [n for n in [driver.provider, *names] if n.casefold() not in refused.error.casefold()]
        assert not missing, (
            f"creating the world was refused ({refused.status}) without naming {missing}: {refused.error}"
        )
        yield None
        return
    world = OpenWorld(harness.client, view)
    try:
        yield world
    finally:
        harness.client.close_world(world.world_id, quiet=False)


def _first_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " / ".join(lines[:3])


KINDS_COMPARED = (EntityKind.TICKET, EntityKind.COMMENT, EntityKind.MESSAGE, EntityKind.DOCUMENT)
