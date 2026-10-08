"""A run's transitions and the items its people engine held pending, as the read model's `transitions` and `items`
views give them (docs/querying.md)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from minutehand.adapters.providers.jira.provider import build
from minutehand.adapters.query.reader import open_model, query
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.people import People
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Scenario
from minutehand.session import RUNS, SCENARIO, WORLD
from tests.providers.jira.jira_site import SCENARIO as SITE
from tests.providers.jira.jira_site import START
from tests.support.people import people_model


def _played() -> Scenario:
    body = SITE.model_dump(mode="json")
    body["transitions_on"] = ["jira"]
    for person in body["people"]:
        if person["key"] == "tomas":
            person["takes"] = [{"provider": "jira", "take": "Start work", "after": "PT2H", "verbatim": "On it."}]
        else:
            person["reply"] = {"kind": "silent"}
    return Scenario.model_validate(body)


async def test_the_views_hold_each_move_and_each_item_held_pending(tmp_path: Path) -> None:
    played = _played()
    directory = tmp_path / RUNS / "run000000001"
    directory.mkdir(parents=True)
    (directory / SCENARIO).write_text(played.model_dump_json(), encoding="utf-8")
    clock = RunClock(START)
    store = SqliteStore(directory / WORLD, "run000000001", clock)
    jira = build()
    jira.seed(played, store)
    engine = People(played, lambda _: jira, people_model())
    booked = next(b for b in engine.look(store, clock).booked if b.person == "tomas")
    clock.jump(START + timedelta(hours=2))
    await engine.act(booked.pending, store, clock)
    store.close()

    db = open_model(tmp_path, "run000000001")
    moves = query(
        db, "SELECT provider, item_kind, name, from_state, to_state, actor, who, content, at FROM transitions"
    )
    assert [list(r) for r in moves.rows] == [
        ["jira", "ticket", "Start work", "To Do", "In Progress", "person", "tomas", json.dumps({"comment": "On it."}),
         "2026-08-24T12:50:03.000Z"]
    ]  # fmt: skip
    items = query(
        db, "SELECT person, nth, state, status, due_at, take, drawn_from, closed_at FROM items ORDER BY person"
    )
    assert [list(r) for r in items.rows] == [
        ["iris", 1, "In Progress", "pending", None, None, None, None],
        ["tomas", 1, "To Do", "acted", "2026-08-24T12:50:03.000Z", "Start work", "pinned", "2026-08-24T12:50:03.000Z"],
    ]
    joined = query(db, "SELECT t.name FROM items i JOIN transitions t ON t.seq = i.transition_seq")
    assert [list(r) for r in joined.rows] == [["Start work"]]
    events = query(db, "SELECT count(*) FROM events WHERE entity_kind = 'pending'")
    assert events.rows[0][0] == 3, "the engine's own records are in the log, as the run loop's are"
