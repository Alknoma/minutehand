"""Bodies kept once: every reader gets back exactly the text written, the same bytes are one stored row wherever
they were written, forks share them, and a discarded run takes only the bodies nothing else refers to, even when
the process dies halfway through."""

from __future__ import annotations

import hashlib
import itertools
import sqlite3
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.store.sqlite import INLINE_LIMIT, PARTS_FROM, SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.telemetry import ArrayValue, Attribute, MapValue, ReceivedSpan, SpanSource, StringValue
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Exchange, Operation, RecordSnapshot

START = datetime(2026, 8, 24, 10, 51, tzinfo=UTC)
REAL = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)

AWKWARD = {
    "empty": "",
    "odd json spacing": '{ "a" :1 ,\n\t"b":[ ] ,"c" : "x"   }\r\n',
    "bytes that are not UTF-8, as the proxy decodes them (latin-1)": bytes(range(256)).decode("latin-1") * 3,
    "one byte under the limit": "u" * (INLINE_LIMIT - 1),
    "exactly at the limit": "x" * INLINE_LIMIT,
    "at the limit in two-byte characters": "é" * (INLINE_LIMIT // 2),
    "one byte over the limit": "o" * (INLINE_LIMIT + 1),
    "a NUL and a lone carriage return": "a\x00b\rc" * 200,
    "two megabytes of JSON": '{"rows": [' + ",".join(f'{{"n": {n}, "v": "{n:08x}"}}' for n in range(70_000)) + "]}",
    "a conversation kept as its parts": '{"model":"m","messages":['
    + ",".join(f'{{"role":"user","content":"turn {n} {"x" * 300}"}}' for n in range(40))
    + '],"tools":[{"type":"function","name":"t"}]}',
    "where a body is cut, inside a string and at its very start": '[{"a":"[{\\"b\\":1},{\\"c\\":2}]"},'
    + ",".join('{"' + "k" * 200 + f'":{n}}}' for n in range(40))
    + "]",
}


def entity(name: str) -> EntityRef:
    return EntityRef(provider="ledger", kind=EntityKind.RECORD, external_id=name)


def span_with(value: str, span_id: str) -> ReceivedSpan:
    return ReceivedSpan(
        trace_id="c" * 32,
        span_id=span_id,
        name="chat",
        start=REAL,
        end=REAL + timedelta(seconds=1),
        attributes=[
            Attribute(key="gen_ai.input.messages", value=StringValue(value=value)),
            Attribute(key="short", value=StringValue(value="kept in the row")),
            Attribute(
                key="nested",
                value=MapValue(values=[Attribute(key="list", value=ArrayValue(values=[StringValue(value=value)]))]),
            ),
        ],
    )


def content_rows(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM content").fetchone()[0]


@pytest.fixture
def world(tmp_path: Path) -> tuple[SqliteStore, RunClock]:
    clock = RunClock(START)
    return SqliteStore(tmp_path / "world.db", "root", clock), clock


def write_everywhere(store: SqliteStore, name: str, body: str, n: int) -> None:
    """`body` as an entity's version, a snapshot's text, both sides of a call and two span attribute values."""
    event = store.apply(
        Change(
            entity=entity(name),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body=body,
            parent="P",
            after=RecordSnapshot(resource="entries", text=body),
        )
    )
    store.attach(
        Exchange(method="POST", host="ledger.example", path="/e", status=201, request_body=body, response_body=body),
        first_seq=event.seq,
        last_seq=event.seq,
        provider="ledger",
    )
    store.receive([span_with(body, f"{n:016x}")], source=SpanSource.RECEIVED)


@pytest.mark.parametrize("label", sorted(AWKWARD))
def test_an_awkward_body_reads_back_identical_from_every_reader(label: str, tmp_path: Path) -> None:
    body = AWKWARD[label]
    clock = RunClock(START)
    write_everywhere(SqliteStore(tmp_path / "world.db", "root", clock), "e", body, 1)
    store = SqliteStore(tmp_path / "world.db", "root", clock)  # a fresh connection: nothing is read from memory

    got = store.get(entity("e"))
    assert got is not None and got.body == body
    assert [s.body for s in store.versions(entity("e"))] == [body]
    assert [s.body for s in store.children("ledger", EntityKind.RECORD, "P")] == [body]
    [event] = store.events()
    assert isinstance(event.after, RecordSnapshot) and event.after.text == body
    assert event.exchange is not None and event.exchange.request_body == event.exchange.response_body == body
    [call] = store.calls()
    assert call.exchange.request_body == body and call.exchange.response_body == body
    [kept] = store.spans()
    assert kept.span == span_with(body, f"{1:016x}")


def test_none_and_empty_bodies_stay_distinct(world: tuple[SqliteStore, RunClock]) -> None:
    store, _ = world
    store.attach(
        Exchange(method="GET", host="h", path="/", status=204, request_body=None, response_body=""),
        first_seq=1,
        last_seq=0,
    )
    [call] = store.calls()
    assert call.exchange.request_body is None and call.exchange.response_body == ""


def test_a_body_at_the_limit_is_stored_once_and_one_under_it_stays_in_its_row(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "world.db", "root", RunClock(START))
    store.apply(
        Change(entity=entity("under"), operation=Operation.CREATE, actor=Actor.AGENT, body="u" * (INLINE_LIMIT - 1))
    )
    assert content_rows(tmp_path / "world.db") == 0
    store.apply(Change(entity=entity("at"), operation=Operation.CREATE, actor=Actor.AGENT, body="x" * INLINE_LIMIT))
    assert content_rows(tmp_path / "world.db") == 1


def test_one_stored_copy_for_the_same_bytes_across_events_versions_calls_spans_and_runs(
    world: tuple[SqliteStore, RunClock], tmp_path: Path
) -> None:
    store, _ = world
    body = '{"listing": [' + ",".join(f'"member-{n}"' for n in range(500)) + "]}"
    for n in range(10):
        write_everywhere(store, f"e{n}", body, n)
    fork = store.fork("other", at_seq=store.head(), clock=RunClock(START))
    for n in range(10, 15):
        write_everywhere(fork, f"e{n}", body, n)

    with sqlite3.connect(tmp_path / "world.db") as db:
        hashes = db.execute("SELECT hash FROM content").fetchall()
        refs = db.execute("SELECT COUNT(DISTINCT body_ref), COUNT(body_ref) FROM entity_version").fetchone()
        calls = db.execute("SELECT COUNT(DISTINCT request_ref), COUNT(response_ref) FROM exchange").fetchone()
        values = db.execute("SELECT COUNT(DISTINCT ref), COUNT(ref) FROM span_body").fetchone()
    # Two stored rows: the body itself, and the one snapshot JSON that wraps it (the same text in each event).
    assert len(hashes) == 2
    assert refs == (1, 15) and calls == (1, 15) and values == (1, 30)
    assert all(s.body == body for s in fork.versions(entity("e3")) + fork.versions(entity("e12")))


def test_a_fork_shares_its_parents_bodies_and_stores_only_what_is_new(
    world: tuple[SqliteStore, RunClock], tmp_path: Path
) -> None:
    store, _ = world
    shared = "s" * 4000
    store.apply(Change(entity=entity("a"), operation=Operation.CREATE, actor=Actor.AGENT, body=shared))
    fork = store.fork("child", at_seq=1, clock=RunClock(START))
    assert content_rows(tmp_path / "world.db") == 1
    fork.apply(Change(entity=entity("b"), operation=Operation.CREATE, actor=Actor.AGENT, body=shared))
    assert content_rows(tmp_path / "world.db") == 1
    fork.apply(Change(entity=entity("c"), operation=Operation.CREATE, actor=Actor.AGENT, body="n" * 4000))
    assert content_rows(tmp_path / "world.db") == 2
    got = fork.get(entity("a"))
    assert got is not None and got.body == shared


def test_discarding_a_fork_removes_only_the_bodies_nothing_else_refers_to(
    world: tuple[SqliteStore, RunClock], tmp_path: Path
) -> None:
    """The parent refers to each of its bodies from one place only, one of each kind, and the fork refers to one of
    them too: after the fork is discarded the parent still reads every one, and only the fork's own is gone."""
    store, _ = world
    version, snapshot, request, response, value = ("v" * 4000, "s" * 4000, "q" * 4000, "r" * 4000, "a" * 4000)
    store.apply(Change(entity=entity("a"), operation=Operation.CREATE, actor=Actor.AGENT, body=version))
    store.apply(
        Change(
            entity=entity("read"),
            operation=Operation.READ,
            actor=Actor.AGENT,
            after=RecordSnapshot(resource="entries", text=snapshot),
        )
    )
    store.attach(
        Exchange(method="POST", host="h", path="/", status=200, request_body=request, response_body=response),
        first_seq=1,
        last_seq=0,
    )
    store.receive([span_with(value, "2" * 16)], source=SpanSource.RECEIVED)
    before = (store.events(), store.calls(), store.spans())
    held = content_rows(tmp_path / "world.db")

    fork = store.fork("refused", at_seq=store.head(), clock=RunClock(START))
    own = "o" * 4000
    fork.apply(Change(entity=entity("b"), operation=Operation.CREATE, actor=Actor.AGENT, body=version))
    fork.attach(Exchange(method="POST", host="h", path="/", status=200, request_body=own), first_seq=3, last_seq=3)
    fork.receive([span_with(own, "1" * 16)], source=SpanSource.RECEIVED)
    assert content_rows(tmp_path / "world.db") == held + 1
    fork.discard()

    assert content_rows(tmp_path / "world.db") == held
    reopened = SqliteStore(tmp_path / "world.db", "root", RunClock(START))
    got = reopened.get(entity("a"))
    assert got is not None and got.body == version
    assert (reopened.events(), reopened.calls(), reopened.spans()) == before


def test_a_process_killed_while_discarding_deletes_no_body_a_row_still_refers_to(tmp_path: Path) -> None:
    """The process dies at the moment the bodies are swept, after the run's rows were deleted in the same
    transaction: nothing was committed, every row still reads its body, and discarding again completes."""
    path = tmp_path / "world.db"
    store = SqliteStore(path, "root", RunClock(START))
    store.apply(Change(entity=entity("a"), operation=Operation.CREATE, actor=Actor.AGENT, body="p" * 4000))
    fork = store.fork("refused", at_seq=1, clock=RunClock(START))
    fork.apply(Change(entity=entity("b"), operation=Operation.CREATE, actor=Actor.AGENT, body="f" * 4000))
    fork.close()
    store.close()
    program = textwrap.dedent(
        f"""
        import os
        from datetime import UTC, datetime
        from pathlib import Path
        from minutehand.adapters.store.sqlite import SqliteStore
        from minutehand.application.run_clock import RunClock

        fork = SqliteStore(Path({str(path)!r}), "refused", RunClock(datetime(2026, 8, 24, tzinfo=UTC)))

        def die_at_the_sweep(statement: str) -> None:
            if statement.startswith("DELETE FROM content"):
                os._exit(9)

        fork._db.set_trace_callback(die_at_the_sweep)
        fork.discard()
        """
    )
    died = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=30)
    assert died.returncode == 9, died.stderr

    after = SqliteStore(path, "refused", RunClock(START))
    got = after.get(entity("b"))
    assert got is not None and got.body == "f" * 4000
    after.discard()
    assert content_rows(path) == 1
    root = SqliteStore(path, "root", RunClock(START)).get(entity("a"))
    assert root is not None and root.body == "p" * 4000


def test_the_log_is_cut_to_nothing_when_the_store_closes(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "world.db", "root", RunClock(START))
    for n in range(20):
        store.apply(
            Change(entity=entity(f"e{n}"), operation=Operation.CREATE, actor=Actor.AGENT, body=f"{n}" * 300_000)
        )
    reader = sqlite3.connect(tmp_path / "world.db")  # another connection still open, as a viewer's would be
    assert reader.execute("SELECT COUNT(*) FROM event").fetchone() == (20,)
    assert (tmp_path / "world.db-wal").stat().st_size > 0
    store.close()
    assert (tmp_path / "world.db-wal").stat().st_size == 0
    reader.close()


@pytest.mark.parametrize("size", [8, 4000], ids=["small", "past the inline limit"])
def test_a_body_that_is_not_text_is_kept_as_its_bytes_and_read_back_identical(
    size: int, world: tuple[SqliteStore, RunClock], tmp_path: Path
) -> None:
    """A body that is not UTF-8 (a file, JSON invalid in its charset) is kept as exactly its bytes, beside a text
    body read back as text. Before schema 7 such a call was not recorded at all."""
    store, _ = world
    raw = (b"PK\x03\x04\xff\xfe\x00" * size)[:size]
    store.attach(
        Exchange(method="GET", host="h", path="/doc", status=200, request_body="q" * 4000, response_bytes=raw),
        first_seq=1,
        last_seq=0,
    )
    store.attach(Exchange(method="GET", host="h", path="/", status=200), first_seq=1, last_seq=0)

    first, second = store.calls()
    assert first.exchange.response_bytes == raw and first.exchange.response_body is None
    assert first.exchange.request_body == "q" * 4000 and first.exchange.request_bytes is None
    assert second.exchange.response_bytes is None
    reopened = SqliteStore(tmp_path / "world.db", store.run_id, RunClock(datetime(2026, 8, 24, tzinfo=UTC)))
    assert reopened.calls()[0].exchange.response_bytes == raw


def prose(seed: str, size: int) -> str:
    """Text that compresses like a real message, not like one letter repeated: a run of distinct hashes."""
    return "".join(hashlib.sha256(f"{seed}{i}".encode()).hexdigest() for i in range(size // 64 + 1))[:size]


def conversation(turns: int) -> str:
    """A model request as an agent sends it: every earlier turn again, then the new one, and its tools."""
    messages = ",".join(f'{{"role":"user","content":"turn {n}: {prose(str(n), 900)}"}}' for n in range(turns))
    tools = ",".join(
        f'{{"type":"function","name":"tool_{n}","description":"{prose("tool" + str(n), 700)}"}}' for n in range(8)
    )
    return f'{{"model":"m","messages":[{messages}],"tools":[{tools}]}}'


def stored_bytes(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COALESCE(SUM(length(stored)), 0) FROM content").fetchone()[0]


def test_a_conversation_sent_again_with_a_turn_appended_stores_only_the_new_turn(
    world: tuple[SqliteStore, RunClock], tmp_path: Path
) -> None:
    """Thirty calls, each the whole conversation so far: stored once per call, the conversation would be kept
    thirty times over. Kept as parts, each call adds its new turn and the row listing its parts, and nothing else."""
    store, _ = world
    path = tmp_path / "world.db"
    sizes: list[int] = []
    for turns in range(1, 31):
        body = conversation(turns)
        assert len(body.encode()) >= PARTS_FROM
        store.attach(
            Exchange(method="POST", host="model.example", path="/v1/chat", status=200, request_body=body),
            first_seq=1,
            last_seq=0,
        )
        sizes.append(stored_bytes(path))
    growth = [b - a for a, b in itertools.pairwise(sizes)]
    turn = len('{"role":"user","content":"turn 9: ' + prose("9", 900) + '"},')
    # Each call adds its new turn, the turn before it once more (the separator after it changed), and its list of
    # part hashes, one hash a part.
    assert all(g < 2 * turn + 32 * 60 for g in growth), growth
    whole = sum(len(conversation(t).encode()) for t in range(1, 31))
    # The text compresses about 2x on its own, so every call kept whole would be about whole // 2.
    assert sizes[-1] < whole // 8
    reopened = SqliteStore(path, "root", RunClock(START))
    assert [c.exchange.request_body for c in reopened.calls()] == [conversation(t) for t in range(1, 31)]


def test_discarding_a_fork_keeps_every_part_its_parent_still_reads(
    world: tuple[SqliteStore, RunClock], tmp_path: Path
) -> None:
    """The fork's request shares all but its last turn with the parent's: discarding the fork removes that turn and
    the fork's own body row, and every part of the parent's body is still there to join."""
    store, _ = world
    path = tmp_path / "world.db"
    parents = conversation(6)
    store.attach(Exchange(method="POST", host="m", path="/", status=200, request_body=parents), first_seq=1, last_seq=0)
    held = (content_rows(path), stored_bytes(path))
    fork = store.fork("child", at_seq=store.head(), clock=RunClock(START))
    fork.attach(
        Exchange(method="POST", host="m", path="/", status=200, request_body=conversation(7)), first_seq=1, last_seq=0
    )
    # The seventh turn, the sixth turn's part (the separator after it is now a comma, not the end of the list), and
    # the fork's body row.
    assert content_rows(path) == held[0] + 3
    # A run's size is the stored bodies only it refers to, its body's parts counted: exactly the three rows it added.
    assert fork.usage().bodies == stored_bytes(path) - held[1]
    fork.discard()

    assert (content_rows(path), stored_bytes(path)) == held
    [call] = SqliteStore(path, "root", RunClock(START)).calls()
    assert call.exchange.request_body == parents


def test_a_sweep_keeps_the_parts_of_a_body_a_row_refers_to(world: tuple[SqliteStore, RunClock], tmp_path: Path) -> None:
    """A part is referred to by no row, only by the body it is part of: a sweep that counted rows alone would free
    every part and leave the body unreadable."""
    store, _ = world
    store.attach(
        Exchange(method="POST", host="m", path="/", status=200, request_body=conversation(5)), first_seq=1, last_seq=0
    )
    freed = store.sweep()
    assert freed.bodies == 0
    [call] = SqliteStore(tmp_path / "world.db", "root", RunClock(START)).calls()
    assert call.exchange.request_body == conversation(5)


def test_a_sweep_keeps_a_body_another_connection_writes_while_it_sweeps(tmp_path: Path) -> None:
    """`minutehand gc` sweeps a world file a run may be writing. The sweep reads the references, then the stored
    bodies, then deletes: a run that commits a body and its row between the two reads must wait for the sweep, or
    the sweep sees the body and not the row and deletes a body the run refers to."""
    path = tmp_path / "world.db"
    live = SqliteStore(path, "root", RunClock(START))
    sweeper = SqliteStore(path, "root", RunClock(START))
    body = "written by the live run while the sweep ran " + "x" * 2000
    writer = threading.Thread(
        target=live.apply, args=(Change(entity=entity("a"), operation=Operation.CREATE, actor=Actor.AGENT, body=body),)
    )

    def between_the_reads(statement: str) -> None:
        if statement.startswith("SELECT hash, length(stored) FROM content") and not writer.is_alive():
            writer.start()
            writer.join(timeout=1.0)

    sweeper._db.set_trace_callback(between_the_reads)  # pyright: ignore[reportPrivateUsage]
    sweeper.sweep()
    writer.join(timeout=30)
    assert not writer.is_alive()
    got = SqliteStore(path, "root", RunClock(START)).get(entity("a"))
    assert got is not None and got.body == body
