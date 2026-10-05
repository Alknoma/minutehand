"""Microsoft names what it seeds by what the thing is: a drive item by its drive, folder and name, a seeded Teams
message by its second and its place among the scenario's posts of that second. So the same seed writes the same
ids wherever in the log seeding starts, and an open world takes every kind Microsoft seeds (people, guests,
documents in folders old and new, channels and group chats with history, faults, holds, a person whose chat with the
bot is not installed) without moving anything it held."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand.adapters.providers.microsoft.state import MicrosoftWorld
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld
from minutehand.domain.scenario import Person, ProviderSeed, Scenario, Seed, SeededChannel, SeededDocument
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
    {
        "key": "gus",
        "name": "Gus Guest",
        "email": "gus@partner.example",
        "account": "guest",
        "reply": {"kind": "silent"},
    },
]
BASE: dict[str, object] = {
    "starts_at": START.isoformat(),
    "people": PEOPLE,
    "documents": [
        {"provider": "microsoft", "title": "Plan", "text": "hello"},
        {"provider": "microsoft", "title": "Minutes", "text": "m", "folder": "Meetings"},
        {"provider": "microsoft", "title": "notes.txt", "text": "plain"},
    ],
    "channels": [
        {"provider": "microsoft", "name": "launch", "members": ["owen", "sofia"], "history": [
            {"by": "sofia", "text": "one", "ago": "PT1H", "key": "first", "replies": [
                {"by": "owen", "text": "one-reply", "ago": "PT30M"}]},
            {"by": "owen", "text": "two", "ago": "PT1H"},
            {"by": "sofia", "text": "three", "ago": "PT1H"}]},
        {"provider": "microsoft", "members": ["owen", "sofia"], "topic": "Pair",
         "history": [{"by": "owen", "text": "chat", "ago": "PT1H"}]},
    ],
    "provider_seeds": [{"provider": "microsoft", "body": json.dumps({
        "faults": [{"call": "GET /me", "answer": {"kind": "refused", "error": "accessDenied"}}],
        "holds": [{"document": "Plan", "by": "sofia"}],
    })}],
}  # fmt: skip
IVY = {"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com", "reply": {"kind": "silent"}}


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


def _scenario() -> Scenario:
    return Seed.model_validate(BASE).starting(START)


def _held(store: Store) -> dict[tuple[EntityKind, str], tuple[str, str | None]]:
    found: dict[tuple[EntityKind, str], tuple[str, str | None]] = {}
    for event in store.events():
        if event.entity.provider != "microsoft":
            continue
        stored = store.get(event.entity)
        if stored is None:
            found.pop((event.entity.kind, event.entity.external_id), None)
        else:
            found[(event.entity.kind, event.entity.external_id)] = (stored.body, stored.parent)
    return found


def _seeded_after(padding: int, directory: Path) -> dict[tuple[EntityKind, str], tuple[str, str | None]]:
    registry = Registry.installed()
    manifest = next(m for m in registry.manifests if m.key == "microsoft")
    store = SqliteStore(directory / f"padded-{padding}.db", f"p{padding}", RunClock(START))
    try:
        for n in range(padding):
            ref = EntityRef(provider="padding", kind=EntityKind.RECORD, external_id=str(n))
            store.apply(Change(entity=ref, operation=Operation.CREATE, actor=Actor.SCENARIO, body="{}"))
        registry.provider(manifest).seed(_scenario(), store)
        return _held(store)
    finally:
        store.close()


def test_microsoft_seeds_the_same_ids_and_bodies_wherever_in_the_log_seeding_starts(tmp_path: Path) -> None:
    early, late = _seeded_after(0, tmp_path), _seeded_after(777, tmp_path)
    assert {k for k, _ in early} >= {EntityKind.DOCUMENT, EntityKind.MESSAGE, EntityKind.CHANNEL}
    assert early == late


def _open(directory: Path) -> tuple[StandingWorld, SqliteStore]:
    registry = Registry.installed()
    manifests = {m.key: m for m in registry.manifests}
    clock = RunClock(START)
    store = SqliteStore(directory / "world.db", "world", clock)
    world = StandingWorld(
        scenario=_scenario(),
        store=store,
        clock=clock,
        provider=lambda k: registry.provider(manifests[k]),
        inbound=[],
        signing={},
        scripted=False,
    )
    world.open(["microsoft"])
    return world, store


def _fragment(body: object) -> list[ProviderSeed]:
    return [ProviderSeed(provider="microsoft", body=json.dumps(body))]


ADDITIONS: dict[str, dict[str, object]] = {
    "a member": {"people": [IVY]},
    "a guest": {"people": [IVY | {"key": "gia", "email": "gia@else.example", "account": "guest"}]},
    "a document in the library": {"documents": [{"provider": "microsoft", "title": "Budget", "text": "b"}]},
    "a document in a folder already seeded": {
        "documents": [{"provider": "microsoft", "title": "Agenda", "text": "a", "folder": "Meetings"}]
    },
    "a document in a new folder": {
        "documents": [{"provider": "microsoft", "title": "Draft", "text": "d", "folder": "Drafts"}]
    },
    "a channel with history at a second already used": {
        "channels": [
            {
                "provider": "microsoft",
                "name": "ops",
                "members": ["owen"],
                "history": [{"by": "owen", "text": "same second", "ago": "PT1H"}],
            }
        ]
    },
    "a person, their group chat and their document": {
        "people": [IVY],
        "channels": [
            {
                "provider": "microsoft",
                "members": ["ivy", "owen"],
                "history": [{"by": "ivy", "text": "hi", "ago": "PT2H"}],
            }
        ],
        "documents": [{"provider": "microsoft", "title": "Ivy", "text": "i", "owner": "ivy"}],
    },
    "a fault": {"provider_seeds": {"faults": [{"answer": {"kind": "rate_limited"}}]}},
    "a hold on a document already seeded": {"provider_seeds": {"holds": [{"document": "Minutes", "by": "owen"}]}},
    "a document and a hold on it": {
        "documents": [{"provider": "microsoft", "title": "Locked", "text": "l"}],
        "provider_seeds": {"holds": [{"document": "Locked", "by": "sofia"}]},
    },
    "a person whose chat with the bot is not installed": {
        "people": [IVY],
        "provider_seeds": {"not_installed_for": ["ivy"]},
    },
    "a seeded person's chat with the bot uninstalled": {"provider_seeds": {"not_installed_for": ["sofia"]}},
}


def _extend(world: StandingWorld, added: dict[str, object], directory: Path) -> dict[str, int]:
    def each(name: str) -> list[dict[str, object]]:
        found = added[name] if name in added else []
        assert isinstance(found, list)
        return found

    fragment = added["provider_seeds"] if "provider_seeds" in added else None
    return world.extend(
        people=[Person.model_validate(p) for p in each("people")],
        documents=[SeededDocument.model_validate(d) for d in each("documents")],
        channels=[SeededChannel.model_validate(c) for c in each("channels")],
        provider_seeds=_fragment(fragment) if fragment is not None else [],
        directory=directory,
        scratch=_scratch,
    )


@pytest.mark.parametrize("addition", list(ADDITIONS))
def test_an_open_microsoft_world_takes_each_kind_it_seeds_and_moves_nothing(addition: str, tmp_path: Path) -> None:
    world, store = _open(tmp_path)
    try:
        before = _held(world.store)
        written = _extend(world, ADDITIONS[addition], tmp_path)
        after = _held(world.store)
        assert written["microsoft"] > 0
        assert not set(before) - set(after), "an addition took away or renamed what the world held"
        moved = [k for k in before if before[k][0] != after[k][0] and k[0] in (EntityKind.DOCUMENT, EntityKind.MESSAGE)]
        assert not moved, f"seeded documents or messages rewritten: {moved}"
    finally:
        store.close()


def test_a_message_seeded_into_an_open_world_is_listed_in_its_order_before_what_was_sent_since(
    tmp_path: Path,
) -> None:
    """Teams lists a conversation's messages oldest first; a seeded post is older than anything sent in the world,
    and posts of one second keep the scenario's order."""
    world, store = _open(tmp_path)
    try:
        teams = MicrosoftWorld(world.store)
        launch = next(c for c in teams.conversations() if c.display_name == "launch")
        sent = teams.next_activity_id(world.clock)
        _extend(
            world,
            {"channels": [{"provider": "microsoft", "name": "ops", "members": ["owen"], "history": [
                {"by": "owen", "text": "later", "ago": "PT1H"}, {"by": "owen", "text": "after it", "ago": "PT1H"}]}]},
            tmp_path,
        )  # fmt: skip
        assert [m.text for m in teams.messages(launch.id)] == ["one", "two", "three", "one-reply"]
        ops = next(c for c in teams.conversations() if c.display_name == "ops")
        texts = [(m.id, m.text) for m in teams.messages(ops.id)]
        assert [t for _, t in texts] == ["later", "after it"]
        assert all(i < sent for i, _ in texts)
    finally:
        store.close()
