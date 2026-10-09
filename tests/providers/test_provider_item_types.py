"""Every provider declares, next to its fake, the kinds of item the agent's effects on it are assessed as
(`Manifest.item_types`); one that holds two kinds of item as one entity kind tells them apart itself."""

from __future__ import annotations

from pathlib import Path

from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.items import ItemKind
from minutehand.domain.world import Actor, EntityKind, EntityRef, Operation, RecordSnapshot, WorldEvent
from minutehand.ports.provider import TypesItems
from tests.checks.world import at

ITEMS = {
    "slack": {ItemKind.CHAT_MESSAGE, ItemKind.DOCUMENT},
    "microsoft": {ItemKind.CHAT_MESSAGE, ItemKind.EMAIL, ItemKind.DOCUMENT, ItemKind.CALENDAR_EVENT},
    "google_workspace": {ItemKind.EMAIL, ItemKind.CALENDAR_EVENT, ItemKind.DOCUMENT, ItemKind.COMMENT},
    "jira": {ItemKind.TICKET, ItemKind.COMMENT},
    "youtrack": {ItemKind.TICKET, ItemKind.COMMENT},
    "asana": {ItemKind.TICKET, ItemKind.COMMENT},
    "github": {ItemKind.TICKET, ItemKind.COMMENT},
    "notion": {ItemKind.DOCUMENT, ItemKind.COMMENT},
}
"""Chat messages, emails, tickets, documents and calendar events, where each service holds them. AWS and Cloud
Tasks hold the agent's own wake-ups, which are not effects on anyone."""


def test_each_provider_declares_the_kinds_of_item_it_holds() -> None:
    declared = {m.key: {t.kind for t in m.item_types} for m in Registry.installed().manifests if m.item_types}
    assert declared == ITEMS


def test_a_provider_holding_two_kinds_of_item_as_one_entity_kind_tells_them_apart_itself() -> None:
    registry = Registry.installed()
    for manifest in registry.manifests:
        entities = [t.entity for t in manifest.item_types]
        if len(entities) != len(set(entities)) or EntityKind.RECORD in entities:
            assert isinstance(registry.provider(manifest), TypesItems), manifest.key


def test_github_tells_an_issue_from_a_comment_by_the_resource_its_api_names(tmp_path: Path) -> None:
    registry = Registry.installed()
    github = registry.provider(next(m for m in registry.manifests if m.key == "github"))
    assert isinstance(github, TypesItems)

    def written(resource: str) -> WorldEvent:
        return WorldEvent(
            seq=1, run_id="r", wake=1, sim_time=at(1), wall_time=at(1), actor=Actor.AGENT, operation=Operation.CREATE,
            entity=EntityRef(provider="github", kind=EntityKind.RECORD, external_id="x"),
            after=RecordSnapshot(resource=resource, text="Fix the build"),
        )  # fmt: skip

    store = SqliteStore(tmp_path / "world.db", "r", RunClock(at(0)))
    issue, comment, label = (github.typed(written(r), store) for r in ("issues", "comments", "labels"))
    store.close()
    assert issue is not None and issue.kind is ItemKind.TICKET and issue.text == "Fix the build"
    assert comment is not None and comment.kind is ItemKind.COMMENT
    assert label is None
