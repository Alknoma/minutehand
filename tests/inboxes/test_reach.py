"""Reaching an inbox as a person: every page of a list, a list that cannot be read concluding nothing, no credential
kept anywhere, and an inbox declared by operations of an OpenAPI document (the agent's own, or Minutehand's default
shape) whose answers are held to it."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.agent.inboxes import HttpInboxReach
from minutehand.adapters.agent.openapi import OperationUnresolved
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.agent import WakeRequest
from minutehand.domain.inboxes import HttpInbox
from minutehand.domain.scenario import ScriptedDecision
from minutehand.domain.world import Actor, InboxAct, InboxItemSnapshot, ItemStatus
from tests.inboxes.product import Product, serving
from tests.inboxes.support import NADIA, T0, TOKENS, Agent, checks_named, deciding, inbox, people, play, scenario
from tests.support.stored import everything


@pytest.fixture
def product() -> Iterator[Product]:
    with serving(Product(tokens=dict(TOKENS), page=2)) as served:
        yield served


@pytest.fixture(autouse=True)
def nadias_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NADIA_TOKEN", TOKENS[NADIA])


def nadia() -> object:
    return people(deciding())[1]


async def test_every_page_of_a_list_is_read_as_the_person(tmp_path: Path, product: Product) -> None:
    for n in range(5):
        product.raise_approval(f"a{n}", NADIA, f"Approval {n}", f"op-{n}")
    store = SqliteStore(tmp_path / "w.db", "r", RunClock(T0))
    reach = HttpInboxReach(inbox(product), {"nadia": TOKENS[NADIA]})

    listed = await reach.pending(people(deciding())[1], store, RunClock(T0))

    assert listed.read and [i.item_id for i in listed.items] == ["a0", "a1", "a2", "a3", "a4"]
    assert [i.gates for i in listed.items][:2] == ["op-0", "op-1"]
    calls = store.calls()
    assert [c.exchange.path.split("cursor=")[-1] if "cursor" in c.exchange.path else "" for c in calls] == [
        "",
        "2",
        "4",
    ]
    assert all(c.exchange.inbox_call is not None and c.exchange.inbox_call.act is InboxAct.LIST for c in calls)
    assert all(not c.refused and c.provider is None for c in calls)
    assert {s.authorization for s in product.sent} == {f"Bearer {TOKENS[NADIA]}"}


async def test_a_list_the_product_refuses_concludes_nothing_and_withdraws_nothing(
    tmp_path: Path, product: Product
) -> None:
    def made(store: SqliteStore) -> Agent:
        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            if n == 1:
                product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
                return request.now + timedelta(hours=1), False
            product.tokens[NADIA] = "a-token-nobody-holds"  # every list as Nadia is now refused 401
            return request.now + timedelta(hours=1), n >= 3

        return Agent(plan=plan)

    played = await play(tmp_path, scenario(deciding(hours=20)), inbox(product), made)

    statuses = [e.after.status for e in played.store.events() if isinstance(e.after, InboxItemSnapshot)]
    assert statuses == [ItemStatus.PENDING]


async def test_no_credential_reaches_the_stored_bytes(tmp_path: Path, product: Product) -> None:
    def made(store: SqliteStore) -> Agent:
        def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
            product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
            return None, n > 1

        return Agent(plan=plan)

    approve = ScriptedDecision(decision="approve")
    played = await play(tmp_path, scenario(deciding(approve, hours=1)), inbox(product), made)
    played.store.close()

    assert any(s.authorization == f"Bearer {TOKENS[NADIA]}" for s in product.sent)
    assert TOKENS[NADIA].encode() not in everything(tmp_path)
    assert b"approvals?approver=nadia@example.com" in everything(tmp_path)


AGENTS_OWN = {
    "openapi": "3.1.0",
    "info": {"title": "approvals", "version": "1"},
    "paths": {
        "/approvals": {
            "get": {
                "operationId": "listApprovals",
                "parameters": [
                    {"name": "approver", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "description": "a page",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Page"}}},
                    }
                },
            }
        },
        "/approvals/{id}/decision": {
            "post": {
                "operationId": "decideApproval",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"200": {"description": "taken"}},
            }
        },
    },
    "components": {
        "schemas": {
            "Page": {
                "type": "object",
                "required": ["items"],
                "properties": {
                    "items": {"type": "array", "items": {"$ref": "#/components/schemas/Approval"}},
                    "next": {"type": ["string", "null"]},
                },
            },
            "Approval": {
                "type": "object",
                "required": ["id", "summary"],
                "properties": {
                    "id": {"type": "string"},
                    "summary": {"type": "string"},
                    "operation": {"type": "string"},
                },
            },
        }
    },
}


def by_operations(product: Product, document: Path) -> HttpInbox:
    return HttpInbox.model_validate(
        {
            "name": "approvals",
            "as_person": {"headers": {"Authorization": "Bearer {person.credential}"}},
            "pending": {
                "request": {
                    "kind": "operation",
                    "document": str(document),
                    "operation": "listApprovals",
                    "server": product.base,
                    "parameters": {"approver": "{person.email}"},
                },
                "items": "$.items[*]",
                "id": "$.id",
                "summary": "$.summary",
                "gates": "$.operation",
                "paging": {"next": "$.next", "param": "cursor"},
            },
            "decisions": [
                {
                    "name": "approve",
                    "permits": True,
                    "request": {
                        "kind": "operation",
                        "document": str(document),
                        "operation": "decideApproval",
                        "server": product.base,
                        "parameters": {"id": "{item.id}"},
                        "body": {"decision": "approve"},
                    },
                }
            ],
        }
    )


def gated(product: Product) -> Agent:
    def plan(request: WakeRequest, n: int) -> tuple[datetime | None, bool]:
        if n == 1:
            product.raise_approval("a1", NADIA, "Send Owen the booking", "tell-1")
            return request.now + timedelta(days=1), False
        return None, True

    return Agent(plan=plan)


async def test_an_inbox_declared_by_the_agents_own_operations_is_read_and_decided_through_them(
    tmp_path: Path, product: Product
) -> None:
    document = tmp_path / "openapi.json"
    document.write_text(json.dumps(AGENTS_OWN))
    approve = ScriptedDecision(decision="approve")
    played = await play(tmp_path, scenario(deciding(approve)), by_operations(product, document), gated(product))

    assert product.state("a1") == "approved"
    assert checks_named(played.result, "agent_contract_changed") == []
    asked = [e.after for e in played.store.events() if isinstance(e.after, InboxItemSnapshot)]
    assert asked[0].gates == "tell-1"


async def test_an_answer_outside_the_agents_own_description_is_its_contract_changed_naming_the_field(
    tmp_path: Path, product: Product
) -> None:
    document = tmp_path / "openapi.json"
    document.write_text(json.dumps(AGENTS_OWN))
    product.numbered = True
    approve = ScriptedDecision(decision="approve")
    played = await play(tmp_path, scenario(deciding(approve)), by_operations(product, document), gated(product))

    said = checks_named(played.result, "agent_contract_changed")
    assert len(said) == 1
    assert said[0].startswith("the agent's contract changed: listApprovals (")
    assert "$.items[0].summary: 21 is not of type 'string'" in said[0]
    assert [e for e in played.store.events() if isinstance(e.after, InboxItemSnapshot)] == []


def test_an_operation_its_document_does_not_have_or_fill_is_refused_naming_it(tmp_path: Path) -> None:
    document = tmp_path / "openapi.json"
    document.write_text(json.dumps(AGENTS_OWN))
    product = Product(tokens={}, base="http://127.0.0.1:9")
    declared = by_operations(product, document).model_dump()
    declared["pending"]["request"]["operation"] = "listEverything"
    with pytest.raises(OperationUnresolved, match="has no operation 'listEverything'"):
        HttpInboxReach(HttpInbox.model_validate(declared), {})
    declared = by_operations(product, document).model_dump()
    declared["pending"]["request"]["parameters"] = {"approver": "{person.email}", "colour": "red"}
    with pytest.raises(OperationUnresolved, match="has no parameter colour; it has approver, cursor"):
        HttpInboxReach(HttpInbox.model_validate(declared), {})
    declared = by_operations(product, document).model_dump()
    declared["pending"]["request"]["parameters"] = {}
    with pytest.raises(OperationUnresolved, match="requires approver, not given"):
        HttpInboxReach(HttpInbox.model_validate(declared), {})


async def test_an_agent_that_implements_minutehands_default_shape_declares_no_request_of_its_own(
    tmp_path: Path, product: Product
) -> None:
    def operation(name: str) -> dict[str, object]:
        return {"kind": "operation", "document": "minutehand", "operation": name, "server": product.base}

    declared = HttpInbox.model_validate(
        {
            "name": "approvals",
            "as_person": {"headers": {"Authorization": "Bearer {person.credential}"}},
            "pending": {
                "request": operation("listPending"),
                "items": "$.items[*]",
                "id": "$.id",
                "summary": "$.summary",
                "gates": "$.gates",
                "decisions": "$.decisions",
                "paging": {"next": "$.next", "param": "cursor"},
            },
            "decisions": [
                {"name": "approve", "permits": True, "request": operation("decide")},
                {
                    "name": "reject",
                    "permits": False,
                    "request": operation("decide"),
                    "inputs": [{"name": "reason", "description": "Why"}],
                },
            ],
        }
    )
    reject = ScriptedDecision(decision="reject", inputs={"reason": "Not this week"})
    played = await play(tmp_path, scenario(deciding(reject)), declared, gated(product))

    assert product.state("a1") == "rejected" and product.approvals["a1"].reason == "Not this week"
    posted = [json.loads(s.body) for s in product.sent if s.method == "POST"]
    assert posted == [
        {
            "item": "a1",
            "decision": "reject",
            "inputs": {"reason": "Not this week"},
            "person": {"key": "nadia", "email": NADIA, "name": "Nadia Ek"},
            "decided_at": (T0 + timedelta(hours=2)).isoformat(),
        }
    ]
    assert checks_named(played.result, "agent_contract_changed") == []
    decided = [e for e in played.store.events() if e.actor is Actor.PERSON]
    assert len(decided) == 1
