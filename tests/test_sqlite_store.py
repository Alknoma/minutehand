"""The store's contract: append-only, read as of the head, forks that copy nothing."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.store.sqlite import SCHEMA_VERSION, SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.people import PersonReply
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Exchange, Operation, TicketSnapshot
from minutehand.ports.store import Store

START = datetime(2026, 8, 24, 10, 51, tzinfo=UTC)
TICKET = EntityRef(provider="asana", kind=EntityKind.TICKET, external_id="T1")


def ticket(title: str, operation: Operation = Operation.UPDATE) -> Change:
    return Change(
        entity=TICKET,
        operation=operation,
        actor=Actor.AGENT,
        body=f'{{"name": "{title}"}}',
        parent="P1",
        after=TicketSnapshot(title=title),
    )


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


def test_a_fork_sees_its_parent_up_to_the_fork_and_neither_sees_the_other_after(
    world: tuple[SqliteStore, RunClock],
) -> None:
    store, _ = world
    store.apply(ticket("one", Operation.CREATE))
    store.apply(ticket("two"))
    fork = store.fork("what-if", at_seq=1, clock=RunClock(START))
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


def test_versions_are_the_history_a_fork_can_see_and_a_delete_is_not_one(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    store.apply(ticket("one", Operation.CREATE))
    store.apply(ticket("two"))
    store.apply(Change(entity=TICKET, operation=Operation.DELETE, actor=Actor.AGENT))
    store.apply(ticket("three", Operation.CREATE))
    fork = store.fork("what-if", at_seq=2, clock=RunClock(START))
    fork.apply(ticket("forked"))
    assert [(v.seq, v.body) for v in store.versions(TICKET)] == [
        (1, '{"name": "one"}'),
        (2, '{"name": "two"}'),
        (4, '{"name": "three"}'),
    ]
    assert [(v.seq, v.body) for v in fork.versions(TICKET)] == [
        (1, '{"name": "one"}'),
        (2, '{"name": "two"}'),
        (3, '{"name": "forked"}'),
    ]


def test_a_discarded_fork_leaves_nothing_and_its_parent_is_untouched(
    world: tuple[SqliteStore, RunClock], tmp_path: Path
) -> None:
    store, _ = world
    store.apply(ticket("one", Operation.CREATE))
    before = store.events()
    fork = store.fork("refused", at_seq=1, clock=RunClock(START))
    fork.apply(ticket("forked"))
    fork.remember(
        PersonReply(person="sofia", in_reply_to=TICKET, text="Yes.", at=START),
    )
    fork.attach(Exchange(method="GET", host="x", path="/", status=200), first_seq=2, last_seq=2)
    fork.discard()
    db = sqlite3.connect(tmp_path / "world.db")
    for table in ("run", "event", "entity_version", "exchange", "reply"):
        assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id='refused'").fetchone()[0] == 0, table
    assert store.events() == before


def test_discarding_a_run_that_has_forks_is_refused(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    store.apply(ticket("one", Operation.CREATE))
    store.fork("child", at_seq=1, clock=RunClock(START))
    with pytest.raises(ValueError, match="child"):
        store.discard()


def test_a_fork_past_the_head_is_refused(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    with pytest.raises(ValueError, match="head is 0"):
        store.fork("too-far", at_seq=5, clock=RunClock(START))


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


def test_a_refused_call_is_readable_and_is_not_pinned_on_the_next_event(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    refused = Exchange(method="GET", host="example.org", path="/", status=502)
    store.attach(refused, first_seq=store.head() + 1, last_seq=store.head())
    event = store.apply(ticket("one", Operation.CREATE))
    answered = Exchange(method="POST", host="app.asana.com", path="/api/1.0/tasks", status=201)
    store.attach(answered, first_seq=event.seq, last_seq=event.seq, provider="asana")
    assert store.events()[0].exchange == answered
    calls = store.calls()
    assert [(c.exchange.host, c.provider, c.first_seq > c.last_seq) for c in calls] == [
        ("example.org", None, True),
        ("app.asana.com", "asana", False),
    ]


def test_one_call_that_wrote_two_events_is_on_both(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    first = store.apply(ticket("one", Operation.CREATE))
    second = store.apply(ticket("two"))
    call = Exchange(method="POST", host="app.asana.com", path="/api/1.0/tasks", status=200)
    store.attach(call, first_seq=first.seq, last_seq=second.seq, provider="asana")
    assert [e.exchange for e in store.events()] == [call, call]


def test_a_fork_sees_the_calls_made_before_it_and_not_those_after(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    event = store.apply(ticket("one", Operation.CREATE))
    before = Exchange(method="POST", host="a.example", path="/before", status=200)
    store.attach(before, first_seq=event.seq, last_seq=event.seq, provider="asana")
    fork = store.fork("what-if", at_seq=1, clock=RunClock(START))
    later = store.apply(ticket("two"))
    store.attach(
        Exchange(method="POST", host="a.example", path="/after", status=200), first_seq=later.seq, last_seq=later.seq
    )
    fork.attach(Exchange(method="GET", host="a.example", path="/in-fork", status=200), first_seq=2, last_seq=1)
    assert [c.exchange.path for c in fork.calls()] == ["/before", "/in-fork"]
    assert [c.exchange.path for c in store.calls()] == ["/before", "/after"]


def test_the_store_works_from_a_thread_that_did_not_open_it(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    failures: list[BaseException] = []

    def write() -> None:
        try:
            for n in range(25):
                ref = EntityRef(provider="asana", kind=EntityKind.TICKET, external_id=f"{threading.get_ident()}-{n}")
                store.apply(Change(entity=ref, operation=Operation.CREATE, actor=Actor.AGENT, body="{}", parent="P1"))
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=write) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert failures == []
    assert [e.seq for e in store.events()] == list(range(1, 101))


def test_a_file_from_another_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    SqliteStore(path, "root", RunClock(START))
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="store schema"):
        SqliteStore(path, "root", RunClock(START))


def test_a_version_5_file_is_refused_since_its_bodies_are_inline(tmp_path: Path) -> None:
    """Version 6 keeps a long body once, under its hash, and snapshots as manifests; a version 5 file holds every
    body in its row and is not guessed at."""
    path = tmp_path / "v5.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE entity_version(run_id TEXT, seq INTEGER, body TEXT)")
        db.execute("PRAGMA user_version=5")
    with pytest.raises(RuntimeError, match="written with store schema 5; this version reads schema 7"):
        SqliteStore(path, "root", RunClock(START))


def test_an_entity_moved_to_another_parent_is_listed_only_under_the_new_one(
    world: tuple[SqliteStore, RunClock],
) -> None:
    store, _ = world
    store.apply(Change(entity=TICKET, operation=Operation.CREATE, actor=Actor.AGENT, body="{}", parent="P1"))
    store.apply(Change(entity=TICKET, operation=Operation.UPDATE, actor=Actor.AGENT, body="{}", parent="P2"))
    assert store.children("asana", EntityKind.TICKET, "P1") == []
    assert [s.parent for s in store.children("asana", EntityKind.TICKET, "P2")] == ["P2"]


def test_a_fork_stamps_from_its_own_clock_not_its_parents(world: tuple[SqliteStore, RunClock]) -> None:
    store, clock = world
    store.apply(ticket("one", Operation.CREATE))
    clock.jump(START + timedelta(days=9))
    fork = store.fork("what-if", at_seq=1, clock=RunClock(START + timedelta(days=1)))
    assert fork.apply(ticket("forked")).sim_time == START + timedelta(days=1)
    assert store.apply(ticket("parent")).sim_time == START + timedelta(days=9)


def test_a_fork_at_the_end_of_a_wake_does_not_see_the_first_call_of_the_next(
    world: tuple[SqliteStore, RunClock],
) -> None:
    """The checkpoint is the last event of wake 0; the next wake's first call writes the event right after it. Its
    first seq is the fork's seq plus one, which a call made at the checkpoint with no event of its own also has:
    only its wake tells them apart. Before, a fork taken at setup listed the next wake's first call as its own."""
    store, clock = world
    checkpoint = store.apply(ticket("checkpoint", Operation.CREATE))
    quiet = Exchange(method="GET", host="api.example", path="/a", status=200)
    store.attach(quiet, first_seq=checkpoint.seq + 1, last_seq=checkpoint.seq)  # at the checkpoint, no event
    clock.begin_wake()
    first = store.apply(ticket("written in wake 1"))
    store.attach(
        Exchange(method="POST", host="api.example", path="/b", status=200), first_seq=first.seq, last_seq=first.seq
    )

    child = store.fork("child", at_seq=checkpoint.seq, clock=clock)

    assert [c.exchange.path for c in child.calls()] == ["/a"]
