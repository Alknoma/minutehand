"""The provider's surface is Notion's: every operation of Notion's published OpenAPI document, with the one operation of
2022-06-28 it no longer lists (`surface.OPERATIONS`), is served, or refused naming it with the shared not-served
refusal, and nothing else. An operation answered `invalid_request_url` would tell an agent Notion has no such
endpoint. The subset for the resources the provider claims, with their schemas, is test data
(`tests/data/notion_api/`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.notion import surface
from minutehand.adapters.providers.notion.app import UNSERVED, NotionApp
from tests.providers.notion.notion_world import World, unserved

DATA = Path(__file__).resolve().parents[2] / "data" / "notion_api"
METHODS = ("get", "post", "put", "delete", "patch")


def _shape(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _operations() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for name in ("openapi-subset-2026-10-08.json", "version-2022-06-28.json"):
        document = json.loads((DATA / name).read_text(encoding="utf-8"))
        for path, item in document["paths"].items():
            for method, operation in item.items():
                if method in METHODS:
                    found.append((method.upper(), path, operation["operationId"]))
    return found


DOCUMENTED = _operations()
OPERATIONS = list(surface.OPERATIONS)
REFUSED = {(method, _shape(path)) for method, path, _ in UNSERVED}


def _routes(world: World) -> set[tuple[str, str]]:
    app = world.provider.app(world.store, world.clock)
    assert isinstance(app, NotionApp)
    return {(m, _shape(r.path)) for r in app.routes for m in r.methods or () if m != "HEAD"}


def test_every_operation_of_the_vendors_document_is_served_or_refused_by_name(world: World) -> None:
    routes = _routes(world)
    served = [(m, p, o) for m, p, o in OPERATIONS if (m, _shape(p)) in routes and (m, _shape(p)) not in REFUSED]
    refused = [(m, p, o) for m, p, o in OPERATIONS if (m, _shape(p)) in REFUSED]
    neither = [(m, p, o) for m, p, o in OPERATIONS if (m, _shape(p)) not in routes]
    assert not neither, f"answered invalid_request_url as if Notion had no such endpoint: {neither}"
    assert len(served) + len(refused) == len(OPERATIONS) == 65
    assert (len(served), len(refused)) == (20, 45)
    claimed = {(m, _shape(p)) for m, p, _ in DOCUMENTED}
    assert (
        sum((m, _shape(p)) in claimed for m, p, _ in served),
        sum((m, _shape(p)) in claimed for m, p, _ in refused),
    ) == (20, 13)


def test_the_committed_subset_is_part_of_the_surface_the_provider_holds() -> None:
    held = {(m, _shape(p)): o for m, p, o in OPERATIONS}
    for method, path, operation in DOCUMENTED:
        assert held.get((method, _shape(path))) == operation, (method, path, operation)


def test_nothing_is_refused_by_name_that_notion_does_not_document() -> None:
    held = {(m, _shape(p)): o for m, p, o in OPERATIONS}
    for method, path, operation in UNSERVED:
        assert held.get((method, _shape(path))) == operation, (method, path, operation)


@pytest.mark.parametrize(("method", "path", "operation"), UNSERVED, ids=[f"{m} {p}" for m, p, _ in UNSERVED])
async def test_an_operation_not_served_is_refused_501_naming_it_and_writes_nothing(
    world: World, api: httpx.AsyncClient, method: str, path: str, operation: str
) -> None:
    before = world.store.head()
    concrete = re.sub(r"\{[^}]+\}", "668d797c-76fa-4934-9b05-ad288df2d136", path)
    answered = await api.request(method, concrete, json={} if method in ("POST", "PATCH") else None)
    assert unserved(answered) == f"{method} {path} ({operation})"
    assert world.store.head() == before
