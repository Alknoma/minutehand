"""Checkpoints of an agent that works in the background, and restores proven by more than the report.

The reference agent answers its wake at once and does the work in a worker. Its naive report says IDLE as soon as
the worker has picked the job up. Before: a checkpoint was taken mid-job and called restorable, both when the
work was writes to its own database (invisible to the proxy) and when it was a model call on a tunnel that was
already open; and a restore of another moment whose report read the same, or one that left the worker running
with the future in its memory, was verified.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import sqlite3
from contextlib import closing

import pytest

from tests.architecture.support import HOOKS, PY, Rig

BUSY = {"busy": [PY, HOOKS, "busy"]}
FINGERPRINT = {"fingerprint": [PY, HOOKS, "fingerprint"]}


def _jobs_in_snapshots(rig: Rig, run_id: str) -> dict[str, list[str]]:
    """The state of each job in each snapshot the run's store kept, by wake, each written out to be read."""
    found: dict[str, list[str]] = {}
    with rig.world(run_id) as world:
        for kept in world.snapshots():
            into = rig.base / "read" / f"wake-{kept.wake}"
            world.materialise(kept.run_id, kept.wake, into)
            with closing(sqlite3.connect(into / "agent.db")) as db:
                found[into.name] = [state for (state,) in db.execute("SELECT state FROM jobs")]
    return found


def test_a_busy_command_keeps_a_checkpoint_from_being_taken_mid_job(rig: Rig) -> None:
    """Two seconds of database-only work after the wake was answered, and a report that says IDLE meanwhile."""
    done = rig.run(
        "scenario.yaml",
        env={"REFERENCE_BEHAVIOUR": "slow", "REFERENCE_SLOW_SECONDS": "2", "REFERENCE_REPORT": "naive"},
        state=BUSY,
    )

    assert done.code == 0, done.out + done.err[-2000:]
    snapshots = _jobs_in_snapshots(rig, done.run_id)
    assert snapshots and all("running" not in jobs and "queued" not in jobs for jobs in snapshots.values()), snapshots
    assert "unconfirmed" not in done.out


def test_without_a_busy_command_every_checkpoint_says_it_is_unconfirmed(rig: Rig) -> None:
    done = rig.run("scenario.yaml")

    points = re.findall(r"seq \d+, after wake \d+: (.*)", done.out)
    assert points and all(p.startswith("restorable, unconfirmed: settled by the agent's report") for p in points)


def test_a_model_call_on_an_already_open_tunnel_holds_the_checkpoint(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The model answers after 1.5 s, on the worker's pooled connection: from the second wake on there is no
    CONNECT, only bytes on the open tunnel. The proxy holds the checkpoint until the answer's bytes come back.

    Wake 1's call is the first on a new TLS 1.3 connection, after which the server sends its session tickets: bytes
    from the host that cannot be told from an answer without opening the tunnel. That one is left out here, and is
    a known limit (docs/design.md)."""
    monkeypatch.setenv("MODEL_DELAY", "1.5")
    done = rig.run("scenario_silent.yaml", env={"REFERENCE_REPORT": "naive"})

    assert done.code in (1, 3), done.out + done.err[-2000:]
    snapshots = _jobs_in_snapshots(rig, done.run_id)
    later = {name: jobs for name, jobs in snapshots.items() if int(name.removeprefix("wake-")) >= 2}
    assert len(later) >= 2, snapshots
    assert all("running" not in jobs for jobs in later.values()), snapshots


def _wake_points(out: str) -> dict[int, int]:
    return {int(w): int(s) for s, w in re.findall(r"seq (\d+), after wake (\d+): restorable", out)}


def test_a_restore_of_another_moment_with_the_same_report_is_refused_by_its_fingerprint(rig: Rig) -> None:
    """Owen's direction after six hours is noted and changes nothing the agent reports: the checkpoints after
    wakes 1 and 2 report alike over different databases. A restore that takes wake 2's snapshot for wake 1's
    was verified by the report alone."""
    parent = rig.run("scenario_directed.yaml", state={**BUSY, **FINGERPRINT})
    at = _wake_points(parent.out)[1]

    good = rig.fork(parent.run_id, at, [])
    assert f"the agent was restored from seq {at}, verified" in good.out, good.out + good.err[-2000:]

    wrong = rig.fork(parent.run_id, at, [], env={"REFERENCE_RESTORE_BUG": "next"})
    assert wrong.code == 2, wrong.out
    assert "fingerprint: " in wrong.err and "next_wake" not in wrong.err, wrong.err[-2000:]


def test_a_restore_that_leaves_the_worker_running_is_refused_by_its_fingerprint(rig: Rig) -> None:
    """The worker runs as a service of its own that stopping the agent's command does not stop: after the restore
    it still remembers every email of the parent run."""
    try:
        parent = rig.run("scenario_directed.yaml", env={"REFERENCE_WORKER": "detached"}, state={**BUSY, **FINGERPRINT})
        at = _wake_points(parent.out)[1]
        forked = rig.fork(parent.run_id, at, [], env={"REFERENCE_WORKER": "detached"})
        assert forked.code == 2, forked.out
        assert "fingerprint: " in forked.err, forked.err[-2000:]
    finally:
        pidfile = rig.home / "worker.pid"
        if pidfile.is_file():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pidfile.read_text()), signal.SIGTERM)
