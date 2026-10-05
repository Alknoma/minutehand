"""An open world's Drive takes every kind of addition its seed has: a person (and their My Drive), a document of each
kind, one in a folder already seeded and one in a new folder, a shared drive, a sign-in, and a fault of Drive's own
seed; and what was seeded before keeps its id, its version and its place in a listing."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand.adapters.providers.google_drive import state, wire
from minutehand.adapters.providers.google_drive.state import DriveWorld
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld, WorldRefused
from minutehand.domain.scenario import (
    Person,
    ProviderSeed,
    Seed,
    SeededDocument,
    SharedSpace,
    SignIn,
)
from minutehand.domain.world import EntityKind
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
]


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


@contextmanager
def _open(tmp_path: Path) -> Iterator[StandingWorld]:
    seed = Seed.model_validate(
        {
            "starts_at": START.isoformat(),
            "people": PEOPLE,
            "documents": [
                {"provider": "google_drive", "title": "Plan", "text": "hello", "folder": "Launch/Docs"},
                {"provider": "google_drive", "title": "Budget", "kind": "spreadsheet", "rows": [["a"]]},
            ],
            "spaces": [{"provider": "google_drive", "name": "Team", "members": [{"person": "sofia"}]}],
            "sign_ins": [{"provider": "google_drive", "credential": "refresh-owen", "person": "owen"}],
        }
    )
    scenario = seed.starting(START)
    registry = Registry.installed()
    manifests = {m.key: m for m in registry.manifests}
    clock = RunClock(scenario.starts_at)
    store = SqliteStore(tmp_path / "world.db", "world", clock)
    world = StandingWorld(
        scenario=scenario,
        store=store,
        clock=clock,
        provider=lambda k: registry.provider(manifests[k]),
        inbound=[],
        signing={},
        scripted=False,
    )
    try:
        world.open(["google_drive"])
        yield world
    finally:
        store.close()


def _files(world: StandingWorld) -> dict[str, wire.StoredFile]:
    found: dict[str, wire.StoredFile] = {}
    for event in world.store.events():
        if event.entity.provider == "google_drive" and event.entity.kind is EntityKind.DOCUMENT:
            stored = world.store.get(event.entity)
            if stored is not None:
                found[event.entity.external_id] = wire.parse(wire.StoredFile, stored.body)
    return found


def _extend(world: StandingWorld, tmp_path: Path, **added: object) -> dict[str, int]:
    return world.extend(directory=tmp_path, scratch=_scratch, **added)  # type: ignore[arg-type]


DOCUMENTS = [
    SeededDocument(provider="google_drive", title="Notes", text="# Notes\nbody"),
    SeededDocument(provider="google_drive", title="Sheet", kind="spreadsheet", rows=[["x", "y"]]),  # type: ignore[arg-type]
    SeededDocument(provider="google_drive", title="Deck", kind="presentation", text="Slide one"),  # type: ignore[arg-type]
    SeededDocument(provider="google_drive", title="notes.txt", kind="file", text="plain", mime_type="text/plain"),  # type: ignore[arg-type]
    SeededDocument(provider="google_drive", title="Agenda", text="in a seeded folder", folder="Launch/Docs"),
    SeededDocument(provider="google_drive", title="Minutes", text="in a new folder", folder="Launch/Minutes"),
    SeededDocument(provider="google_drive", title="Team plan", text="in the shared drive", space="Team"),
]


@pytest.mark.parametrize("document", DOCUMENTS, ids=[d.title for d in DOCUMENTS])
def test_a_document_of_each_kind_lands_and_moves_nothing_seeded(document: SeededDocument, tmp_path: Path) -> None:
    with _open(tmp_path) as world:
        before = _files(world)
        written = _extend(world, tmp_path, documents=[document])
        after = _files(world)
        assert written["google_drive"] > 0
        assert {k: v for k, v in after.items() if k in before} == before
        drive = DriveWorld(world.store)
        made = drive.seeded(document.title)
        assert made is not None and made not in before
        if document.folder == "Launch/Docs":
            plan = drive.file(drive.seeded("Plan") or "")
            assert plan is not None and after[made].file.parents == plan.file.parents


def test_a_person_lands_with_a_my_drive_of_their_own(tmp_path: Path) -> None:
    with _open(tmp_path) as world:
        before = _files(world)
        ivy = Person(key="ivy", name="Ivy Ng", email="ivy@example.com")
        _extend(world, tmp_path, people=[ivy])
        after = _files(world)
        assert state.root_id("ivy@example.com") in after
        assert {k: v for k, v in after.items() if k in before} == before
        assert DriveWorld(world.store).person("ivy") is not None


def test_a_shared_drive_a_sign_in_and_a_fault_land(tmp_path: Path) -> None:
    with _open(tmp_path) as world:
        before = _files(world)
        written = _extend(
            world,
            tmp_path,
            spaces=[SharedSpace(provider="google_drive", name="Ops", members=[{"person": "owen"}])],  # type: ignore[list-item]
            sign_ins=[SignIn(provider="google_drive", credential="robot@project.iam.gserviceaccount.com")],
            provider_seeds=[
                ProviderSeed(
                    provider="google_drive",
                    body=json.dumps({"faults": [{"operation": "files.list", "kind": "rate_limited"}]}),
                )
            ],
        )
        drive = DriveWorld(world.store)
        assert written["google_drive"] >= 4
        assert {d.name for d in drive.drives()} == {"Team", "Ops"}
        assert state.root_id("robot@project.iam.gserviceaccount.com") in _files(world)
        assert len(drive.faults()) == 1
        assert {k: v for k, v in _files(world).items() if k in before} == before


def test_seeded_files_list_before_what_is_made_later_and_never_share_an_id_with_it() -> None:
    seeded = [state.seeded_file_id(n, "x") for n in (0, 99, 100, 999_999)]
    minted = [state.file_id(seq) for seq in (1, 2, 99_999_999)]
    assert seeded == sorted(seeded) and max(seeded) < min(minted)
    assert not set(seeded) & set(minted)
    assert state.seeded_drive_id(3, "Ops") != state.drive_id(3) and len(state.seeded_drive_id(3, "Ops")) == 19


def test_a_document_whose_title_is_seeded_already_is_refused(tmp_path: Path) -> None:
    with _open(tmp_path) as world:
        head = world.store.head()
        with pytest.raises(WorldRefused):
            _extend(world, tmp_path, documents=[SeededDocument(provider="google_drive", title="Plan", text="other")])
        assert world.store.head() == head
