"""An agent whose own scheduler polls its records for what has come due, and keeps a projection of them in its memory
(`AgentUnderTest.due`): the run reads them as they stand at every decision of its next jump.

The sandbox here is the agent's: every task blocked, its clock the run's, and inside it the agent's own poller, which
each time the clock moves reads its job table and runs every open job whose run-at has passed, as `act` says. The
memory is the run's real store; the poller writes to it as the agent's store would, through the receiver's own code.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from minutehand.adapters.providers.slack.provider import SlackProvider, build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.checkpoint import checkpoints
from minutehand.application.dues import due_entries
from minutehand.application.memory import Versions, held, remember, value_of
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.replier import PeopleReplier
from minutehand.application.restore import Played
from minutehand.application.rewind import fork_run
from minutehand.application.run_clock import RunClock
from minutehand.application.sandbox import SandboxClock, SandboxReading
from minutehand.checks.due_records import DueRecordsCheck
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.checks import FindingKind, RunView
from minutehand.domain.clock import DueSource
from minutehand.domain.experiment import Fork
from minutehand.domain.memory import DueRecords, MemoryPut, MemoryWrite, SeededMemory, instant
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import ProviderKey, Scenario, Silent
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp, HeldCalls
from minutehand.ports.store import Store
from tests.providers.slack.slack_workspace import SCENARIO as WORKSPACE
from tests.support.scenarios import SANDBOX

SECOND = 1_000_000_000
T0 = WORKSPACE.starts_at
HOUR = timedelta(hours=1)
JOBS = DueRecords(collection="due", prefix="jobs/", where={"state": "open"}, field="run_at")
AGENT = AgentUnderTest(name="agent", sandbox=SANDBOX, due=[JOBS])

Act = Callable[[str, dict[str, JsonValue], datetime], list[MemoryPut]]
"""What the agent's poller does with one job that has come due: the writes it mirrors into its memory."""


def close(key: str, job: dict[str, JsonValue], now: datetime) -> list[MemoryPut]:
    """The job done: closed, as a working agent leaves it."""
    del now
    return [MemoryPut(collection="due", key=key, value={**job, "state": "done"})]


def job(at: timedelta | None, state: str = "open") -> dict[str, JsonValue]:
    return {"state": state, "run_at": None if at is None else (T0 + at).isoformat()}


def scenario(jobs: dict[str, dict[str, JsonValue]]) -> Scenario:
    """No goal, nobody answers, and the agent's job table as its memory holds it: only its own work wakes it."""
    return Scenario.model_validate(
        {
            **WORKSPACE.model_dump(exclude={"goal"}),
            "people": [p.model_copy(update={"reply": Silent()}).model_dump() for p in WORKSPACE.people],
            "memory": [SeededMemory(collection="due", key=k, value=v).model_dump() for k, v in jobs.items()],
        }
    )


class _Poller(SandboxClock):
    """The agent's sandbox, and its poller: each time the clock moves, every open job whose run-at has passed is
    run by `act`, and what it writes lands in the run's memory as the agent's. It is also what the run mounts its
    world on (`Mounts`, as the proxy is), so in a fork it writes to the child's memory, never the parent's."""

    def __init__(self, act: Act, *, at: datetime = T0) -> None:
        super().__init__(SANDBOX)
        self._store: Store | None = None
        self._act = act
        self._now_ns = int(at.timestamp()) * SECOND

    def mount(
        self,
        world: Store,
        clock: Clock,
        apps: Mapping[ProviderKey, ASGIApp],
        *,
        scenario: Scenario,
        holds: HeldCalls | None = None,
    ) -> None:
        del clock, apps, scenario, holds
        self._store = world

    def flush(self) -> None:
        return None

    async def read(self) -> SandboxReading:
        return SandboxReading.model_validate_json(json.dumps({"idle": True, "now_ns": self._now_ns}))

    async def advance(self, nanoseconds: int) -> None:
        self._now_ns += nanoseconds
        now = T0 + timedelta(microseconds=(self._now_ns - int(T0.timestamp()) * SECOND) / 1000)
        assert self._store is not None, "the run moved the sandbox's clock before it mounted its world"
        self._poll(self._store, now)

    def _poll(self, store: Store, now: datetime) -> None:
        for key, body in held(store, "due", "jobs/"):
            found = value_of(body)
            assert isinstance(found, dict)
            moment = instant(found["run_at"]) if "run_at" in found else None
            if found.get("state") == "open" and moment is not None and moment <= now:
                writes = self._act(key, found, now)
                if writes:
                    remember(store, MemoryWrite(writes=list(writes)), Versions())


def _services(slack: SlackProvider) -> Services:
    return Services(providers=[slack], pushes={"slack": slack}, schedulers={"slack": slack})


async def _played(
    tmp_path: Path, jobs: dict[str, dict[str, JsonValue]], act: Act = close
) -> tuple[RunRecord, SqliteStore]:
    played = scenario(jobs)
    clock = RunClock(T0)
    store = SqliteStore(tmp_path / "world.db", "parent", clock)
    poller = _Poller(act)
    record = await run_scenario(
        scenario=played,
        agent=AGENT,
        sandbox=poller,
        mounts=poller,
        store=store,
        clock=clock,
        services=_services(build()),
        replier=PeopleReplier(played, None, []),
    )
    return record, store


def _woke_at(record: RunRecord) -> list[timedelta]:
    return [w.sim_time - T0 for w in record.wakes[1:]]


async def test_the_earliest_of_several_declared_records_is_the_next_jump(tmp_path: Path) -> None:
    record, store = await _played(tmp_path, {"jobs/a": job(3 * HOUR), "jobs/b": job(HOUR), "jobs/c": job(2 * HOUR)})
    assert _woke_at(record) == [HOUR, 2 * HOUR, 3 * HOUR]
    fired = [e for e in due_entries(store) if e.source is DueSource.RECORDED]
    assert [(r.key, r.at - T0) for e in fired for r in e.records] == [
        ("jobs/b", HOUR),
        ("jobs/c", 2 * HOUR),
        ("jobs/a", 3 * HOUR),
    ]
    assert record.stop is StopReason.NOTHING_PENDING


def _then(key: str, value: dict[str, JsonValue]) -> Act:
    """Close the job that came due, and change `key` to `value` in the same mirror write."""

    def act(due: str, found: dict[str, JsonValue], now: datetime) -> list[MemoryPut]:
        return [*close(due, found, now), MemoryPut(collection="due", key=key, value=value)]

    return act


async def test_a_record_moved_earlier_after_a_first_read_is_seen_at_the_next_decision(tmp_path: Path) -> None:
    record, _ = await _played(tmp_path, {"jobs/x": job(HOUR), "jobs/a": job(5 * HOUR)}, _then("jobs/a", job(2 * HOUR)))
    assert _woke_at(record) == [HOUR, 2 * HOUR], "the record moved earlier was not followed: an earlier read stood"


async def test_a_record_moved_later_is_not_woken_at_its_old_time(tmp_path: Path) -> None:
    record, _ = await _played(tmp_path, {"jobs/x": job(HOUR), "jobs/a": job(2 * HOUR)}, _then("jobs/a", job(6 * HOUR)))
    assert _woke_at(record) == [HOUR, 6 * HOUR], "the agent was woken at a moment its record no longer held"


async def test_a_record_cleared_or_closed_is_not_woken(tmp_path: Path) -> None:
    def act(due: str, found: dict[str, JsonValue], now: datetime) -> list[MemoryPut]:
        return [
            *close(due, found, now),
            MemoryPut(collection="due", key="jobs/a", value=job(None)),
            MemoryPut(collection="due", key="jobs/b", value=job(3 * HOUR, state="closed")),
        ]

    record, _ = await _played(tmp_path, {"jobs/x": job(HOUR), "jobs/a": job(2 * HOUR), "jobs/b": job(3 * HOUR)}, act)
    assert _woke_at(record) == [HOUR], "a record cleared or closed after it was read still woke the agent"


def _leave(due: str, found: dict[str, JsonValue], now: datetime) -> list[MemoryPut]:
    """A poller whose query and wait disagree: it is woken, finds nothing it would run, and changes nothing."""
    del due, found, now
    return []


async def test_a_past_record_left_unchanged_wakes_once_and_then_is_a_finding(tmp_path: Path) -> None:
    record, store = await _played(tmp_path, {"jobs/stuck": job(-HOUR)}, _leave)
    assert _woke_at(record) == [timedelta(0)], (
        f"the agent was woken {len(record.wakes) - 1} times for one record it left due at one instant"
    )
    assert record.stop is StopReason.NOTHING_PENDING
    view = RunView(
        scenario=scenario({}), events=store.events(), wakes=record.wakes, dues=due_entries(store), due=[JOBS]
    )
    [finding] = DueRecordsCheck().run(view).findings
    assert finding.kind is FindingKind.REVIEW
    assert f"due/jobs/stuck, due at {(T0 - HOUR).isoformat()}, and left it due" in finding.message


async def test_a_record_with_no_offset_is_not_due_and_is_a_finding(tmp_path: Path) -> None:
    record, store = await _played(tmp_path, {"jobs/naive": {"state": "open", "run_at": "2026-08-24T12:00:00"}})
    assert _woke_at(record) == []
    view = RunView(
        scenario=scenario({}), events=store.events(), wakes=record.wakes, dues=due_entries(store), due=[JOBS]
    )
    [finding] = DueRecordsCheck().run(view).findings
    assert 'due/jobs/naive holds run_at = "2026-08-24T12:00:00", which is not an ISO 8601 instant' in finding.message


async def test_the_wake_is_the_declared_poll_after_the_record_falls_due(tmp_path: Path) -> None:
    played = scenario({"jobs/a": job(HOUR)})
    polled = AGENT.model_copy(update={"due": [JOBS.model_copy(update={"poll": timedelta(seconds=30)})]})
    clock = RunClock(T0)
    store = SqliteStore(tmp_path / "world.db", "parent", clock)
    poller = _Poller(close)
    record = await run_scenario(
        scenario=played,
        agent=polled,
        sandbox=poller,
        mounts=poller,
        store=store,
        clock=clock,
        services=_services(build()),
        replier=PeopleReplier(played, None, []),
    )
    assert _woke_at(record) == [HOUR + timedelta(seconds=30)]


class _Program:
    """`OwnProgram` for an agent whose sandbox is `_Poller`: there is no process to start or stop."""

    command: Sequence[str] = ("agent",)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _NothingToPlay:
    """`Playback` of an agent that made no call: its poller's work is in the memory the fork shares."""

    async def play(self, progress: object, *, start: object) -> Played:
        del progress, start
        return Played(calls_answered=0, deliveries_resent=0, jumps=0)


async def test_a_fork_goes_on_from_the_checkpoints_due_records(tmp_path: Path) -> None:
    jobs = {"jobs/stuck": job(-HOUR), "jobs/a": job(HOUR), "jobs/b": job(2 * HOUR)}
    parent, store = await _played(tmp_path, jobs, lambda k, f, n: [] if k == "jobs/stuck" else close(k, f, n))
    assert _woke_at(parent) == [timedelta(0), HOUR, 2 * HOUR]
    after_a = next(w.index for w in parent.wakes if w.sim_time == T0 + HOUR)
    at_seq = max(seq for seq, c in checkpoints(store).items() if c.wake == after_a)
    store.close()

    def open_parent(clock: Clock) -> SqliteStore:
        return SqliteStore(tmp_path / "world.db", "parent", clock)

    def act(key: str, found: dict[str, JsonValue], now: datetime) -> list[MemoryPut]:
        return [] if key == "jobs/stuck" else close(key, found, now)

    played = scenario(jobs)
    sandbox = _Poller(act, at=checkpoints(open_parent(RunClock(T0)))[at_seq].now)  # played back to the checkpoint
    [child] = await fork_run(
        fork=Fork(parent_run="parent", at_seq=at_seq),
        parent=parent,
        open_parent=open_parent,
        run_id="child",
        scenario=played,
        agent=AGENT,
        sandbox=sandbox,
        mounts=sandbox,
        playback_for=lambda _store, _checkpoint: _NothingToPlay(),
        services=_services(build()),
        replier_for=lambda s, _pins: PeopleReplier(s, None, []),
        state_dir=tmp_path / "state",
        own=_Program(),
    )
    woke = [w.sim_time - T0 for w in child.wakes if w.index > after_a]
    assert woke == [2 * HOUR], (
        f"the fork woke at {woke}: it should go on from the checkpoint's due records, jobs/b at 2 h, and not wake "
        "again for jobs/stuck, which its parent was woken for"
    )


@pytest.mark.parametrize(
    "entry, named",
    [
        ({"collection": "due", "prefix": "jobs/"}, "field"),
        ({"field": "run..at"}, "field"),
        ({"field": "run_at", "where": {"run_at": "x"}}, "also in `where`"),
        ({"field": "run_at", "every": "PT1M"}, "every"),
        ({"field": "run_at", "collection": "has a space"}, "collection"),
        ({"field": "run_at", "poll": "-PT1S"}, "poll"),
        ({"field": "run_at", "later": ["run_at"]}, "`later` field 'run_at' is also `field`"),
        (
            {"field": "run_at", "where": {"state": "open"}, "later": ["state"]},
            "`later` field 'state' is also in `where`",
        ),
    ],
)
def test_a_malformed_due_entry_is_refused(entry: dict[str, object], named: str) -> None:
    with pytest.raises(ValidationError, match=named):
        AgentUnderTest.model_validate({"name": "agent", "sandbox": SANDBOX.model_dump(), "due": [entry]})


def _job_table(key: str, found: dict[str, JsonValue], now: datetime) -> list[MemoryPut]:
    """A job table's poller, iterating every job that has come due and changing each as it runs it, each change
    mirrored with the row's version:

    - `jobs/flaky` fails and backs off, 10 then 20 minutes, and is done on its third attempt;
    - `jobs/daily` recurs a day later, and the mirror write of its previous run-at, from another process that
      committed earlier, arrives after the new one;
    - `jobs/spawner` is done and adds `jobs/child`, due half an hour on."""
    version = found["v"]
    assert isinstance(version, int)
    if key == "jobs/flaky":
        attempts = found["attempts"]
        assert isinstance(attempts, int)
        if attempts + 1 >= 3:
            return [_mirror(key, {**found, "state": "done", "attempts": attempts + 1}, version + 1)]
        backoff = timedelta(minutes=10 * 2**attempts)
        return [_mirror(key, {**found, "attempts": attempts + 1, "run_at": (now + backoff).isoformat()}, version + 1)]
    if key == "jobs/daily":
        moment = instant(found["run_at"])
        assert moment is not None
        later = {**found, "run_at": (moment + timedelta(days=1)).isoformat()}
        return [_mirror(key, later, version + 1), _mirror(key, found, version)]  # the older write arrives last
    if key == "jobs/spawner":
        child = {"state": "open", "v": 1, "run_at": (now + timedelta(minutes=30)).isoformat()}
        return [_mirror(key, {**found, "state": "done"}, version + 1), _mirror("jobs/child", child, 1)]
    return [_mirror(key, {**found, "state": "done"}, version + 1)]


def _mirror(key: str, value: dict[str, JsonValue], version: int) -> MemoryPut:
    return MemoryPut(collection="due", key=key, value={**value, "v": version}, version=version)


async def test_records_iterated_and_changed_as_they_run_are_followed_pass_after_pass(tmp_path: Path) -> None:
    jobs = {
        "jobs/flaky": {**job(HOUR), "attempts": 0, "v": 1},
        "jobs/daily": {**job(2 * HOUR), "v": 1},
        "jobs/spawner": {**job(3 * HOUR), "v": 1},
    }
    played = scenario(jobs).model_copy(update={"runs_for": timedelta(days=2)})
    clock = RunClock(T0)
    store = SqliteStore(tmp_path / "world.db", "parent", clock)
    poller = _Poller(_job_table)
    record = await run_scenario(
        scenario=played,
        agent=AGENT,
        sandbox=poller,
        mounts=poller,
        store=store,
        clock=clock,
        services=_services(build()),
        replier=PeopleReplier(played, None, []),
    )
    minutes = timedelta(minutes=1)
    assert _woke_at(record) == [
        HOUR,  # flaky, first attempt
        HOUR + 10 * minutes,  # flaky again, after its backoff
        HOUR + 30 * minutes,  # flaky's third attempt: done
        2 * HOUR,  # daily
        3 * HOUR,  # spawner, which adds child
        3 * HOUR + 30 * minutes,  # child
        26 * HOUR,  # daily a day on: the older mirror write that arrived last did not move it back
    ]
    fired = [(r.key, r.at - T0) for e in due_entries(store) if e.source is DueSource.RECORDED for r in e.records]
    assert ("jobs/daily", 26 * HOUR) in fired and ("jobs/child", 3 * HOUR + 30 * minutes) in fired
    assert record.stop is StopReason.WINDOW_ENDED  # daily's next run, at 50 h, is past the two-day window


Step = Callable[[Store, datetime], None]
"""The agent's own pollers, run each time its sandbox's clock moves: what they read and write in its memory."""

T = T0 + HOUR
P = timedelta(seconds=30)


class _Pollers(_Poller):
    """The agent's sandbox, running its own pollers (`step`) each time the clock moves, and at no other time."""

    def __init__(self, step: Step, *, at: datetime = T0) -> None:
        super().__init__(close, at=at)
        self._step = step

    def _poll(self, store: Store, now: datetime) -> None:
        self._step(store, now)


def _value(store: Store, key: str) -> dict[str, JsonValue]:
    [found] = [value_of(body) for k, body in held(store, "due", key) if k == key]
    assert isinstance(found, dict)
    return found


def _put(store: Store, key: str, value: dict[str, JsonValue]) -> None:
    remember(store, MemoryWrite(writes=[MemoryPut(collection="due", key=key, value=value)]), Versions())


def _passed(found: dict[str, JsonValue], field: str, now: datetime) -> bool:
    moment = instant(found[field]) if field in found else None
    return moment is not None and moment <= now


async def _with(
    tmp_path: Path, memory: dict[str, dict[str, JsonValue]], due: list[DueRecords], step: Step
) -> tuple[RunRecord, SqliteStore]:
    played = scenario(memory)
    clock = RunClock(T0)
    store = SqliteStore(tmp_path / "world.db", "parent", clock)
    sandbox = _Pollers(step)
    record = await run_scenario(
        scenario=played,
        agent=AGENT.model_copy(update={"due": due}),
        sandbox=sandbox,
        mounts=sandbox,
        store=store,
        clock=clock,
        services=_services(build()),
        replier=PeopleReplier(played, None, []),
    )
    return record, store


def _woken_by(store: Store) -> list[list[int]]:
    """The declarations each recorded wake was read by, in order."""
    return [[r.source for r in e.records] for e in due_entries(store) if e.source is DueSource.RECORDED and e.closed]


def _findings(store: Store, record: RunRecord, due: list[DueRecords]) -> list[str]:
    view = RunView(scenario=scenario({}), events=store.events(), wakes=record.wakes, dues=due_entries(store), due=due)
    return [f.message for f in DueRecordsCheck().run(view).findings]


async def test_a_wake_for_one_declaration_is_not_a_wake_for_another_holding_the_same_instant(tmp_path: Path) -> None:
    """The worked example: a sweep takes the record once `b` has passed and `a` has not; woken for `a` alone, the
    agent moves `a` a day on, and at the next decision the sweep's declaration is due."""
    due = [
        DueRecords(collection="due", field="a", poll=P),
        DueRecords(collection="due", field="b", later=["a"], poll=P),
    ]

    def step(store: Store, now: datetime) -> None:
        found = _value(store, "rec")
        if _passed(found, "b", now) and not _passed(found, "a", now):
            _put(store, "rec", {**found, "b": None})  # the sweep
        elif _passed(found, "a", now):
            moved = (T + timedelta(days=1)).isoformat() if found["a"] == T.isoformat() else None
            _put(store, "rec", {**found, "a": moved})

    record, store = await _with(tmp_path, {"rec": {"a": T.isoformat(), "b": T.isoformat()}}, due, step)
    assert _woke_at(record) == [HOUR + P, HOUR + 2 * P, HOUR + timedelta(days=1) + P]
    assert _woken_by(store) == [[0], [1], [0]], "the wake for `a` was taken as a wake for the sweep's `b`"
    assert _value(store, "rec")["b"] is None, "the sweep did not run in the wake for `b`"
    assert _findings(store, record, due) == []


@pytest.mark.parametrize(
    "expires, woke",
    [
        ({"expiresAt": (T - HOUR).isoformat()}, []),
        ({"expiresAt": (T + timedelta(days=1)).isoformat()}, [HOUR + P]),
        ({"expiresAt": None}, [HOUR + P]),
        ({}, [HOUR + P]),
    ],
    ids=["expired", "unexpired", "null", "absent"],
)
async def test_a_record_is_due_only_while_its_later_fields_have_not_passed(
    tmp_path: Path, expires: dict[str, JsonValue], woke: list[timedelta]
) -> None:
    """A request poller takes a pending request while it has not expired: one that expired before it fell due is not
    due, and a `later` field absent or null is due exactly as with no `later`."""
    due = [DueRecords(collection="due", where={"status": "pending"}, field="updatedAt", later=["expiresAt"], poll=P)]

    def step(store: Store, now: datetime) -> None:
        found = _value(store, "req")
        expiry = instant(found["expiresAt"]) if "expiresAt" in found else None
        if found["status"] == "pending" and _passed(found, "updatedAt", now) and (expiry is None or expiry > now):
            _put(store, "req", {**found, "status": "answered"})

    record, store = await _with(
        tmp_path, {"req": {"status": "pending", "updatedAt": T.isoformat(), **expires}}, due, step
    )
    assert _woke_at(record) == woke
    assert record.stop is StopReason.NOTHING_PENDING
    assert _findings(store, record, due) == []


async def test_two_declarations_holding_one_instant_on_one_record_are_each_woken_for_once(tmp_path: Path) -> None:
    due = [DueRecords(collection="due", field="a"), DueRecords(collection="due", field="b", poll=HOUR)]
    record, store = await _with(tmp_path, {"rec": {"a": T.isoformat(), "b": T.isoformat()}}, due, lambda s, n: None)
    assert _woke_at(record) == [HOUR, 2 * HOUR]
    assert _woken_by(store) == [[0], [1]]
    left = _findings(store, record, due)
    assert len(left) == 2 and all("due/rec, due at" in m and "left it due" in m for m in left)


async def test_a_backlog_taken_one_record_per_poll_is_woken_for_until_it_is_drained(tmp_path: Path) -> None:
    """Each poll the agent takes only the oldest open task of each owner; two due at one instant are taken on
    consecutive polls, and nothing of the second changes in between: no clock in the sandbox moves inside a wake."""
    record, store = await _with(tmp_path, _tasks(1, 2), [TASKS], _oldest_per_owner(lambda key: True))
    assert _woke_at(record) == [HOUR + P, HOUR + 2 * P], "the second task, left due by the first poll, was not polled"
    assert all(_value(store, key)["state"] == "done" for key in _tasks(1, 2))
    assert _findings(store, record, [TASKS]) == []


async def test_a_record_no_one_ever_takes_is_woken_for_once_and_left_due(tmp_path: Path) -> None:
    record, store = await _with(tmp_path, _tasks(1), [TASKS], _oldest_per_owner(lambda key: False))
    assert _woke_at(record) == [HOUR + P], "a record nobody takes was woken for again"
    [left] = _findings(store, record, [TASKS])
    assert "due/tasks/1, due at" in left and "left it due" in left


async def test_a_backlog_record_no_one_takes_is_woken_for_again_once_and_left_due(tmp_path: Path) -> None:
    """The first task is taken, so the second is polled again; nobody takes it, and that wake took nothing."""
    record, store = await _with(tmp_path, _tasks(1, 2), [TASKS], _oldest_per_owner(lambda key: key == "tasks/1"))
    assert _woke_at(record) == [HOUR + P, HOUR + 2 * P]
    [left] = _findings(store, record, [TASKS])
    assert "due/tasks/2, due at" in left
    assert _value(store, "tasks/2")["state"] == "open"


async def test_a_record_first_due_after_its_moment_is_woken_for_a_poll_on_and_its_poller_runs(tmp_path: Path) -> None:
    """Woken for one job, the agent adds another already past: its own timer-driven poller finds it only when its
    clock next moves, so the wake for it is a poll after the decision that first reads it, never at it."""
    due = [DueRecords(collection="due", prefix="jobs/", field="run_at", poll=P)]

    def step(store: Store, now: datetime) -> None:
        jobs = {key: value_of(body) for key, body in held(store, "due", "jobs/")}
        for key, found in jobs.items():
            assert isinstance(found, dict)
            if _passed(found, "run_at", now):
                _put(store, key, {"run_at": None, "ran_at": now.isoformat()})
                if key == "jobs/x":
                    _put(store, "jobs/late", {"run_at": (T - HOUR).isoformat()})

    record, store = await _with(tmp_path, {"jobs/x": {"run_at": T.isoformat()}}, due, step)
    assert _woke_at(record) == [HOUR + P, HOUR + 2 * P], "the wake for the late job moved no clock"
    assert _value(store, "jobs/late")["ran_at"] == (T + 2 * P).isoformat(), "its own poller did not run in its wake"
    assert _findings(store, record, due) == []


TASKS = DueRecords(collection="due", prefix="tasks/", where={"state": "open"}, field="due_at", poll=P)


def _tasks(*numbers: int) -> dict[str, dict[str, JsonValue]]:
    return {f"tasks/{n}": {"owner": "o", "state": "open", "due_at": T.isoformat()} for n in numbers}


def _oldest_per_owner(takes: Callable[[str], bool]) -> Step:
    """A poller that takes only the oldest open task of each owner each time it polls, if `takes` says it can."""

    def step(store: Store, now: datetime) -> None:
        owners: set[JsonValue] = set()
        for key, body in held(store, "due", "tasks/"):
            found = value_of(body)
            assert isinstance(found, dict)
            if found["state"] == "open" and _passed(found, "due_at", now) and found["owner"] not in owners:
                owners.add(found["owner"])
                if takes(key):
                    _put(store, key, {**found, "state": "done"})

    return step


async def test_a_record_whose_later_field_passed_in_its_wake_is_not_left_due(tmp_path: Path) -> None:
    """Woken for a request, the agent expires it and leaves its due time: no longer due by its declaration, read at
    the instant it held, it is not left due."""
    due = [DueRecords(collection="due", field="updatedAt", later=["expiresAt"], poll=P)]

    def step(store: Store, now: datetime) -> None:
        found = _value(store, "req")
        if _passed(found, "updatedAt", now) and found["expiresAt"] != (T - HOUR).isoformat():
            _put(store, "req", {**found, "expiresAt": (T - HOUR).isoformat()})

    record, store = await _with(
        tmp_path, {"req": {"updatedAt": T.isoformat(), "expiresAt": (T + timedelta(days=1)).isoformat()}}, due, step
    )
    assert _woke_at(record) == [HOUR + P]
    assert _findings(store, record, due) == []


async def test_a_later_field_that_is_not_an_instant_is_not_due_and_is_a_finding(tmp_path: Path) -> None:
    due = [DueRecords(collection="due", field="updatedAt", later=["expiresAt"])]
    record, store = await _with(
        tmp_path, {"req": {"updatedAt": T.isoformat(), "expiresAt": "2026-08-25"}}, due, lambda s, n: None
    )
    assert _woke_at(record) == []
    [finding] = _findings(store, record, due)
    assert 'due/req holds expiresAt = "2026-08-25", which is not an ISO 8601 instant' in finding


async def test_a_fork_after_a_wake_that_took_part_of_a_backlog_polls_for_the_rest(tmp_path: Path) -> None:
    """What the parent's wake took is read from the log and the memory as it left that wake."""
    step = _oldest_per_owner(lambda key: True)
    parent, store = await _with(tmp_path, _tasks(1, 2), [TASKS], step)
    first = next(w.index for w in parent.wakes if w.sim_time == T + P)
    at_seq = max(seq for seq, c in checkpoints(store).items() if c.wake == first)
    store.close()

    def open_parent(clock: Clock) -> SqliteStore:
        return SqliteStore(tmp_path / "world.db", "parent", clock)

    played = scenario(_tasks(1, 2))
    agent = AGENT.model_copy(update={"due": [TASKS]})
    sandbox = _Pollers(step, at=checkpoints(open_parent(RunClock(T0)))[at_seq].now)
    [child] = await fork_run(
        fork=Fork(parent_run="parent", at_seq=at_seq),
        parent=parent,
        open_parent=open_parent,
        run_id="child",
        scenario=played,
        agent=agent,
        sandbox=sandbox,
        mounts=sandbox,
        playback_for=lambda _store, _checkpoint: _NothingToPlay(),
        services=_services(build()),
        replier_for=lambda s, _pins: PeopleReplier(s, None, []),
        state_dir=tmp_path / "state",
        own=_Program(),
    )
    woke = [w.sim_time - T0 for w in child.wakes if w.index > first]
    assert woke == [HOUR + 2 * P], f"the fork woke at {woke}: the second task, left by a wake that took the first"


async def test_a_backlog_record_moved_later_counts_as_taken(tmp_path: Path) -> None:
    """The poller defers the oldest task a day instead of closing it: it still matches, at a later instant, and the
    next is polled for."""

    def step(store: Store, now: datetime) -> None:
        for key, body in held(store, "due", "tasks/"):
            found = value_of(body)
            assert isinstance(found, dict)
            if found["state"] == "open" and _passed(found, "due_at", now):
                deferred = key == "tasks/1" and found["due_at"] == T.isoformat()
                later = {**found, "due_at": (T + timedelta(days=1)).isoformat()}
                _put(store, key, later if deferred else {**found, "state": "done"})
                return

    record, store = await _with(tmp_path, _tasks(1, 2), [TASKS], step)
    assert _woke_at(record) == [HOUR + P, HOUR + 2 * P, HOUR + timedelta(days=1) + P]
    assert _findings(store, record, [TASKS]) == []
