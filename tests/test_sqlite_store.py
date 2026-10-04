"""The store's contract: append-only, read as of the head, forks that copy nothing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.people import PersonReply
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Exchange, Operation, TicketSnapshot
from minutehand.ports.store import Store

START = datetime(2026, 8, 24, 10, 51, tzinfo=timezone.utc)
TICKET = EntityRef(provider="asana", kind=EntityKind.TICKET, external_id="T1")


def ticket(title: str, operation: Operation = Operation.UPDATE) -> Change:
    return Change(entity=TICKET, operation=operation, actor=Actor.AGENT, body=f'{{"name": "{title}"}}',
                  parent="P1", after=TicketSnapshot(title=title))


@pytest.fixture
def world(tmp_path: Path) -> tuple[SqliteStore, RunClock]:
    clock = RunClock(START)
    return SqliteStore(tmp_path / "world.db", "root", clock), clock


def test_it_satisfies_the_port(world: tuple[SqliteStore, RunClock]) -> None:
    store: Store = world[0]
    assert store.head() == 0


def test_an_entity_reads_back_its_latest_version_and_keeps_its_history(world: tuple[SqliteStore, RunClock]) -> None:
    store, clock = world
    store.apply(ticket("first", Operation.CREATE))
    clock.jump(START + timedelta(days=3))
    clock.begin_wake()
    second = store.apply(ticket("second"))
    got = store.get(TICKET)
    assert got is not None and got.body == '{"name": "second"}' and got.seq == second.seq == 2
    assert got.sim_time == START + timedelta(days=3)
    assert [e.wake for e in store.events()] == [0, 1]
    assert [e.after.title for e in store.events() if isinstance(e.after, TicketSnapshot)] == ["first", "second"]


def test_a_delete_hides_the_entity_and_a_read_changes_nothing(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    store.apply(ticket("first", Operation.CREATE))
    store.apply(Change(entity=TICKET, operation=Operation.READ, actor=Actor.AGENT))
    assert store.get(TICKET) is not None
    store.apply(Change(entity=TICKET, operation=Operation.DELETE, actor=Actor.AGENT))
    assert store.get(TICKET) is None
    assert store.children("asana", EntityKind.TICKET, "P1") == []
    assert [e.operation for e in store.events()] == [Operation.CREATE, Operation.READ, Operation.DELETE]


def test_children_list_live_entities_under_one_parent_in_pages(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    for n in ("A", "B", "C"):
        ref = EntityRef(provider="asana", kind=EntityKind.TICKET, external_id=n)
        store.apply(Change(entity=ref, operation=Operation.CREATE, actor=Actor.SCENARIO, body="{}", parent="P1"))
    other = EntityRef(provider="asana", kind=EntityKind.TICKET, external_id="Z")
    store.apply(Change(entity=other, operation=Operation.CREATE, actor=Actor.SCENARIO, body="{}", parent="P2"))
    first = store.children("asana", EntityKind.TICKET, "P1", limit=2)
    assert [s.entity.external_id for s in first] == ["A", "B"]
    rest = store.children("asana", EntityKind.TICKET, "P1", after="B", limit=2)
    assert [s.entity.external_id for s in rest] == ["C"]


def test_a_fork_sees_its_parent_up_to_the_fork_and_neither_sees_the_other_after(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    store.apply(ticket("one", Operation.CREATE))
    store.apply(ticket("two"))
    fork = store.fork("what-if", at_seq=1)
    assert fork.head() == 1
    seen = fork.get(TICKET)
    assert seen is not None and seen.body == '{"name": "one"}'
    fork.apply(ticket("forked"))
    store.apply(ticket("three"))
    in_fork, in_parent = fork.get(TICKET), store.get(TICKET)
    assert in_fork is not None and in_fork.body == '{"name": "forked"}'
    assert in_parent is not None and in_parent.body == '{"name": "three"}'
    assert [e.seq for e in fork.events()] == [1, 2]
    assert [e.seq for e in store.events()] == [1, 2, 3]


def test_a_fork_past_the_head_is_refused(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    with pytest.raises(ValueError, match="head is 0"):
        store.fork("too-far", at_seq=5)


def test_an_exchange_is_returned_with_the_event_it_produced(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    event = store.apply(ticket("one", Operation.CREATE))
    call = Exchange(method="POST", host="app.asana.com", path="/api/1.0/tasks", status=201, traceparent="00-abc-def-01")
    store.attach(call, first_seq=event.seq, last_seq=event.seq)
    assert store.events()[0].exchange == call


def test_replies_come_back_in_the_order_they_were_given(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    message = EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id="m1")
    for text in ("first", "second"):
        store.remember(PersonReply(person="sofia", in_reply_to=message, text=text, at=START))
    assert [r.text for r in store.replies()] == ["first", "second"]


def test_the_clock_refuses_to_run_backwards() -> None:
    clock = RunClock(START)
    with pytest.raises(ValueError, match="only moves forward"):
        clock.jump(START - timedelta(seconds=1))
