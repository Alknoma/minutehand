"""`minutehand gc`: every world file under the state directory swept of the snapshot files and stored bodies
nothing refers to, with what was freed printed; what a kept snapshot names is never touched."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand import cli, session
from minutehand.adapters.store.sqlite import POOL_SUFFIX, SqliteStore
from minutehand.application.run_clock import RunClock

START = datetime(2026, 8, 24, 10, 51, tzinfo=UTC)


def test_gc_frees_what_a_crash_left_unreferenced_and_keeps_every_snapshot_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    world = session.run_dir(state, "r1") / session.WORLD
    world.parent.mkdir(parents=True)
    store = SqliteStore(world, "r1", RunClock(START))
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "agent.db").write_bytes(b"what the agent knows " * 1000)
    store.keep_snapshot(1, agent)
    store.close()
    pool = world.with_name(world.stem + POOL_SUFFIX)
    left = [pool / "ab" / f"{'ab' * 32}.zst", pool / "cd" / f"{'cd' * 32}.zst.1a2b3c4d.partial"]
    for orphan in left:
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"x" * 100)
    with sqlite3.connect(world) as db:
        db.execute("INSERT INTO content VALUES(?, 3, 'raw', ?)", (b"\x01" * 32, b"abc"))

    assert cli.main(["gc", "--state", str(state)]) == 0
    printed = capsys.readouterr().out
    assert printed == "freed 1 stored bodies (3 bytes) and 2 snapshot files (200 bytes) across 1 world files\n"
    assert not any(p.exists() for p in left)
    reopened = SqliteStore(world, "r1", RunClock(START))
    reopened.materialise("r1", 1, tmp_path / "restored")
    assert (tmp_path / "restored" / "agent.db").read_bytes() == (agent / "agent.db").read_bytes()


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
