"""A world file is written by one machine at a time (`adapters.store.sharing`), and a store whose file lost what it
had committed stops at its next write, naming what was lost.

A run in a container whose state directory was opened from the host through Docker Desktop's bind mount lost the end
of its first wake: the people engine's pending entries, the run loop's table, the checkpoint. The loop still held
them; four wakes later a person's moment came for an entry the file no longer had, and the run ended in "LookupError:
the engine holds no pending item ...", read as a bug in the engine. Here the loss is made by hand, as that opener made
it: everything written after the seed taken out of the file, under a store that had committed it.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.providers.asana.provider import build as asana
from minutehand.adapters.store import sharing
from minutehand.adapters.store.sqlite import SqliteStore, truncate_log
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.refusals import RunRefused
from minutehand.application.replier import PeopleReplier
from minutehand.application.run_clock import RunClock
from minutehand.application.sandbox import SandboxReading
from minutehand.domain.scenario import Answers, Person, Scenario, SeededTicket
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation
from minutehand.session import _ReadOnlyStore
from tests.orchestrator.test_due_records import AGENT, _Poller, close
from tests.providers.asana.asana_workspace import SCENARIO as ASANA
from tests.support.people import people_model

T0 = datetime(2026, 10, 16, 16, 0, tzinfo=UTC)
ELSEWHERE = "linux:a-kernel-this-process-is-not-on"


def _change(n: int) -> Change:
    return Change(
        entity=EntityRef(provider="p", kind=EntityKind.RECORD, external_id=f"r{n}"),
        operation=Operation.CREATE,
        actor=Actor.SCENARIO,
        body="{}",
        parent="p",
    )


def _lose_after(path: Path, seq: int) -> None:
    """What an opener on another kernel left the file holding: nothing after `seq`, though it was committed."""
    db = sqlite3.connect(path)
    db.execute("DELETE FROM event WHERE seq > ?", (seq,))
    db.execute("DELETE FROM entity_version WHERE seq > ?", (seq,))
    db.commit()
    db.close()


def test_a_store_whose_file_lost_committed_events_is_refused_at_its_next_write(tmp_path: Path) -> None:
    path = tmp_path / "world.db"
    store = SqliteStore(path, "r", RunClock(T0))
    for n in range(5):
        store.apply(_change(n))
    _lose_after(path, 3)

    # Mutation: dropping the check writes seq 4 again, over what the run had already read as written.
    with pytest.raises(RunRefused, match=r"lost events 4\.\.5 after they were written: another machine opened it"):
        store.apply(_change(5))
    with pytest.raises(RunRefused, match=r"lost events 4\.\.5"):
        store.apply_all([_change(6)])
    assert store.head() == 3, "nothing written over what was lost"


def test_a_store_that_reopens_a_file_reads_its_head_from_it_and_writes_on(tmp_path: Path) -> None:
    path = tmp_path / "world.db"
    first = SqliteStore(path, "r", RunClock(T0))
    for n in range(3):
        first.apply(_change(n))
    first.close()
    again = SqliteStore(path, "r", RunClock(T0))
    assert again.apply(_change(3)).seq == 4, "a store that opens a file has lost nothing it wrote"


class _Opened(_Poller):
    """The agent's sandbox, while something on another kernel opens the run's file: once the first wake's pending
    entries are in it, everything written after the seed is gone from it."""

    def __init__(self, path: Path) -> None:
        super().__init__(close, at=T0)
        self._path = path
        self._lost = False

    async def read(self) -> SandboxReading:
        if not self._lost and self._store is not None:
            pending = [e for e in self._store.events() if e.entity.kind is EntityKind.PENDING]
            if pending:
                seeded = next(e.seq for e in self._store.events() if e.entity.kind is EntityKind.RECORD)
                checkpoint = max(e.seq for e in self._store.events() if e.seq < pending[0].seq)
                _lose_after(self._path, max(seeded, checkpoint))
                self._lost = True
        return await super().read()


def _tracker_scenario() -> Scenario:
    """One person, no takes, two open tickets on the tracker the engine plays: both pending in the first wake, each
    moved at a moment of its own in a later one."""
    people = [
        Person(key="iris", name="Iris Calder", email="iris@example.com"),
        Person(key="john", name="John Abel", email="john@example.com", reply=Answers()),
    ]
    tickets = [
        SeededTicket(provider="asana", project="Venue Move", title="Book the freight lift", assignee="john"),
        SeededTicket(provider="asana", project="Venue Move", title="Return the old keys", assignee="john"),
    ]
    played = ASANA.model_copy(
        update={
            "goal": None,
            "owner": None,
            "starts_at": T0,
            "people": people,
            "tickets": tickets,
            "transitions_on": ["asana"],
            "runs_for": timedelta(days=3),
        }
    )
    return Scenario.model_validate(played.model_dump())


async def test_a_run_whose_file_lost_its_first_wakes_end_stops_naming_the_loss_not_a_missing_pending_item(
    tmp_path: Path,
) -> None:
    played = _tracker_scenario()
    path = tmp_path / "world.db"
    clock = RunClock(T0)
    store = SqliteStore(path, "r", clock)
    sandbox = _Opened(path)
    model = people_model()

    # Before: the run went on with a table the file no longer held, and ended at the first person's moment in
    # "LookupError: the engine holds no pending item john|asana|ticket|...". Mutation: dropping the store's check
    # brings that LookupError back.
    with pytest.raises(RunRefused, match=r"the world file .* lost events \d+\.\.\d+ after they were written"):
        await run_scenario(
            scenario=played,
            agent=AGENT.model_copy(update={"due": []}),
            sandbox=sandbox,
            mounts=sandbox,
            store=store,
            clock=clock,
            services=Services(providers=[asana()]),
            replier=PeopleReplier(played, model),
            model=model,
        )
    assert sandbox._lost


def _written_elsewhere(path: Path, *, age: float = 0.0) -> Path:
    """A store on another kernel saying it writes `path`, `age` seconds ago."""
    mark = path.with_name(f"{path.name}.writing-1-feedface")
    mark.write_text(ELSEWHERE, encoding="utf-8")
    then = time.time() - age
    os.utime(mark, (then, then))
    return mark


def test_a_world_file_written_on_another_kernel_is_refused_to_every_opener(tmp_path: Path) -> None:
    path = tmp_path / "world.db"
    SqliteStore(path, "r", RunClock(T0)).close()
    _written_elsewhere(path)

    # Mutation: dropping `refuse_written_elsewhere` from any of these lets that opener cost the writer its writes.
    with pytest.raises(RunRefused, match=r"is being written by a run on another machine \(linux:a-kernel-"):
        SqliteStore(path, "r", RunClock(T0))
    with pytest.raises(RunRefused, match=r"another machine"):
        _ReadOnlyStore(path, "r", RunClock(T0))
    with pytest.raises(RunRefused, match=r"another machine"):
        _ReadOnlyStore.runs_in(path)
    with pytest.raises(RunRefused, match=r"another machine"):
        truncate_log(path)
    with pytest.raises(RunRefused, match=r"another machine"):
        SqliteStore(tmp_path / "other.db", "o", RunClock(T0)).adopt_written(path)


def test_a_writer_on_another_kernel_that_stopped_saying_so_is_gone(tmp_path: Path) -> None:
    path = tmp_path / "world.db"
    SqliteStore(path, "r", RunClock(T0)).close()
    _written_elsewhere(path, age=sharing.STALE + 1)
    # Mutation: ignoring the mark's age refuses a file whose container was stopped, for ever.
    SqliteStore(path, "r", RunClock(T0)).close()


def test_a_writer_on_this_kernel_shares_the_file(tmp_path: Path) -> None:
    path = tmp_path / "world.db"
    writer = SqliteStore(path, "r", RunClock(T0))
    reader = _ReadOnlyStore(path, "r", RunClock(T0))  # a viewer in the same container, or on the same VM
    assert reader.head() == writer.head()
    reader.close()
    writer.close()


def test_a_writer_marks_the_file_while_it_is_open_and_refreshes_the_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sharing, "BEAT", 0.05)
    monkeypatch.setattr(sharing._Beat, "_thread", None)  # a beat of its own, at the test's pace
    path = tmp_path / "world.db"
    store = SqliteStore(path, "r", RunClock(T0))
    [mark] = path.parent.glob("world.db.writing-*")
    assert mark.read_text(encoding="utf-8") == sharing.kernel()
    old = time.time() - 60
    os.utime(mark, (old, old))
    deadline = time.monotonic() + 5
    while mark.stat().st_mtime <= old + 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    # Mutation: no beat leaves a long run's mark stale, and an opener elsewhere takes it as gone.
    assert mark.stat().st_mtime > old + 1, "the mark was not refreshed while the store was open"
    store.close()
    assert not mark.exists(), "a closed store still says it writes the file"
