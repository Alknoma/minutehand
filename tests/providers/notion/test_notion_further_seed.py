"""Notion names what it seeds by what it is (the page a block is in, its kind, its place among that page's own),
never by where the log has reached: the same seed writes the same ids however long the log already is, so an open
world takes more people, documents, workspaces, webhooks and faults with nothing it held moving."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand.adapters.providers.notion import wire
from minutehand.adapters.providers.notion.edits import Editor
from minutehand.adapters.providers.notion.provider import build
from minutehand.adapters.providers.notion.seed import object_id
from minutehand.adapters.providers.notion.state import NotionWorld
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld
from minutehand.domain.scenario import Person, ProviderSeed, Scenario, Seed, SeededDocument
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
]
NESTED = {"type": "toggle", "text": "more", "children": [{"type": "paragraph", "text": "inside"}]}
ACME = {
    "key": "acme",
    "name": "Acme",
    "integrations": [{"key": "agent", "name": "Planning bot", "tokens": ["secret_acme"], "shared": ["home"]}],
    "pages": [
        {"key": "home", "title": "Home", "blocks": [{"type": "heading_1", "text": "Hi"}, NESTED]},
        {"key": "plan", "title": "Plan", "document": "Plan"},
        {"key": "child", "title": "Child", "parent": "home", "blocks": [{"type": "to_do", "text": "x"}]},
    ],
    "databases": [
        {
            "key": "tasks",
            "title": "Tasks",
            "parent": "home",
            "properties": [{"name": "Name", "type": "title"}, {"name": "Done", "type": "checkbox"}],
            "rows": [{"key": "r1", "values": {"Name": "One", "Done": True}, "blocks": [NESTED]}],
        }
    ],
}
HOOK = {"integration": "agent", "url": "http://127.0.0.1:9/hooks", "verification_token": "secret_" + "v" * 43}


def _scenario() -> Scenario:
    return Seed.model_validate(
        {
            "starts_at": START.isoformat(),
            "people": PEOPLE,
            "documents": [
                {"provider": "notion", "title": "Plan", "text": "line one\nline two"},
                {"provider": "notion", "title": "Notes", "text": "a\nb", "folder": "Team"},
                {"provider": "notion", "title": "Loose", "text": "c"},
            ],
            "provider_seeds": [{"provider": "notion", "body": json.dumps({"workspaces": [ACME], "webhooks": [HOOK]})}],
        }
    ).starting(START)


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


def _held(store: Store) -> dict[tuple[EntityKind, str], str]:
    found: dict[tuple[EntityKind, str], str] = {}
    for event in store.events():
        if event.entity.provider != "notion":
            continue
        stored = store.get(event.entity)
        if stored is None:
            found.pop((event.entity.kind, event.entity.external_id), None)
        else:
            found[(event.entity.kind, event.entity.external_id)] = stored.body
    return found


def _seeded_after(padding: int, directory: Path) -> dict[tuple[EntityKind, str], str]:
    scenario = _scenario()
    store = SqliteStore(directory / f"at-{padding}.db", f"run-{padding}", RunClock(scenario.starts_at))
    try:
        for n in range(padding):
            ref = EntityRef(provider="padding", kind=EntityKind.RECORD, external_id=str(n))
            store.apply(Change(entity=ref, operation=Operation.CREATE, actor=Actor.SCENARIO, body="{}"))
        build().seed(scenario, store)
        return _held(store)
    finally:
        store.close()


def test_pages_rows_blocks_and_documents_are_seeded_the_same_wherever_the_log_starts(tmp_path: Path) -> None:
    early, late = _seeded_after(0, tmp_path), _seeded_after(1000, tmp_path)
    assert set(early) == set(late)
    moved = sorted(i for k, i in early if early[(k, i)] != late[(k, i)])
    assert not moved, f"bodies that carry the log's position: {moved}"


def _open(directory: Path) -> StandingWorld:
    scenario = _scenario()
    clock = RunClock(scenario.starts_at)
    store = SqliteStore(directory / "world.db", "world", clock)
    world = StandingWorld(
        scenario=scenario, store=store, clock=clock, provider=lambda _: build(), inbound=[], signing={}, scripted=False
    )
    world.open(["notion"])
    return world


def _close(world: StandingWorld) -> None:
    assert isinstance(world.store, SqliteStore)
    world.store.close()


IVY = Person.model_validate({"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com", "reply": {"kind": "silent"}})
BETA = {"key": "beta", "name": "Beta", "integrations": [{"key": "helper", "name": "Helper", "tokens": ["secret_beta"]}],
        "pages": [{"key": "b-home", "title": "Beta home", "blocks": [NESTED]}]}  # fmt: skip
ADDITIONS: dict[str, dict[str, object]] = {
    "person": {"people": [IVY]},
    "document": {"documents": [SeededDocument(provider="notion", title="Second", text="x\ny")]},
    "document in a new folder": {
        "documents": [SeededDocument(provider="notion", title="Deep", text="z", folder="Ops")]
    },
    "person and their document": {
        "people": [IVY],
        "documents": [SeededDocument(provider="notion", title="Ivy's", text="hers", owner="ivy")],
    },
    "workspace": {"seeds": {"workspaces": [BETA]}},
    "webhook": {"seeds": {"webhooks": [HOOK | {"url": "http://127.0.0.1:9/second"}]}},
    "fault": {"seeds": {"faults": [{"kind": "rate_limited", "times": 2}]}},
}


@pytest.mark.parametrize("addition", list(ADDITIONS), ids=[a.replace(" ", "-") for a in ADDITIONS])
def test_an_open_notion_world_takes_each_addition_and_keeps_what_it_held(addition: str, tmp_path: Path) -> None:
    world = _open(tmp_path)
    try:
        before = _held(world.store)
        added = ADDITIONS[addition]
        seeds = added["seeds"] if "seeds" in added else None
        written = world.extend(
            people=added["people"] if "people" in added else [],  # type: ignore[arg-type]
            documents=added["documents"] if "documents" in added else [],  # type: ignore[arg-type]
            provider_seeds=[ProviderSeed(provider="notion", body=json.dumps(seeds))] if seeds is not None else [],
            directory=tmp_path,
            scratch=_scratch,
        )
        after = _held(world.store)
        assert written["notion"] > 0
        assert set(before) <= set(after)
        new = {k: after[k] for k in set(after) - set(before)}
        assert new, f"{addition} added nothing"
    finally:
        _close(world)


def test_a_person_added_to_an_open_world_is_a_member_of_its_workspace(tmp_path: Path) -> None:
    world = _open(tmp_path)
    try:
        world.extend(people=[IVY], directory=tmp_path, scratch=_scratch)
        users = NotionWorld(world.store).users(object_id("acme", "acme"))
        assert "ivy@example.com" in {u.email for u in users}
    finally:
        _close(world)


def test_a_block_the_agent_appends_comes_after_the_seeded_ones_under_an_id_of_its_own(tmp_path: Path) -> None:
    world = _open(tmp_path)
    try:
        notion = NotionWorld(world.store)
        home = object_id("acme", "home")
        seeded = list(notion.page(home).children[home])  # type: ignore[union-attr]
        editor = Editor(notion, object_id("acme", "acme"), world.clock, actor=Actor.AGENT)
        page = notion.page(home)
        assert page is not None
        added = wire.NewBlock(type=wire.BlockType.PARAGRAPH, content={"rich_text": []}, children=[])
        changed, placed = editor._place(page, home, [added], "agent")  # pyright: ignore[reportPrivateUsage]
        assert changed.children[home] == [*seeded, placed[0].id]
        assert placed[0].id not in _held(world.store) and placed[0].id not in changed.blocks.keys() - {placed[0].id}
    finally:
        _close(world)
