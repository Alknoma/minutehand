"""Forks of an agent that works in the background in two processes, with no hooks: its memory is the run's.

The reference agent answers its wake at once and does the work in a worker, both keeping everything they know in
`minutehand.agent.store`. Under Minutehand that memory is the run's log, so a fork from a middle checkpoint starts
from exactly the memory the parent had there, and its start is proven by the agent's report. Before, the agent's
SQLite file was snapshotted and restored by hooks it had to write (snapshot, restore, busy, fingerprint).
"""

from __future__ import annotations

import re

from minutehand import session
from minutehand.application.restore import Verification
from tests.architecture.support import Rig


def _wake_points(out: str) -> dict[int, int]:
    return {int(w): int(s) for s, w in re.findall(r"seq (\d+), after wake (\d+): restorable", out)}


def test_a_fork_from_a_middle_checkpoint_starts_from_the_parents_memory_there_and_is_verified_by_its_report(
    rig: Rig,
) -> None:
    """Owen's direction after six hours is noted in the memory between wakes 1 and 2: a fork after wake 1 starts
    without it, and with every email and job of wake 1."""
    parent = rig.run("scenario_directed.yaml")
    assert parent.code in (0, 1, 3), parent.out + parent.err[-2000:]
    at = _wake_points(parent.out)[1]
    later = rig.memory(parent.run_id, "notes")
    assert later, "the parent noted Owen's direction after wake 1"

    forked = rig.fork(parent.run_id, at, [])

    assert forked.code in (0, 1, 3), forked.out + forked.err[-2000:]
    child = forked.run_id
    for collection in ("facts", "jobs", "sent", "replies", "notes", "approvals"):
        assert rig.memory(child, collection, until=at) == rig.memory(parent.run_id, collection, until=at), collection
    assert rig.memory(child, "notes", until=at) == {}
    assert rig.memory(child, "sent", until=at), "wake 1's email is in the memory the fork starts from"
    restored = session.restore_of(rig.state, child)
    assert restored is not None and restored.verified, restored
    assert restored.verified_by == [Verification.MEMORY, Verification.REPORT]


def test_the_agents_own_database_is_never_opened_under_minutehand(rig: Rig) -> None:
    done = rig.run("scenario.yaml", policy=False)

    assert done.code == 0, done.out + done.err[-2000:]
    assert not (rig.home / "agent.db").exists()
    assert rig.memory(done.run_id, "facts")["done"] == "1"


def test_a_report_that_says_idle_while_the_worker_still_writes_leaves_checkpoints_a_fork_refuses(rig: Rig) -> None:
    """The naive report says IDLE once the worker has picked its job up; the worker then writes for two more
    seconds. Those writes land after the wake's checkpoint, and that checkpoint is not restorable."""
    done = rig.run(
        "scenario.yaml",
        env={"REFERENCE_BEHAVIOUR": "slow", "REFERENCE_SLOW_SECONDS": "2", "REFERENCE_REPORT": "naive"},
    )

    assert "not restorable: the agent went on writing its memory after this checkpoint" in done.out, done.out
