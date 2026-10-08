"""The provider's surface is Asana's: every operation of Asana's published OpenAPI document (`surface.OPERATIONS`,
from https://github.com/Asana/openapi at 4cf6c7c) is served, or refused naming it with the shared not-served refusal,
and nothing else. An operation answered 404 "No matching route for request" would tell an agent Asana has no such
route. The subset of the document for the resources the provider claims, with their schemas, is test data
(`tests/data/asana_rest_1_0/openapi-subset-2026-10-08.json`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from minutehand.adapters.providers.asana import surface
from minutehand.adapters.providers.asana.app import UNSERVED
from tests.providers.asana.asana_workspace import Workspace, unserved

SUBSET = Path(__file__).resolve().parents[2] / "data" / "asana_rest_1_0" / "openapi-subset-2026-10-08.json"
METHODS = ("get", "post", "put", "delete", "patch")


def _shape(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _operations() -> list[tuple[str, str, str]]:
    document = json.loads(SUBSET.read_text(encoding="utf-8"))
    return [
        (method.upper(), path, operation["operationId"])
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in METHODS
    ]


DOCUMENTED = _operations()
OPERATIONS = list(surface.OPERATIONS)
REFUSED = {(method, _shape(path)) for method, path, _ in UNSERVED}


def _routes(workspace: Workspace) -> set[tuple[str, str]]:
    app = workspace.provider.app(workspace.store, workspace.clock)
    assert isinstance(app, Starlette)
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        assert isinstance(route, Route)
        for method in route.methods or ():
            if method != "HEAD":
                found.add((method, _shape(route.path)))
    return found


def test_every_operation_of_the_vendors_document_is_served_or_refused_by_name(workspace: Workspace) -> None:
    routes = _routes(workspace)
    served = [(m, p, o) for m, p, o in OPERATIONS if (m, _shape(p)) in routes and (m, _shape(p)) not in REFUSED]
    refused = [(m, p, o) for m, p, o in OPERATIONS if (m, _shape(p)) in REFUSED]
    neither = [(m, p, o) for m, p, o in OPERATIONS if (m, _shape(p)) not in routes]
    assert not neither, f"answered 404 as if Asana had no such route: {neither}"
    assert len(served) + len(refused) == len(OPERATIONS) == 251
    assert (len(served), len(refused)) == (51, 200)
    claimed = {(m, _shape(p)) for m, p, _ in DOCUMENTED}
    assert (
        sum((m, _shape(p)) in claimed for m, p, _ in served),
        sum((m, _shape(p)) in claimed for m, p, _ in refused),
    ) == (51, 71)


def test_the_committed_subset_is_part_of_the_surface_the_provider_holds() -> None:
    held = {(m, _shape(p)): o for m, p, o in OPERATIONS}
    for method, path, operation in DOCUMENTED:
        assert held.get((method, _shape(path))) == operation, (method, path, operation)


def test_nothing_is_refused_by_name_that_the_document_does_not_hold() -> None:
    held = {(m, _shape(p)): o for m, p, o in OPERATIONS}
    for method, path, operation in UNSERVED:
        assert held.get((method, _shape(path))) == operation, (method, path, operation)


@pytest.mark.parametrize(("method", "path", "operation"), UNSERVED, ids=[o for _, _, o in UNSERVED])
async def test_an_operation_not_served_is_refused_501_naming_it_and_writes_nothing(
    workspace: Workspace, client: httpx.AsyncClient, method: str, path: str, operation: str
) -> None:
    before = workspace.store.head()
    concrete = re.sub(r"\{[^}]+\}", "1201234567890123", path)
    answered = await client.request(method, concrete, json={"data": {}} if method in ("POST", "PUT") else None)
    assert unserved(answered) == f"{method} {path} ({operation})"
    assert workspace.store.head() == before
