"""People acting on a tracker's items through a whole run: each item pending on a person keeps one record from the
wake it began to wait on them, under the key it was written at, and is moved at its moment in a later wake; a fork
from a checkpoint taken after those records were written moves them as its parent did; and every item whose moment
falls on one decision is moved.

These hold on the code as it was when a run ended in "LookupError: the engine holds no pending item ...": that run's
file had lost the records (`tests/test_world_file_sharing.py`); the engine never built a key two ways.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from minutehand.adapters.providers.asana.provider import build as asana
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.checkpoint import checkpoints
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier import PeopleReplier
from minutehand.application.rewind import fork_run
from minutehand.application.run_clock import RunClock
from minutehand.domain.experiment import Fork
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import Scenario, Take
from minutehand.domain.world import EntityKind, Operation, PendingSnapshot, PendingStatus, TransitionSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store
from tests.orchestrator.test_due_records import AGENT, _NothingToPlay, _Poller, _Program, close
from tests.support.people import people_model
from tests.test_world_file_sharing import T0, _tracker_scenario

QUIET = AGENT.model_copy(update={"due": []})
LEGAL = {"Done (completed)", "Cancelled (completed)"}


async def _run(tmp_path: Path, played: Scenario) -> tuple[RunRecord, SqliteStore]:
    clock = RunClock(T0)
    store = SqliteStore(tmp_path / "world.db", "parent", clock)
    sandbox = _Poller(close, at=T0)
    model = people_model()
    record = await run_scenario(
        scenario=played,
        agent=QUIET,
        sandbox=sandbox,
        mounts=sandbox,
        store=store,
        clock=clock,
        services=Services(providers=[asana()]),
        replier=PeopleReplier(played, model),
        model=model,
    )
    return record, store


def _pending(store: Store) -> dict[str, PendingSnapshot]:
    """Each record the engine wrote, by its key, as it last stood: created at the seq its key ends in."""
    found: dict[str, PendingSnapshot] = {}
    for e in store.events():
        if e.entity.kind is not EntityKind.PENDING or not isinstance(e.after, PendingSnapshot):
            continue
        if e.operation is Operation.CREATE:
            assert e.entity.external_id.endswith(f"|{e.seq:010d}"), f"{e.entity.external_id} written at seq {e.seq}"
        found[e.entity.external_id] = e.after
    return found


def _moves(store: Store) -> list[tuple[int, TransitionSnapshot]]:
    return [(e.wake, e.after) for e in store.events() if isinstance(e.after, TransitionSnapshot)]


async def test_two_items_pending_on_one_person_from_one_wake_are_each_moved_legally_at_a_later_moment(
    tmp_path: Path,
) -> None:
    record, store = await _run(tmp_path, _tracker_scenario())

    held = _pending(store)
    assert [p.person for p in held.values()] == ["john", "john"], "one record per item, written in one wake"
    assert all(p.status is PendingStatus.ACTED for p in held.values())
    moves = _moves(store)
    assert len(moves) == 2 and all(m.name in LEGAL and m.who == "john" for _, m in moves)
    assert record.stop is not None


async def test_a_fork_from_after_the_records_were_written_moves_both_items_at_their_moments(tmp_path: Path) -> None:
    played = _tracker_scenario()
    parent, store = await _run(tmp_path, played)
    written = max(
        e.seq for e in store.events() if e.entity.kind is EntityKind.PENDING and e.operation is Operation.CREATE
    )
    at_seq = min(seq for seq in checkpoints(store) if seq > written)
    at = checkpoints(store)[at_seq].now
    store.close()

    def open_parent(clock: Clock) -> SqliteStore:
        return SqliteStore(tmp_path / "world.db", "parent", clock)

    model = people_model()
    sandbox = _Poller(close, at=at)
    [child] = await fork_run(
        fork=Fork(parent_run="parent", at_seq=at_seq),
        parent=parent,
        open_parent=open_parent,
        run_id="child",
        scenario=played,
        agent=QUIET,
        sandbox=sandbox,
        mounts=sandbox,
        playback_for=lambda _store, _checkpoint: _NothingToPlay(),
        services=Services(providers=[asana()]),
        replier_for=lambda s, _pins: PeopleReplier(s, model),
        state_dir=tmp_path / "state",
        own=_Program(),
        model=model,
    )
    forked = SqliteStore(tmp_path / "world.db", "child", RunClock(T0))
    # Mutation: a key built from a seq other than the one it is written at fails here, as in the parent run.
    held = _pending(forked)
    assert len(held) == 2 and all(p.status is PendingStatus.ACTED for p in held.values())
    moves = [m for _, m in _moves(forked)]
    assert len(moves) == 2 and all(m.name in LEGAL and m.who == "john" for m in moves)
    assert child.stop is not None


async def test_every_item_whose_moment_falls_on_one_decision_is_moved(tmp_path: Path) -> None:
    played = _tracker_scenario()
    take = Take(provider="asana", take="Done (completed)", after=timedelta(hours=2))
    people = [p.model_copy(update={"takes": [take]}) if p.key == "john" else p for p in played.people]
    _, store = await _run(tmp_path, played.model_copy(update={"people": people}))

    moves = _moves(store)
    # Mutation: firing only the first of what fell due leaves the second ticket open, its record pending.
    assert [m.name for _, m in moves] == ["Done (completed)", "Done (completed)"]
    assert len({wake for wake, _ in moves}) == 1, "both moved at the one decision their moment fell on"
    assert all(p.status is PendingStatus.ACTED for p in _pending(store).values())
