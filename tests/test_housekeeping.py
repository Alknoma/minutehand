"""`minutehand gc`: every world file under the state directory swept of the stored bodies nothing refers to, with
what was freed printed; a body a row refers to is never touched."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand import cli, session
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation

START = datetime(2026, 8, 24, 10, 51, tzinfo=UTC)


def test_gc_frees_what_a_crash_left_unreferenced_and_keeps_every_body_a_row_refers_to(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    world = session.run_dir(state, "r1") / session.WORLD
    world.parent.mkdir(parents=True)
    store = SqliteStore(world, "r1", RunClock(START))
    long = "what the agent knows " * 100
    store.apply(
        Change(
            entity=EntityRef(provider="memory", kind=EntityKind.MEMORY, external_id="default/k"),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body=long,
        )
    )
    store.close()
    with sqlite3.connect(world) as db:
        db.execute("INSERT INTO content VALUES(?, 3, 'raw', ?)", (b"\x01" * 32, b"abc"))

    assert cli.main(["gc", "--state", str(state)]) == 0
    printed = capsys.readouterr().out
    assert printed == "freed 1 stored bodies (3 bytes) across 1 world files\n"
    reopened = SqliteStore(world, "r1", RunClock(START))
    held = reopened.get(EntityRef(provider="memory", kind=EntityKind.MEMORY, external_id="default/k"))
    assert held is not None and held.body == long


def test_gc_says_which_world_files_it_could_not_sweep(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    world = session.run_dir(state, "old") / session.WORLD
    world.parent.mkdir(parents=True)
    with sqlite3.connect(world) as db:
        db.execute("CREATE TABLE run(run_id TEXT, parent TEXT, forked_at INTEGER)")
        db.execute("INSERT INTO run VALUES('old', NULL, NULL)")
        db.execute("PRAGMA user_version=5")
    assert cli.main(["gc", "--state", str(state)]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed[0].endswith("across 0 world files")
    assert "not swept" in printed[1] and "written with store schema 5" in printed[1]
