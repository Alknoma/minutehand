"""The composition root: a scenario played end to end against a real agent, and a finished run forked.

The one module that imports both `application` and `adapters`. Everything a run leaves behind is in its
directory under the state directory, written by the models, so a later process can read it back:

    <state>/ca/                          the proxy's CA, made on first use and reused
    <state>/runs/<run_id>/world.db       the world, for a run started from the beginning; a fork writes
                                         into its root run's file
    <state>/runs/<run_id>/record.json    `RunRecord`
    <state>/runs/<run_id>/result.json    `RunResult`: findings, blocked checks, notes, the scorecard
    <state>/runs/<run_id>/scenario.json  the scenario as this run played it (a fork's, with its changes)
    <state>/runs/<run_id>/agent.json     `AgentUnderTest`
    <state>/runs/<run_id>/agent.log      what the agent's own process printed, when Minutehand started it
    <state>/runs/<run_id>/wake-<n>/      the agent's snapshot after wake n, when it declares `StateHooks` and
                                         settled in time
    <state>/runs/<run_id>/restore.json   for a fork, or a sample after the first: every step of the restore that
                                         started it, with each command's output, and whether it was verified

One proxy per process: mitmproxy keeps its master in a module global, so `play` and `fork` each start one
and move it from sample to sample with `Proxy.mount`, and never two at once.

A provider signs the events it pushes with the secret its `InboundTarget.secret` resolves to for the run:
one generated per run and handed to the agent's command, or the agent's own, read from a variable of this
process. The secret is given to the provider with each push; it is never set in this process's environment.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field

from minutehand.adapters.agent.reach import reach_for
from minutehand.adapters.model.openai_compatible import API_KEY_VARIABLE, BASE_URL_VARIABLE, MODEL_VARIABLE
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.proxy.trust import write_bundle
from minutehand.adapters.store.sqlite import SCHEMA_VERSION, SqliteStore
from minutehand.application.checkpoint import (
    CHECKPOINT,
    AgentState,
    NoHooks,
    NotRestorable,
    checkpoints,
    read_checkpoint,
)
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.refusals import RunRefused
from minutehand.application.replier_model import PeopleReplier
from minutehand.application.restore import Progress, Restored, restore_agent
from minutehand.application.rewind import RESTORE_RECORD, changed_scenario, fork_run
from minutehand.application.run_clock import RunClock
from minutehand.application.state_hooks import wake_dir
from minutehand.checks.runner import RunResult, evaluate, evaluate_judged, view_of
from minutehand.domain.agent import AgentUnderTest, Booked, GoalByMessage, Polled, Reported
from minutehand.domain.checks import WakeRecord
from minutehand.domain.experiment import Fork
from minutehand.domain.people import GeneratedSecret, SecretFromEnvironment
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import Answers, Model, ProviderKey, Scenario, WrittenScenario
from minutehand.domain.world import Actor, Operation
from minutehand.ports.agent import Reports
from minutehand.ports.clock import Clock
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.provider import BooksWakes, EditsTickets, HoldsTickets, Provider, PushesEvents
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry

RUNS = "runs"
WORLD = "world.db"
RECORD = "record.json"
RESULT = "result.json"
SCENARIO = "scenario.json"
AGENT = "agent.json"
AGENT_LOG = "agent.log"

CA_VARIABLES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "HTTPLIB2_CA_CERTS", "AWS_CA_BUNDLE")
"""Each HTTP library's own name for the file of CAs it trusts."""

LISTEN_TIMEOUT = 30.0
"""Seconds the agent's process has to accept connections on its wake or inbound URL."""

STOP_TIMEOUT = 5.0


class AgentExited(RunRefused):
    """The agent's process, started by Minutehand, exited before the run was over."""


class Outcome(Model):
    """One finished run: what happened, and what the checks said about it."""

    record: RunRecord
    result: RunResult


class ForkPoint(Model):
    """Where a fork may be taken: the end of a wake, as a seq in the world's log, and whether the agent's own
    state there can be put back."""

    wake: int
    seq: int
    agent: AgentState


def run_dir(state: Path, run_id: str) -> Path:
    return state / RUNS / run_id


# -- playing a scenario ---------------------------------------------------------------------------------------


async def play(
    written: Scenario | WrittenScenario,
    agent: AgentUnderTest,
    *,
    state: Path,
    samples: int = 1,
    command: Sequence[str] | None = None,
    telemetry: Telemetry | None = None,
    model: LanguageModel | None = None,
    judge: bool = False,
    listen: Listen | None = None,
    progress: Progress | None = None,
) -> list[Outcome]:
    """Run the scenario `samples` times from its start, each a run of its own, through one proxy.

    `model` writes the replies of `Answers` people and, with `judge`, runs the judged checks; a scenario
    with an `Answers` person and no model is refused before anything starts.

    `command`, when given, is the agent's own program: started before each run with only the proxy, its
    CA and the run's signing secrets added to this process's environment, waited for until it accepts
    connections, and stopped when the run ends.

    Each sample after the first starts from the agent's own state as the first found it, restored and
    verified through its `StateHooks` as a fork's is (`progress` hears each step). Without hooks the agent
    carries what it remembers from one sample into the next, and the samples are not independent.

    A scenario with no `starts_at` starts now: the instant is taken once, here, and every sample plays and
    records it, so a fork of any of them starts from the same moment.
    """
    if samples < 1:
        raise RunRefused(f"a run needs at least one sample, not {samples}")
    scenario = written.starting(_now())
    _refuse_unwritten(scenario, model)
    registry = Registry.installed()
    routing = Routing(registry)
    services = _services(scenario, agent, registry)
    outcomes: list[Outcome] = []
    first = _open(state, _new_run_id(), scenario)
    listen = listen or Listen()
    async with _proxy(routing, first[0], first[1], state, listen) as proxy:
        for sample in range(samples):
            store, clock = first if sample == 0 else _open(state, _new_run_id(), scenario)
            directory = run_dir(state, store.run_id)
            _write_inputs(directory, scenario, agent)
            scorer = _Judge(scenario, model if judge else None, judging=judge)
            signing = signing_for(agent)
            env = agent_environment(listen, proxy.port, proxy.ca_bundle, signing.for_agent)
            reach = reach_for(agent, env=env)
            async with _agent_process(command, env, agent, directory / AGENT_LOG) as own:
                if sample > 0 and agent.state is not None:
                    await _restore_start(state, first[0], agent, reach.main, own, directory, progress)
                record = await run_scenario(
                    scenario=scenario,
                    agent=agent,
                    reach=reach,
                    store=store,
                    clock=clock,
                    services=services,
                    replier=PeopleReplier(scenario, model),
                    telemetry=telemetry,
                    mounts=proxy,
                    scorer=scorer,
                    state_dir=state / RUNS,
                    signing=signing.by_provider,
                    traffic=proxy,
                )
            outcomes.append(_keep(directory, record, scorer))
    return outcomes


async def _restore_start(
    state: Path,
    first: Store,
    agent: AgentUnderTest,
    main: object,
    own: _Program | None,
    directory: Path,
    progress: Progress | None,
) -> None:
    """Before a sample after the first: the agent put back as the first sample found it, through the same
    verified restore a fork uses. Refused when the first sample's start was not restorable."""
    assert agent.state is not None
    start = next(iter(checkpoints(first).items()), None)
    if start is None:
        raise RunRefused(f"run {first.run_id} has no checkpoint at its start to restore the next sample from")
    seq, checkpoint = start
    restorable = checkpoint.agent
    if isinstance(restorable, NotRestorable | NoHooks):
        why = restorable.reason if isinstance(restorable, NotRestorable) else "no state hooks"
        raise RunRefused(
            f"the next sample cannot start where run {first.run_id} started: its start is not restorable: {why}"
        )
    restored = await restore_agent(
        agent.state,
        wake_dir(state / RUNS, restorable.snapshot_of, restorable.wake),
        checkpoint_seq=seq,
        recorded=restorable.report,
        reports=main if isinstance(main, Reports) else None,
        own=own,
        progress=progress,
    )
    (directory / RESTORE_RECORD).write_text(restored.model_dump_json(indent=2), encoding="utf-8")


async def fork(
    parent_run: str,
    changes: Fork,
    *,
    state: Path,
    command: Sequence[str] | None = None,
    telemetry: Telemetry | None = None,
    model: LanguageModel | None = None,
    judge: bool = False,
    listen: Listen | None = None,
    progress: Progress | None = None,
) -> list[Outcome]:
    """Rerun a finished run from one of its checkpoints with `changes` applied, once per `Fork.samples`.

    The people, tickets and deadline change in the world; a `PromptPatch` or `ModelSwap` is applied on the
    wire, to the agent's own calls to its model, through the proxy's EDIT policy. The agent's own state is
    restored and verified first (`application.restore`); `progress` hears each step as it is taken.

    A fork that is refused leaves no run behind: no row in the world file and no directory.
    """
    if changes.parent_run != parent_run:
        raise RunRefused(f"the changes are for run {changes.parent_run}, not {parent_run}")
    parent = load(state, parent_run)
    directory = run_dir(state, parent_run)
    scenario = Scenario.model_validate_json((directory / SCENARIO).read_text(encoding="utf-8"))
    agent = AgentUnderTest.model_validate_json((directory / AGENT).read_text(encoding="utf-8"))
    world = _root_dir(state, parent.record) / WORLD
    changed = changed_scenario(scenario, changes)
    _refuse_unwritten(changed, model)
    registry = Registry.installed()
    routing = Routing(registry)
    services = _services(changed, agent, registry)
    child_id = _new_run_id()
    scorer = _Judge(changed, model if judge else None, judging=judge)
    signing = signing_for(agent)

    def open_parent(clock: Clock) -> Store:
        return SqliteStore(world, parent_run, clock)

    holding = RunClock(scenario.starts_at)
    listen = listen or Listen()
    async with _proxy(routing, open_parent(holding), holding, state, listen) as proxy:
        env = agent_environment(listen, proxy.port, proxy.ca_bundle, signing.for_agent)
        log = run_dir(state, child_id) / AGENT_LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with _agent_process(command, env, agent, log) as own:
                records = await fork_run(
                    fork=changes,
                    parent=parent.record,
                    open_parent=open_parent,
                    run_id=child_id,
                    scenario=scenario,
                    agent=agent,
                    reach=reach_for(agent, env=env),
                    services=services,
                    replier_for=lambda s: PeopleReplier(s, model),
                    state_dir=state / RUNS,
                    wire=routing,
                    telemetry=telemetry,
                    mounts=proxy,
                    traffic=proxy,
                    scorer=scorer,
                    signing=signing.by_provider,
                    own=own,
                    progress=progress,
                )
        except RunRefused:
            _remove_refused(state, world, child_id, changes.samples)
            raise
        finally:
            routing.apply(child_id, [])
    outcomes: list[Outcome] = []
    for record in records:
        child = run_dir(state, record.run_id)
        _write_inputs(child, changed, agent)
        outcomes.append(_keep(child, record, scorer))
    return outcomes


def _remove_refused(state: Path, world: Path, child_id: str, samples: int) -> None:
    """The directories a refused fork made, for every child the world file does not hold: a refusal leaves no
    run behind. A sample that finished before a later one was refused keeps its directory."""
    held = {run_id for run_id, _, _ in _ReadOnlyStore.runs_in(world)}
    for run_id in [child_id, *(f"{child_id}-{n + 1}" for n in range(samples))]:
        directory = run_dir(state, run_id)
        if directory.is_dir() and run_id not in held and not (directory / RECORD).is_file():
            shutil.rmtree(directory)


# -- reading runs back ----------------------------------------------------------------------------------------


def load(state: Path, run_id: str) -> Outcome:
    """A finished run as it was persisted."""
    directory = run_dir(state, run_id)
    if not (directory / RECORD).is_file():
        raise RunRefused(f"no finished run {run_id} under {state / RUNS}")
    return Outcome(
        record=RunRecord.model_validate_json((directory / RECORD).read_text(encoding="utf-8")),
        result=RunResult.model_validate_json((directory / RESULT).read_text(encoding="utf-8")),
    )


def runs(state: Path) -> list[Outcome]:
    """Every finished run under `state`, oldest first by the time its record was written."""
    base = state / RUNS
    if not base.is_dir():
        return []
    found = sorted((d for d in base.iterdir() if (d / RECORD).is_file()), key=lambda d: (d / RECORD).stat().st_mtime)
    return [load(state, d.name) for d in found]


def fork_points(state: Path, run_id: str) -> list[ForkPoint]:
    """Every point this run can be forked from, in order."""
    with reading(state, run_id) as world:
        return points_in(world)


def points_in(world: Store) -> list[ForkPoint]:
    """The checkpoints a world's log holds, each a point a fork may be taken from, and whether the agent's own
    state there can be put back."""
    return [ForkPoint(wake=c.wake, seq=seq, agent=c.agent) for seq, c in checkpoints(world).items()]


def restore_of(state: Path, run_id: str) -> Restored | None:
    """How the agent was restored to start this run: a fork, or a sample after the first. None for a run that
    started from the beginning."""
    path = run_dir(state, run_id) / RESTORE_RECORD
    return Restored.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


class Logged(Model):
    """A run as the world file holding it knows it: finished, or still being written by another process."""

    run_id: str
    root: str = Field(description="The run whose directory holds the world file")
    parent_run: str | None
    forked_at: int | None
    finished: bool


def logged(state: Path) -> list[Logged]:
    """Every run any world file under `state` holds, finished or not, root runs in directory order and each
    root's forks after it in the order they were taken."""
    base = state / RUNS
    if not base.is_dir():
        return []
    found: list[Logged] = []
    for directory in sorted(base.iterdir()):
        path = directory / WORLD
        if not path.is_file():
            continue
        for run_id, parent, forked_at in _ReadOnlyStore.runs_in(path):
            finished = (run_dir(state, run_id) / RECORD).is_file()
            found.append(
                Logged(run_id=run_id, root=directory.name, parent_run=parent, forked_at=forked_at, finished=finished)
            )
    return found


def find(state: Path, run_id: str) -> Logged:
    """One run, finished or still being written; refused when no world file holds it."""
    for entry in logged(state):
        if entry.run_id == run_id:
            return entry
    raise RunRefused(f"no run {run_id} under {state / RUNS}")


@contextmanager
def reading(state: Path, run_id: str) -> Iterator[Store]:
    """A run's world opened for reading only, so a run another process is still writing can be read without
    taking its write lock; closed on leaving. A fork is read from its root run's file."""
    entry = find(state, run_id)
    store = _ReadOnlyStore(run_dir(state, entry.root) / WORLD, run_id, RunClock(datetime.fromtimestamp(0, UTC)))
    try:
        yield store
    finally:
        store.close()


def scenario_of(state: Path, run_id: str) -> Scenario:
    """The scenario as this run played it. A fork still running has not written its own yet, so it answers
    the scenario of the run it was forked from, before the fork's changes."""
    entry = find(state, run_id)
    while True:
        path = run_dir(state, entry.run_id) / SCENARIO
        if path.is_file():
            return Scenario.model_validate_json(path.read_text(encoding="utf-8"))
        if entry.parent_run is None:
            raise RunRefused(f"run {run_id} has written no scenario yet")
        entry = find(state, entry.parent_run)


def wakes_of(state: Path, run_id: str, world: Store) -> list[WakeRecord]:
    """The run's wakes: from its record once it has finished, and from the checkpoints in its log while it
    runs. A wake still in progress has no checkpoint and is not listed. Whether a running wake changed the
    agent's commitments is not in the log, so those records say it did not."""
    if (run_dir(state, run_id) / RECORD).is_file():
        return load(state, run_id).record.wakes
    events = world.events()
    changes: dict[int, int] = {}
    for event in events:
        if event.actor is Actor.AGENT and event.operation not in (Operation.READ, Operation.SEARCH):
            changes[event.wake] = changes.get(event.wake, 0) + 1
    wakes: dict[int, WakeRecord] = {}
    for event in events:
        if event.entity == CHECKPOINT and event.wake >= 1 and event.wake not in wakes:
            wakes[event.wake] = WakeRecord(
                index=event.wake,
                sim_time=event.sim_time,
                world_changes=changes.get(event.wake, 0),
                commitments_changed=False,
            )
    return list(wakes.values())


class _ReadOnlyStore(SqliteStore):
    """A world file opened with SQLite's read-only mode: nothing is created, stamped or locked for writing,
    and a run still being written in WAL mode is read as of its last commit. Any write raises."""

    def __init__(self, path: Path, run_id: str, clock: Clock) -> None:
        self.run_id = run_id
        self._path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._db = _read_only(path)
        found = self._db.execute("PRAGMA user_version").fetchone()[0]
        if found != SCHEMA_VERSION:
            self._db.close()
            raise RunRefused(f"{path} was written with store schema {found}; this version reads {SCHEMA_VERSION}")
        self._lineage = self._load_lineage()

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def runs_in(path: Path) -> list[tuple[str, str | None, int | None]]:
        """Each run the file holds, with its parent and the seq it was forked at, in the order written."""
        db = _read_only(path)
        try:
            return [(r[0], r[1], r[2]) for r in db.execute("SELECT run_id, parent, forked_at FROM run ORDER BY rowid")]
        except sqlite3.OperationalError:
            return []  # the writer has created the file and not yet its tables: it holds no run yet
        finally:
            db.close()


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)


# -- the parts of a run ---------------------------------------------------------------------------------------


class _Judge:
    """`application.orchestrator.Scorer`: the run's view built from the world, and every check run over it;
    with `judging`, the judged checks too, by `model` or blocked for want of one."""

    def __init__(self, scenario: Scenario, model: LanguageModel | None, *, judging: bool) -> None:
        self._scenario = scenario
        self._model = model
        self._judging = judging
        self.results: dict[str, RunResult] = {}

    async def score(self, record: RunRecord, world: Store) -> RunResult:
        last = read_checkpoint(world)
        view = view_of(
            self._scenario,
            world.events(),
            record.wakes,
            world.replies(),
            commitments=last.commitments if last is not None else None,
            unmatched_calls=[call.exchange for call in world.calls() if call.provider is None],
        )
        result = await evaluate_judged(view, self._model) if self._judging else evaluate(view)
        self.results[record.run_id] = result
        return result


def _refuse_unwritten(scenario: Scenario, model: LanguageModel | None) -> None:
    """An `Answers` person needs a model to write their replies; without one the run is refused before it starts."""
    written = [p.key for p in scenario.people if isinstance(p.reply, Answers)]
    if written and model is None:
        raise RunRefused(
            f"a model writes the replies of {', '.join(written)} (reply kind 'answers'), and no model is "
            f"configured: set {MODEL_VARIABLE} and {API_KEY_VARIABLE}, and {BASE_URL_VARIABLE} for a service "
            "other than OpenAI's"
        )


def _now() -> datetime:
    """The moment a run starts, to the second, for a scenario that starts when its run does."""
    return datetime.now(UTC).replace(microsecond=0)  # clock-lint: exempt the real start of a run, read once


def _new_run_id() -> str:
    return secrets.token_hex(6)


def _open(state: Path, run_id: str, scenario: Scenario) -> tuple[SqliteStore, RunClock]:
    directory = run_dir(state, run_id)
    directory.mkdir(parents=True, exist_ok=False)
    clock = RunClock(scenario.starts_at)
    return SqliteStore(directory / WORLD, run_id, clock), clock


def _root_dir(state: Path, record: RunRecord) -> Path:
    """The directory whose world file holds this run: its own, or its oldest ancestor's."""
    while record.parent_run is not None:
        record = load(state, record.parent_run).record
    return run_dir(state, record.run_id)


def _write_inputs(directory: Path, scenario: Scenario, agent: AgentUnderTest) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SCENARIO).write_text(scenario.model_dump_json(indent=2), encoding="utf-8")
    (directory / AGENT).write_text(agent.model_dump_json(indent=2), encoding="utf-8")


def _keep(directory: Path, record: RunRecord, judge: _Judge) -> Outcome:
    outcome = Outcome(record=record, result=judge.results[record.run_id])
    (directory / RECORD).write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (directory / RESULT).write_text(outcome.result.model_dump_json(indent=2), encoding="utf-8")
    return outcome


def _services(scenario: Scenario, agent: AgentUnderTest, registry: Registry) -> Services:
    """The providers the scenario and the agent name, each held to the ports its manifest claims.

    A provider named by neither is still answered when the agent calls it, built by the proxy on that
    call, but it is not seeded and no ticket fate or reply can land on it.
    """
    named: set[ProviderKey] = {t.provider for t in scenario.tickets} | {d.provider for d in scenario.documents}
    named |= {t.provider for t in agent.inbound}
    if isinstance(agent.goal, GoalByMessage):
        named.add(agent.goal.provider)
    if any(isinstance(w, Booked) for w in agent.wakes):
        named |= {m.key for m in registry.manifests if m.books_wakes}
    manifests = {m.key: m for m in registry.manifests}
    unknown = sorted(named - set(manifests))
    if unknown:
        raise RunRefused(
            f"no installed provider is named {', '.join(unknown)}; installed: {', '.join(sorted(manifests))}"
        )
    providers: list[Provider] = [registry.provider(manifests[key]) for key in sorted(named)]
    pushes: dict[ProviderKey, PushesEvents] = {}
    tickets: dict[ProviderKey, HoldsTickets] = {}
    editors: dict[ProviderKey, EditsTickets] = {}
    schedulers: dict[ProviderKey, BooksWakes] = {}
    for provider in providers:
        key = provider.manifest.key
        if provider.manifest.pushes_events:
            if not isinstance(provider, PushesEvents):
                raise RunRefused(f"provider {key} declares pushes_events and does not implement PushesEvents")
            pushes[key] = provider
        if provider.manifest.books_wakes:
            if not isinstance(provider, BooksWakes):
                raise RunRefused(f"provider {key} declares books_wakes and does not implement BooksWakes")
            schedulers[key] = provider
        if isinstance(provider, HoldsTickets):
            tickets[key] = provider
        if isinstance(provider, EditsTickets):
            editors[key] = provider
    return Services(providers=providers, pushes=pushes, tickets=tickets, editors=editors, schedulers=schedulers)


@dataclass(frozen=True)
class Signing:
    """The secrets a run signs pushed events with: one per provider, and the ones the agent's command is given."""

    by_provider: dict[ProviderKey, str]
    for_agent: dict[str, str]


def signing_for(agent: AgentUnderTest) -> Signing:
    """Each inbound target's secret for this run: generated and handed to the agent's command, read from this
    process's own variable (the secret an agent already running was configured with), or, when the target
    names none, generated and given to no one. A variable named and not set refuses the run."""
    by_provider: dict[ProviderKey, str] = {}
    for_agent: dict[str, str] = {}
    for target in agent.inbound:
        source = target.secret
        if isinstance(source, SecretFromEnvironment):
            if source.env not in os.environ:
                raise RunRefused(
                    f"the agent's {target.provider} signing secret is read from {source.env}, which is not set "
                    "in Minutehand's environment; set it to the secret the agent was configured with"
                )
            value = os.environ[source.env]
        else:
            value = secrets.token_hex(16)
            if isinstance(source, GeneratedSecret):
                for_agent[source.env] = value
        by_provider.setdefault(target.provider, value)
    return Signing(by_provider=by_provider, for_agent=for_agent)


class Listen(Model):
    """Where the proxy listens, and how the agent reaches it.

    The defaults suit an agent Minutehand starts on this machine: loopback, a port the system picks. An agent in
    containers, or one already running, needs a fixed `port`, a `host` it can reach (`0.0.0.0`), and
    `agent_host`, the name the AGENT uses for this machine, which is not the bind address
    (`host.docker.internal`)."""

    host: str = "127.0.0.1"
    port: int = Field(default=0, ge=0, le=65535, description="0 lets the system pick one for each run")
    agent_host: str | None = Field(
        default=None, description="The host in the proxy URL the agent is given; None is the bind host"
    )
    no_proxy: list[str] = Field(default=[], description="More hosts the agent reaches directly, not through the proxy")

    def proxy_url(self, port: int) -> str:
        """The proxy as the agent reaches it. Binding every interface is not an address: it is reached on loopback."""
        host = self.agent_host or ("127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host)
        return f"http://{host}:{port}"


DIRECT = ("localhost", "127.0.0.1")
"""Hosts every agent reaches directly: itself, and Minutehand's own calls to it never go through the proxy."""


def agent_environment(listen: Listen, port: int, ca_bundle: str | Path, secrets: Mapping[str, str]) -> dict[str, str]:
    """What the agent's process needs to reach the fakes and trust them, and nothing else: the proxy in both
    spellings libraries read, the hosts it reaches directly, the one CA file in each library's variable
    (`ca_bundle`, as the agent sees the path), and the signing secrets it is handed."""
    proxy = listen.proxy_url(port)
    direct = ",".join(dict.fromkeys([*DIRECT, *listen.no_proxy]))
    return {
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "NO_PROXY": direct,
        "https_proxy": proxy,
        "http_proxy": proxy,
        "no_proxy": direct,
        **{name: str(ca_bundle) for name in CA_VARIABLES},
        **secrets,
    }


def environment(agent: AgentUnderTest, *, state: Path, listen: Listen, ca_bundle: str | None = None) -> dict[str, str]:
    """The environment an agent Minutehand does not start needs for every run under `state` and `listen`, so it
    can be configured once and be running before `minutehand run` begins.

    The proxy's CA is made under `state` now if it is not there yet, and the bundle written; `ca_bundle` is
    where the agent will find that file when it is mounted elsewhere (in a container). Refused when the port is
    left to the system, which the agent could not know in advance, and when a signing secret is generated per
    run, which only reaches a command Minutehand starts."""
    if listen.port == 0:
        raise RunRefused("an agent configured before the run needs the proxy on a fixed port: give --proxy-port")
    generated = [t for t in agent.inbound if isinstance(t.secret, GeneratedSecret)]
    if generated:
        names = ", ".join(f"{t.provider} ({t.secret.env})" for t in generated if t.secret is not None)
        raise RunRefused(
            f"agent {agent.name} has its signing secret generated per run for {names}, and a generated secret "
            "reaches only a command Minutehand starts; an agent started on its own says "
            "`secret: {kind: from_env, env: <variable>}` with the secret it was configured with"
        )
    bundle = write_bundle(state / "ca")
    return agent_environment(listen, listen.port, ca_bundle or str(bundle.resolve()), {})


@asynccontextmanager
async def _proxy(routing: Routing, store: Store, clock: Clock, state: Path, listen: Listen) -> AsyncIterator[Proxy]:
    async with Proxy(routing, store, clock, confdir=state / "ca", host=listen.host, port=listen.port) as proxy:
        yield proxy


def _listens_on(agent: AgentUnderTest) -> str | None:
    """The URL whose port tells that the agent is up: its wake endpoint, or else where it takes events."""
    for source in agent.wakes:
        if isinstance(source, Reported | Polled):
            return source.wake_url
    return agent.inbound[0].url if agent.inbound else None


def _tail(log: Path) -> str:
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    return text.strip()[-1500:] or "(it printed nothing)"


class _Program:
    """The agent's own program, started by Minutehand: `application.restore.OwnProgram`, so a restore can stop it
    and start it again with the same environment, and nothing it held in memory survives the restore."""

    def __init__(self, command: Sequence[str], env: Mapping[str, str], agent: AgentUnderTest, log: Path) -> None:
        self._command = list(command)
        self._env = {**os.environ, **env}
        self._agent = agent
        self._log = log
        self._process: asyncio.subprocess.Process | None = None

    @property
    def command(self) -> Sequence[str]:
        return self._command

    async def start(self) -> None:
        with self._log.open("ab") as out:
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self._command,
                    env=self._env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=out,
                    stderr=out,
                )
            except OSError as e:
                raise RunRefused(f"the agent's command {self._command[0]} could not be started: {e}") from e
        url = _listens_on(self._agent)
        if url is not None:
            await _until_listening(self._process, url, self._log)

    def exited(self) -> int | None:
        return self._process.returncode if self._process is not None else None

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()


@asynccontextmanager
async def _agent_process(
    command: Sequence[str] | None,
    env: Mapping[str, str],
    agent: AgentUnderTest,
    log: Path,
) -> AsyncIterator[_Program | None]:
    if not command:
        yield None
        return
    program = _Program(command, env, agent, log)
    try:
        await program.start()
        yield program
        code = program.exited()
        if code is not None:
            raise AgentExited(f"the agent's command exited {code} before the run ended:\n{_tail(log)}")
    finally:
        await program.stop()


async def _until_listening(process: asyncio.subprocess.Process, url: str, log: Path) -> None:
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    give_up = loop.time() + LISTEN_TIMEOUT
    while True:
        if process.returncode is not None:
            raise AgentExited(
                f"the agent's command exited {process.returncode} before {url} accepted connections:\n{_tail(log)}"
            )
        try:
            _, writer = await asyncio.open_connection(host, port)
        except OSError:
            if loop.time() > give_up:
                raise AgentExited(
                    f"the agent's command did not accept connections on {url} within "
                    f"{LISTEN_TIMEOUT:.0f}s:\n{_tail(log)}"
                ) from None
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
