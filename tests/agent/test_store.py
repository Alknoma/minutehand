"""`minutehand_agent.store` in both of its modes: in production a pass-through to the agent's own backend that records
nothing, and under Minutehand the run's own memory, recorded in the run's log, that never touches the agent's
database."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tomllib
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from minutehand_agent import _store, _wire, store, wake

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.telemetry.receiver import AGENT_PATH, Receiver
from minutehand.application.memory import memory_of
from minutehand.application.run_clock import RunClock
from minutehand.domain.memory import MEMORY_PROVIDER
from minutehand.domain.world import Actor, EntityKind, NextWakeSnapshot, Operation

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
PACKAGE = Path(__file__).resolve().parents[2] / "packages" / "minutehand-agent"
"""The distribution `minutehand-agent`, which an agent installs in its own environment."""


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(_wire.ON, raising=False)
    monkeypatch.delenv(_wire.URL, raising=False)
    monkeypatch.setattr(_store._Configured, "backend", None)  # pyright: ignore[reportPrivateUsage]
    yield


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest, tmp_path: Path, production: None) -> store.Backend:
    chosen: store.Backend = (
        store.MemoryBackend() if request.param == "memory" else store.SqliteBackend(tmp_path / "a.db")
    )
    store.configure(chosen)
    return chosen


# -- production ------------------------------------------------------------------------------------------------------


def test_put_get_delete_list_and_query_pass_through_to_the_backend(backend: store.Backend) -> None:
    store.put("asks/sam", {"status": "asked", "venue": {"city": "Lyon"}})
    store.put("asks/rosa", {"status": "confirmed"})
    store.put("owner", "owen@example.com")
    assert store.get("asks/sam") == {"status": "asked", "venue": {"city": "Lyon"}}
    assert store.get("nobody") is None and store.get("nobody", 3) == 3
    assert store.list("asks/") == [
        ("asks/rosa", {"status": "confirmed"}),
        ("asks/sam", {"status": "asked", "venue": {"city": "Lyon"}}),
    ]
    assert store.query("asks/", where={"status": "confirmed"}) == [("asks/rosa", {"status": "confirmed"})]
    assert store.query("", where={"venue.city": "Lyon"}) == [
        ("asks/sam", {"status": "asked", "venue": {"city": "Lyon"}})
    ]
    store.delete("asks/sam")
    store.delete("never-there")
    assert [k for k, _ in store.list()] == ["asks/rosa", "owner"]
    assert backend.get("default", "owner") == '"owen@example.com"'


def test_collections_keep_their_keys_apart(backend: store.Backend) -> None:
    jobs = store.collection("jobs")
    jobs.put("1", {"kind": "wake"})
    store.put("1", "not a job")
    assert jobs.get("1") == {"kind": "wake"} and store.get("1") == "not a job"
    assert jobs.list() == [("1", {"kind": "wake"})]


def test_a_batch_applies_all_of_its_writes_or_none(backend: store.Backend) -> None:
    store.put("a", 1)
    with store.batch() as b:
        b.put("b", 2)
        b.delete("a")
        b.put("1", {"x": 1}, collection="jobs")
        assert store.get("b") is None  # not before the block ends
    assert store.get("a") is None and store.get("b") == 2 and store.collection("jobs").get("1") == {"x": 1}
    with pytest.raises(RuntimeError), store.batch() as b:
        b.put("c", 3)
        raise RuntimeError("the agent changed its mind")
    assert store.get("c") is None


async def test_every_call_has_an_async_twin(backend: store.Backend) -> None:
    await store.aput("k", [1, 2])
    async with store.batch() as b:
        b.put("j", True)
    assert await store.aget("k") == [1, 2] and await store.aget("j") is True
    assert await store.alist() == [("j", True), ("k", [1, 2])]
    assert await store.aquery(where={}) == [("j", True), ("k", [1, 2])]
    await store.adelete("k")
    assert await store.aget("k") is None


def test_a_value_that_is_not_json_is_refused(backend: store.Backend) -> None:
    with pytest.raises(ValueError, match="JSON values"):
        store.put("when", datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="1 to 1024"):
        store.put("", 1)


def test_the_store_used_before_it_is_configured_is_refused(production: None) -> None:
    with pytest.raises(store.NotConfigured, match="configure"):
        store.Store().get("k")


def test_two_processes_share_one_sqlite_file(tmp_path: Path, production: None) -> None:
    path = tmp_path / "shared.db"
    store.configure(store.SqliteBackend(path))
    store.put("from/test", 1)
    other = (
        "from minutehand_agent import store\n"
        f"store.configure(store.SqliteBackend({str(path)!r}))\n"
        "store.put('from/child', store.get('from/test') + 1)\n"
    )
    subprocess.run(
        [sys.executable, "-c", other], check=True, env={"PATH": "", "PYTHONPATH": str(PACKAGE / "src")}, timeout=30
    )
    assert store.list("from/") == [("from/child", 2), ("from/test", 1)]


def test_wake_does_nothing_in_production(production: None) -> None:
    wake.at(T0)
    wake.clear()
    with pytest.raises(ValueError, match="timezone"):
        wake.at(datetime(2026, 9, 1, 9, 0))


def test_production_records_nothing_and_never_calls_minutehand(
    backend: store.Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With MINUTEHAND_ON unset not one call reaches the wire, whatever MINUTEHAND_AGENT_URL says."""
    monkeypatch.setenv(_wire.URL, "http://127.0.0.1:9/minutehand/agent")
    called: list[str] = []
    monkeypatch.setattr(_wire, "call", lambda path, body: called.append(path))
    store.put("k", 1)
    store.get("k")
    store.list()
    with store.batch() as b:
        b.delete("k")
    wake.at(T0)
    assert called == []


# -- under Minutehand ------------------------------------------------------------------------------------------------


_receivers: list[Receiver] = []


@pytest.fixture
async def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SqliteStore]:
    """A run's store, its receiver listening, and this process told it is played by Minutehand."""
    clock = RunClock(T0)
    world = SqliteStore(tmp_path / "world.db", "run", clock)
    async with Receiver(world, clock) as receiver:
        _receivers.append(receiver)
        monkeypatch.setenv(_wire.ON, "1")
        monkeypatch.setenv(_wire.URL, f"http://127.0.0.1:{receiver.port}{AGENT_PATH}")
        clock.begin_wake()
        yield world
        _receivers.remove(receiver)
    world.close()


async def test_under_minutehand_writes_are_the_runs_and_the_agents_own_database_is_never_opened(
    run: SqliteStore, tmp_path: Path
) -> None:
    own = tmp_path / "production.db"
    store.configure(store.SqliteBackend(own))
    await store.aput("asks/sam", {"status": "asked"})
    async with store.batch() as b:
        b.put("asks/rosa", {"status": "confirmed"})
        b.put("1", {"kind": "wake"}, collection="jobs")
    assert await store.aget("asks/sam") == {"status": "asked"}
    assert await store.aquery("asks/", where={"status": "confirmed"}) == [("asks/rosa", {"status": "confirmed"})]
    await store.adelete("asks/sam")
    assert await store.alist("asks/") == [("asks/rosa", {"status": "confirmed"})]
    assert not own.exists(), "the agent's own database was opened under Minutehand"

    events = [e for e in run.events() if e.entity.provider == MEMORY_PROVIDER]
    assert all(e.actor is Actor.AGENT and e.wake == 1 and e.sim_time == T0 for e in events)
    assert [(e.operation, e.entity.external_id) for e in events] == [
        (Operation.CREATE, "default/asks/sam"),
        (Operation.CREATE, "default/asks/rosa"),
        (Operation.CREATE, "jobs/1"),
        (Operation.READ, "default/asks/sam"),
        (Operation.SEARCH, "default/asks/"),
        (Operation.DELETE, "default/asks/sam"),
        (Operation.SEARCH, "default/asks/"),
    ]
    assert events[1].seq + 1 == events[2].seq, "a batch is written as one, nothing between its writes"
    assert memory_of(run.events()) == {
        ("default", "asks/rosa"): '{"status":"confirmed"}',
        ("jobs", "1"): '{"kind":"wake"}',
    }


async def test_under_minutehand_a_read_is_answered_from_the_run_as_the_clock_moves_on(run: SqliteStore) -> None:
    await store.aput("count", 1)
    after = run.head()
    await store.aput("count", 2)
    held = run.fork("fork", at_seq=after, clock=RunClock(T0))
    assert memory_of(held.events()) == {("default", "count"): "1"}
    assert memory_of(run.events()) == {("default", "count"): "2"}


async def test_under_minutehand_a_wake_mark_is_recorded_as_the_agents_next_wake(run: SqliteStore) -> None:
    at = T0 + timedelta(days=2)
    await asyncio.to_thread(wake.at, at)
    await asyncio.to_thread(wake.clear)
    marks = [e for e in run.events() if e.entity.kind is EntityKind.NEXT_WAKE]
    assert [m.after for m in marks] == [NextWakeSnapshot(at=at), NextWakeSnapshot(at=None)]
    assert all(m.actor is Actor.AGENT for m in marks)


async def test_minutehand_unreachable_while_it_is_on_is_refused_and_nothing_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = tmp_path / "production.db"
    store.configure(store.SqliteBackend(own))
    monkeypatch.setenv(_wire.ON, "1")
    monkeypatch.setenv(_wire.URL, "http://127.0.0.1:9/minutehand/agent")  # the discard port: nothing listens
    monkeypatch.setattr(_wire, "FIRST_WAIT", 0.01)
    with pytest.raises(store.MinutehandUnreachable, match="never falls back"):
        await store.aput("k", 1)
    assert not own.exists()
    monkeypatch.delenv(_wire.URL)
    with pytest.raises(store.MinutehandUnreachable, match="MINUTEHAND_AGENT_URL is not"):
        store.get("k")


async def test_a_call_minutehand_cannot_read_is_refused_saying_why(run: SqliteStore) -> None:
    with pytest.raises(store.MinutehandRefused, match="400"):
        await asyncio.to_thread(_wire.call, "store", {"op": "put", "key": "k"})


def test_the_agent_package_imports_nothing_but_the_standard_library() -> None:
    """What a production agent pays for the import: nothing but the standard library and the package itself. Run
    isolated (`-I -S`: no site-packages, no user path, no PYTHONPATH) with only the package's own source on the path,
    so a third-party import, or one of `minutehand`'s, fails rather than being found in this checkout's environment."""
    probe = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(PACKAGE / 'src')!r})\n"
        "before = set(sys.modules)\n"
        "from minutehand_agent import store, wake\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))\n"
    )
    done = subprocess.run([sys.executable, "-I", "-S", "-c", probe], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    loaded = json.loads(done.stdout)
    standard = set(sys.stdlib_module_names)
    foreign = [m for m in loaded if m.split(".")[0] not in standard and m.split(".")[0] != "minutehand_agent"]
    assert foreign == [], foreign
    assert "minutehand_agent" in loaded and "asyncio" not in loaded and "sqlite3" in loaded


def test_the_agent_distribution_declares_no_dependency() -> None:
    """`pip install minutehand-agent` brings nothing else into the agent's environment."""
    declared = tomllib.loads((PACKAGE / "pyproject.toml").read_text())["project"]
    assert declared["name"] == "minutehand-agent" and declared["dependencies"] == []


async def test_a_held_receiver_answers_the_agent_only_from_the_run_mounted_next(run: SqliteStore) -> None:
    """A fork's agent may call before the fork exists: held, its call is answered from the fork, not the parent."""
    await store.aput("count", 1)
    child = run.fork("child", at_seq=0, clock=RunClock(T0))
    held = [r for r in _receivers if r.store is run]
    assert held, "the fixture's receiver is registered"
    held[0].hold()
    reading = asyncio.ensure_future(store.aget("count"))
    await asyncio.sleep(0.2)
    assert not reading.done(), "the call waits while the receiver is held"
    held[0].mount(child, RunClock(T0))
    assert await reading is None, "answered from the fork, which has none of the parent's memory"
