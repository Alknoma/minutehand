"""Every provider names what it seeds by what the thing is, never by where seeding reached in the log, so an open
world takes one more person, channel, ticket, document or space without its seeded things moving.

Two properties, over every installed provider that seeds anything from the shared scenario:

- **Position-free.** The same scenario seeded into an empty log and into a log already a thousand events long writes
  the same entities under the same ids with the same bodies. A provider that put the log's position into an id (a
  Slack `ts`, a Jira issue id, a Drive file id) fails here, naming the entity.
- **Additions land.** A world opened from a seed takes each kind of addition its provider seeds, and everything it
  held before is still there under the same id with the same body (aggregates, such as a channel's members, may
  grow)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.further_seed import Scratch
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld
from minutehand.domain.scenario import (
    Person,
    Scenario,
    Seed,
    SeededChannel,
    SeededDocument,
    SeededTicket,
    SharedSpace,
)
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

START = "2026-09-01T09:00:00Z"
PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
    {"key": "mila", "name": "Mila Hart", "email": "mila@example.com", "reply": {"kind": "silent"}},
]
HISTORY = [
    {"by": "sofia", "text": "first", "ago": "PT3H", "key": "p1",
     "replies": [{"by": "owen", "text": "re", "ago": "PT2H"}]},
    {"by": "owen", "text": "second", "ago": "PT1H"},
    {"by": "mila", "text": "third", "ago": "PT1H"},
]  # fmt: skip
TRACKERS = ("jira", "youtrack", "asana")
DOCUMENTS = ("google_workspace", "notion", "microsoft")
MESSAGING = ("slack", "microsoft")


def _github_seed() -> str:
    return json.dumps(
        {
            "users": [{"login": "octo", "person": "owen"}, {"login": "sofi", "person": "sofia"}],
            "tokens": [{"token": "ghp_stable1", "kind": "classic", "login": "octo"}],
            "repositories": [{"owner": "octo", "name": "notes", "files": [{"path": "README.md", "text": "hi"}]}],
        }
    )


def base(provider: str) -> dict[str, object]:
    seed: dict[str, object] = {"starts_at": START, "people": PEOPLE}
    if provider in TRACKERS:
        first: dict[str, object] = {
            "provider": provider,
            "project": "Launch",
            "title": "Book the venue",
            "assignee": "sofia",
            "comments": [{"by": "owen", "text": "please"}, {"by": "sofia", "text": "on it"}],
        }
        if provider != "asana":
            first["key"] = "venue"
        seed["tickets"] = [
            first,
            {"provider": provider, "project": "Launch", "title": "Order food", "assignee": "mila"},
        ]
    if provider in DOCUMENTS:
        seed["documents"] = [
            {"provider": provider, "title": "Plan", "text": "hello", "shared_with": [{"person": "sofia"}]},
            {"provider": provider, "title": "Budget", "kind": "spreadsheet", "rows": [["a", "b"], ["1", "2"]]},
        ]
    if provider in MESSAGING:
        seed["channels"] = [{"provider": provider, "name": "launch", "members": ["owen", "sofia", "mila"],
                             "history": HISTORY}]  # fmt: skip
    if provider == "github":
        seed["provider_seeds"] = [{"provider": "github", "body": _github_seed()}]
    return seed


IVY = {"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com", "reply": {"kind": "silent"}}


def additions(provider: str) -> dict[str, dict[str, list[dict[str, object]]]]:
    found: dict[str, dict[str, list[dict[str, object]]]] = {"person": {"people": [IVY]}}
    if provider in TRACKERS:
        found["ticket"] = {
            "tickets": [
                {
                    "provider": provider,
                    "project": "Launch",
                    "title": "Hire a band",
                    "assignee": "owen",
                    "comments": [{"by": "sofia", "text": "which one?"}],
                }
            ]
        }
        found["ticket in a new project"] = {"tickets": [{"provider": provider, "project": "Ops", "title": "Rota",
                                                         "assignee": "mila"}]}  # fmt: skip
        found["person and their ticket"] = {"people": [IVY], "tickets": [
            {"provider": provider, "project": "Launch", "title": "Ivy's task", "assignee": "ivy"}]}  # fmt: skip
    if provider in DOCUMENTS:
        found["document"] = {"documents": [{"provider": provider, "title": "Second doc", "text": "x",
                                            "shared_with": [{"person": "mila"}]}]}  # fmt: skip
        found["person and their document"] = {"people": [IVY], "documents": [
            {"provider": provider, "title": "Ivy's notes", "text": "y", "owner": "ivy"}]}  # fmt: skip
    if provider in MESSAGING:
        found["channel"] = {"channels": [{"provider": provider, "name": "ops", "members": ["owen", "mila"],
                                          "history": [{"by": "owen", "text": "hi", "ago": "PT1H"}]}]}  # fmt: skip
        found["person and their channel"] = {"people": [IVY], "channels": [
            {"provider": provider, "name": "ivys-room", "members": ["ivy", "owen"],
             "history": [{"by": "ivy", "text": "hello", "ago": "PT2H"}]}]}  # fmt: skip
    if provider == "google_workspace":
        found["space"] = {"spaces": [{"provider": provider, "name": "Team", "members": [{"person": "owen"}]}]}
    return found


PROVIDERS = ["slack", "asana", "jira", "youtrack", "google_workspace", "notion", "microsoft", "github"]
CASES = [(p, name) for p in PROVIDERS for name in additions(p)]


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


SCRATCH: Scratch = _scratch


def _scenario(provider: str) -> Scenario:
    return Seed.model_validate(base(provider)).starting(datetime.fromisoformat(START))


def _held(store: Store, provider: str) -> dict[tuple[EntityKind, str], tuple[str, str | None]]:
    found: dict[tuple[EntityKind, str], tuple[str, str | None]] = {}
    for event in store.events():
        if event.entity.provider != provider:
            continue
        stored = store.get(event.entity)
        if stored is None:
            found.pop((event.entity.kind, event.entity.external_id), None)
        else:
            found[(event.entity.kind, event.entity.external_id)] = (stored.body, stored.parent)
    return found


def _seeded_at(provider: str, padding: int, directory: Path) -> dict[tuple[EntityKind, str], tuple[str, str | None]]:
    registry = Registry.installed()
    manifest = next(m for m in registry.manifests if m.key == provider)
    scenario = _scenario(provider)
    store = SqliteStore(directory / f"seeded-{padding}.db", f"at-{padding}", RunClock(scenario.starts_at))
    try:
        for n in range(padding):
            ref = EntityRef(provider="padding", kind=EntityKind.RECORD, external_id=str(n))
            store.apply(Change(entity=ref, operation=Operation.CREATE, actor=Actor.SCENARIO, body="{}"))
        registry.provider(manifest).seed(scenario, store)
        return _held(store, provider)
    finally:
        store.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_providers_seeding_writes_the_same_ids_wherever_in_the_log_it_starts(provider: str, tmp_path: Path) -> None:
    early = _seeded_at(provider, 0, tmp_path)
    late = _seeded_at(provider, 1000, tmp_path)
    assert early, f"{provider} seeded nothing"
    moved = sorted(f"{k.value} {i}" for k, i in set(early) ^ set(late))
    assert not moved, f"{provider} put the log's position into these ids: {moved[:6]}"
    rewritten = sorted(f"{k.value} {i}" for k, i in early if early[(k, i)] != late[(k, i)])
    assert not rewritten, f"{provider} put the log's position into these bodies: {rewritten[:6]}"


def _open(provider: str, store: SqliteStore, clock: RunClock) -> StandingWorld:
    registry = Registry.installed()
    manifests = {m.key: m for m in registry.manifests}
    world = StandingWorld(
        scenario=_scenario(provider),
        store=store,
        clock=clock,
        provider=lambda k: registry.provider(manifests[k]),
        inbound=[],
        signing={},
        scripted=False,
    )
    world.open([provider])
    return world


@pytest.mark.parametrize(("provider", "addition"), CASES, ids=[f"{p}-{a.replace(' ', '-')}" for p, a in CASES])
def test_an_open_world_takes_every_kind_of_addition_and_moves_nothing_it_held(
    provider: str, addition: str, tmp_path: Path
) -> None:
    clock = RunClock(_scenario(provider).starts_at)
    store = SqliteStore(tmp_path / "world.db", "world", clock)
    world = _open(provider, store, clock)
    try:
        before = _held(world.store, provider)
        added = additions(provider)[addition]
        written = world.extend(
            people=[Person.model_validate(p) for p in added.get("people", [])],
            tickets=[SeededTicket.model_validate(t) for t in added.get("tickets", [])],
            documents=[SeededDocument.model_validate(d) for d in added.get("documents", [])],
            channels=[SeededChannel.model_validate(c) for c in added.get("channels", [])],
            spaces=[SharedSpace.model_validate(s) for s in added.get("spaces", [])],
            directory=tmp_path,
            scratch=SCRATCH,
        )
        after = _held(world.store, provider)
        lost = sorted(f"{k.value} {i}" for k, i in set(before) - set(after))
        assert not lost, f"{provider} lost what it held: {lost[:6]}"
        if provider != "github":
            assert written[provider] > 0, f"{provider} was given nothing for {addition}"
        assert world.scenario.people[-1].key == ("ivy" if "people" in added else "mila")
    finally:
        store.close()
