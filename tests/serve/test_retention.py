"""A closed world is a run in the state directory; only the newest closed worlds are kept."""

from __future__ import annotations

from pathlib import Path

from minutehand import session
from minutehand.adapters.store.sqlite import POOL_SUFFIX
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


def test_retention_sweeps_the_worlds_it_keeps_of_what_nothing_refers_to(tmp_path: Path) -> None:
    """Closing a world runs the same sweep as `minutehand gc`: a file left unreferenced in a world still kept, as a
    crash would leave it, is gone after the next close."""
    options = ServeOptions(proxy_port=0, control_port=0, telemetry_port=0, keep=5)
    with serve_in_background(tmp_path, options) as url, MinutehandClient(url) as client:
        kept = client.create_world(spec("xoxb-kept-a")).world_id
        client.close_world(kept)
        orphan = tmp_path / "runs" / kept / f"world{POOL_SUFFIX}" / "ab" / f"{'ab' * 32}.zst"
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"left by a crash")
        closed = client.create_world(spec("xoxb-kept-b")).world_id
        client.close_world(closed)
    assert not orphan.exists()
    assert [o.record.run_id for o in session.runs(tmp_path)] == [kept, closed]
