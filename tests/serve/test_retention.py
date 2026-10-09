"""A closed world is a run in the state directory; only the newest closed worlds are kept."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from minutehand import session
from minutehand.domain.run import StopReason
from minutehand.serve import ServeOptions
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from tests.serve.support import spec


def test_a_closed_world_is_a_run_findings_reads_and_only_the_newest_are_kept(tmp_path: Path) -> None:
    options = ServeOptions(proxy_port=0, control_port=0, telemetry_port=0, keep=2)
    with serve_in_background(tmp_path, options) as url, MinutehandClient(url) as client:
        opened = [client.create_world(spec(f"xoxb-kept-{n}")).world_id for n in range(3)]
        checked = client.checks(opened[0])
        assert checked.result.verdict.stop is None
        closed = [client.close_world(w) for w in opened]
        assert all(c.result.verdict.stop is StopReason.CLOSED for c in closed)
    kept = session.runs(tmp_path)
    assert [o.record.run_id for o in kept] == opened[1:]
    assert all(o.record.stop is StopReason.CLOSED for o in kept)
    assert not (tmp_path / "runs" / opened[0]).exists()
    assert session.scenario_of(tmp_path, opened[2]).owner == "owen"


def _left_by_a_crash(state: Path, world_id: str) -> None:
    with sqlite3.connect(state / "runs" / world_id / "world.db") as db:
        db.execute("INSERT INTO content VALUES(?, 15, 'raw', ?)", (CRASHED, b"left by a crash"))


def _held(state: Path, world_id: str) -> int:
    with sqlite3.connect(state / "runs" / world_id / "world.db") as db:
        found: tuple[int] = db.execute("SELECT COUNT(*) FROM content WHERE hash=?", (CRASHED,)).fetchone()
    return found[0]


CRASHED = b"\x01" * 32


def test_retention_sweeps_the_worlds_it_keeps_of_what_nothing_refers_to(tmp_path: Path) -> None:
    """The first close of a server runs the same sweep as `minutehand gc`: a stored body left unreferenced in a world
    still kept, as a crash of the server before would leave it, is gone after it."""
    options = ServeOptions(proxy_port=0, control_port=0, telemetry_port=0, keep=5)
    with serve_in_background(tmp_path, options) as url, MinutehandClient(url) as client:
        kept = client.create_world(spec("xoxb-kept-a")).world_id
        client.close_world(kept)
    _left_by_a_crash(tmp_path, kept)
    with serve_in_background(tmp_path, options) as url, MinutehandClient(url) as client:
        closed = client.create_world(spec("xoxb-kept-b")).world_id
        client.close_world(closed)
    assert _held(tmp_path, kept) == 0
    assert [o.record.run_id for o in session.runs(tmp_path)] == [kept, closed]


def test_a_close_sweeps_no_world_this_server_swept_since_it_closed(tmp_path: Path) -> None:
    """Nothing writes a closed world's file again, so a close leaves alone each one the server has swept since it
    closed: sweeping all `keep` of them at every close made a close cost a store opened per world kept."""
    options = ServeOptions(proxy_port=0, control_port=0, telemetry_port=0, keep=5)
    with serve_in_background(tmp_path, options) as url, MinutehandClient(url) as client:
        kept = client.create_world(spec("xoxb-kept-a")).world_id
        client.close_world(kept)
        client.close_world(client.create_world(spec("xoxb-kept-b")).world_id)
        _left_by_a_crash(tmp_path, kept)
        client.close_world(client.create_world(spec("xoxb-kept-c")).world_id)
        assert _held(tmp_path, kept) == 1
