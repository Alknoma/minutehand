"""A declared service's moves in the read model's `transitions` view, its people's waits in `items`, and every call
its model made in `model_calls`, as `minutehand query` reads them (docs/querying.md)."""

from __future__ import annotations

from pathlib import Path

from minutehand.adapters.query.reader import open_model, query
from minutehand.session import RUNS, SCENARIO
from tests.services.test_desk import ORDER, Desk, scenario


async def test_the_views_hold_a_services_moves_by_agent_person_and_system_and_its_waits(tmp_path: Path) -> None:
    played = scenario(machine={**ORDER, "transitions": ORDER["transitions"][:-1]})
    directory = tmp_path / RUNS / "run000000001"
    directory.mkdir(parents=True)
    (directory / SCENARIO).write_text(played.model_dump_json(), encoding="utf-8")
    desk = Desk(directory, played, "run000000001")
    _, order = await desk.call("POST", "/v1/orders", {"sku": "laptop"})
    assert isinstance(order, dict)
    await desk.fire_next()
    await desk.call("POST", f"/v1/orders/{order['id']}/request_ship")
    await desk.fire_next()
    desk.store.close()

    db = open_model(tmp_path, "run000000001")
    moves = query(
        db, "SELECT provider, item_kind, name, from_state, to_state, actor, who FROM transitions ORDER BY seq"
    )
    assert [list(r) for r in moves.rows] == [
        ["approvals", "service_item", "create", None, "placed", "agent", None],
        ["approvals", "service_item", "approve", "placed", "approved", "person", "nadia"],
        ["approvals", "service_item", "request_ship", "approved", "shipping_requested", "agent", None],
        ["approvals", "service_item", "ship", "shipping_requested", "shipped", "system", "warehouse"],
    ]
    items = query(db, "SELECT person, provider, state, status FROM items")
    assert [list(r) for r in items.rows] == [["nadia", "approvals", "placed", "acted"]]
    rendered = query(db, "SELECT count(*) FROM model_calls WHERE side = 'person' AND prompt_version LIKE 'service-%'")
    [[count]] = rendered.rows
    assert isinstance(count, int) and count >= 3, "the service's renderings are model calls of the run"
