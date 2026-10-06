"""The agent's snapshots kept by the store: each file once across every snapshot in the file, put back as the same
plain directory, pruned to the newest few except what is pinned, at the run's start or forked from, and never a
file a kept snapshot still names, even when the process dies halfway through."""

from __future__ import annotations

import os
import random
import sqlite3
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand.adapters.store.sqlite import POOL_SUFFIX, SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation
from tests.support.stored import everything

START = datetime(2026, 8, 24, 10, 51, tzinfo=UTC)


def pool(world: Path) -> list[Path]:
    directory = world.with_name(world.stem + POOL_SUFFIX)
    return sorted(p for p in directory.rglob("*") if p.is_file()) if directory.is_dir() else []


def agent_state(directory: Path, files: int = 300) -> None:
    directory.mkdir(parents=True)
    for n in range(files):
        (directory / f"part-{n:03d}.bin").write_bytes(random.Random(n).randbytes(2000))


def tree(directory: Path) -> dict[str, tuple[bytes | None, int]]:
    """Every entry under `directory`: a file's bytes and mode, a directory's mode."""
    found: dict[str, tuple[bytes | None, int]] = {}
    for path in sorted(directory.rglob("*")):
        mode = path.lstat().st_mode & 0o7777
        found[path.relative_to(directory).as_posix()] = (path.read_bytes() if path.is_file() else None, mode)
    return found


@pytest.fixture
def clock() -> RunClock:
    return RunClock(START)


@pytest.fixture
def world(tmp_path: Path, clock: RunClock) -> tuple[SqliteStore, Path]:
    return SqliteStore(tmp_path / "world.db", "root", clock), tmp_path / "world.db"


def test_a_snapshot_with_three_changed_files_of_three_hundred_stores_three_new_files(
    world: tuple[SqliteStore, Path], tmp_path: Path
) -> None:
    store, path = world
    state = tmp_path / "agent"
    agent_state(state)
    first = store.keep_snapshot(1, state)
    assert first.files == 300 and first.size == 600_000 and len(pool(path)) == 300
    before = set(pool(path))
    for n in (7, 150, 299):
        (state / f"part-{n:03d}.bin").write_bytes(os.urandom(2000))
    second = store.keep_snapshot(2, state)
    assert second.files == 300 and second.size == 600_000 and len(pool(path)) == 303
    assert second.held == sum(p.stat().st_size for p in set(pool(path)) - before)
    assert [s.wake for s in store.snapshots()] == [1, 2]


def test_a_snapshot_is_put_back_as_the_same_plain_directory_with_its_modes(
    world: tuple[SqliteStore, Path], tmp_path: Path
) -> None:
    store, _ = world
    state = tmp_path / "agent"
    (state / "nested" / "deeper").mkdir(parents=True)
    (state / "empty-directory").mkdir()
    (state / "agent.db").write_bytes(os.urandom(300_000) + bytes(300_000))
    (state / "nested" / "run.sh").write_text("#!/bin/sh\necho restored\n")
    (state / "nested" / "run.sh").chmod(0o755)
    (state / "nested" / "deeper" / "read-only.json").write_text('{"k": 1}')
    (state / "nested" / "deeper" / "read-only.json").chmod(0o444)
    (state / "empty-file").write_bytes(b"")
    (state / "nested").chmod(0o750)
    store.keep_snapshot(4, state)

    store.materialise("root", 4, tmp_path / "restored")
    assert tree(tmp_path / "restored") == tree(state)
    with pytest.raises(FileExistsError):
        store.materialise("root", 4, tmp_path / "restored")


def test_a_link_in_a_snapshot_is_refused(world: tuple[SqliteStore, Path], tmp_path: Path) -> None:
    store, _ = world
    state = tmp_path / "agent"
    agent_state(state, files=1)
    (state / "link").symlink_to(state / "part-000.bin")
    with pytest.raises(ValueError, match="link, which is neither a regular file nor a directory"):
        store.keep_snapshot(1, state)


def snapshots_of_five_wakes(store: SqliteStore, clock: RunClock, tmp_path: Path) -> Path:
    """Wakes 0 to 4, each ending in a checkpoint and a snapshot of three files that never change and one that
    every wake rewrites, so each snapshot alone holds one file; answers the agent's directory."""
    state = tmp_path / "agent"
    agent_state(state, files=3)
    for wake in range(5):
        if wake > 0:
            clock.begin_wake()
        (state / "current.bin").write_bytes(f"wake {wake} ".encode() * 500)
        store.apply(
            Change(
                entity=EntityRef(provider="minutehand", kind=EntityKind.RECORD, external_id="checkpoint"),
                operation=Operation.CREATE if wake == 0 else Operation.UPDATE,
                actor=Actor.SCENARIO,
                body=f'{{"wake": {wake}}}',
            )
        )
        store.keep_snapshot(wake, state)
    return state


def test_pruning_keeps_the_newest_the_pinned_and_the_start_and_lets_the_rest_go(
    world: tuple[SqliteStore, Path], clock: RunClock, tmp_path: Path
) -> None:
    store, path = world
    snapshots_of_five_wakes(store, clock, tmp_path)
    store.pin("root", 2, pinned=True)
    files_before = len(pool(path))

    pruned = store.prune(1)

    assert [s.wake for s in pruned] == [3, 1]
    assert {s.wake: (s.pinned, s.pruned) for s in store.snapshots()} == {
        0: (False, False),
        1: (False, True),
        2: (True, False),
        3: (False, True),
        4: (False, False),
    }
    assert len(pool(path)) == files_before - 2, "the file each pruned wake alone wrote is gone, nothing else"
    for wake in (0, 2, 4):
        store.materialise("root", wake, tmp_path / f"kept-{wake}")
    with pytest.raises(LookupError, match="was pruned"):
        store.materialise("root", 3, tmp_path / "pruned-3")
    with pytest.raises(ValueError, match="already pruned"):
        store.pin("root", 3, pinned=True)
    store.pin("root", 2, pinned=False)
    assert [s.wake for s in store.prune(1)] == [2]


def test_a_snapshot_a_fork_was_taken_from_is_never_pruned(
    world: tuple[SqliteStore, Path], clock: RunClock, tmp_path: Path
) -> None:
    store, _ = world
    snapshots_of_five_wakes(store, clock, tmp_path)
    fork = store.fork("what-if", at_seq=3, clock=RunClock(START))  # the checkpoint written at the end of wake 2
    grandchild = fork.fork("what-if-again", at_seq=3, clock=RunClock(START))
    assert [s.wake for s in grandchild.snapshots()] == [0, 1, 2]

    assert [s.wake for s in store.prune(1)] == [3, 1]
    store.materialise("root", 2, tmp_path / "for-the-fork")


def prune_and_die_at(path: Path, statement: str) -> None:
    """Prune to one snapshot in a process of its own, which dies the moment it issues `statement`."""
    program = textwrap.dedent(
        f"""
        import os
        from datetime import UTC, datetime
        from pathlib import Path
        from minutehand.adapters.store.sqlite import SqliteStore
        from minutehand.application.run_clock import RunClock

        store = SqliteStore(Path({str(path)!r}), "root", RunClock(datetime(2026, 8, 24, tzinfo=UTC)))

        def die(issued: str) -> None:
            if issued.startswith({statement!r}):
                os._exit(9)

        store._db.set_trace_callback(die)
        store.prune(1)
        """
    )
    died = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=30)
    assert died.returncode == 9, died.stderr


def test_a_process_killed_while_pruning_leaves_every_snapshot_whole(
    world: tuple[SqliteStore, Path], clock: RunClock, tmp_path: Path
) -> None:
    """The process dies at the first change to a manifest: nothing was committed and no file was removed."""
    store, path = world
    snapshots_of_five_wakes(store, clock, tmp_path)
    files_before = len(pool(path))
    store.close()
    prune_and_die_at(path, "UPDATE snapshot SET pruned")

    after = SqliteStore(path, "root", RunClock(START))
    assert not any(s.pruned for s in after.snapshots()) and len(pool(path)) == files_before
    for wake in range(5):
        after.materialise("root", wake, tmp_path / f"kept-{wake}")


def test_a_process_killed_between_pruning_and_removing_files_leaves_every_kept_snapshot_whole(
    world: tuple[SqliteStore, Path], clock: RunClock, tmp_path: Path
) -> None:
    """The process dies after the pruned manifests were committed and before their files were removed: every kept
    snapshot still materialises, the pruned files are left unreferenced, and the next sweep frees them."""
    store, path = world
    snapshots_of_five_wakes(store, clock, tmp_path)
    files_before = len(pool(path))
    store.close()
    prune_and_die_at(path, "BEGIN IMMEDIATE")

    after = SqliteStore(path, "root", RunClock(START))
    assert [s.wake for s in after.snapshots() if s.pruned] == [1, 2, 3]
    assert len(pool(path)) == files_before
    for wake in (0, 4):
        after.materialise("root", wake, tmp_path / f"kept-{wake}")
    freed = after.sweep()
    assert freed.files == 3 and freed.bodies == 0
    assert len(pool(path)) == files_before - 3
    after.materialise("root", 4, tmp_path / "still-4")


def test_a_discarded_fork_takes_the_snapshot_files_only_it_named(
    world: tuple[SqliteStore, Path], clock: RunClock, tmp_path: Path
) -> None:
    store, path = world
    state = snapshots_of_five_wakes(store, clock, tmp_path)
    before = len(pool(path))
    fork = store.fork("refused", at_seq=5, clock=RunClock(START))
    (state / "fork-only.bin").write_bytes(b"only the fork wrote this")
    fork.keep_snapshot(5, state)
    assert len(pool(path)) == before + 1
    fork.discard()
    assert len(pool(path)) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM snapshot_file WHERE run_id='refused'").fetchone() == (0,)
    store.materialise("root", 4, tmp_path / "parent-4")


def test_a_snapshot_file_is_searchable_on_disk_only_decompressed(
    world: tuple[SqliteStore, Path], tmp_path: Path
) -> None:
    store, path = world
    state = tmp_path / "agent"
    state.mkdir()
    marker = b"the agent's own words, " * 400
    (state / "notes.txt").write_bytes(marker)
    store.keep_snapshot(1, state)
    pooled = b"".join(p.read_bytes() for p in pool(path))
    assert marker not in pooled, "pooled files are compressed"
    assert marker in everything(tmp_path / "world.pool")


def test_what_a_run_alone_holds_is_its_size(world: tuple[SqliteStore, Path], clock: RunClock, tmp_path: Path) -> None:
    store, _ = world
    snapshots_of_five_wakes(store, clock, tmp_path)
    store.apply(
        Change(
            entity=EntityRef(provider="ledger", kind=EntityKind.RECORD, external_id="big"),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body="b" * 50_000,
        )
    )
    fork = store.fork("child", at_seq=store.head(), clock=RunClock(START))
    assert fork.usage().bodies == 0 and fork.usage().snapshots == 0
    root = store.usage()
    assert root.bodies > 0 and root.snapshots > 0 and root.rows > 0
