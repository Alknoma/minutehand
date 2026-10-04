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
    <state>/runs/<run_id>/wake-<n>/      the agent's snapshot after wake n, when it declares `StateHooks`

One proxy per process: mitmproxy keeps its master in a module global, so `play` and `fork` each start one
and move it from sample to sample with `Proxy.mount`, and never two at once.

The Slack provider signs pushed events with the secret in the variable its `InboundTarget.secret_env`
names, read from this process's environment. Each run generates its secrets, sets them here for as long
as the run lasts, and hands the same values to the agent's process.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field

from minutehand.adapters.agent.reach import reach_for
from minutehand.adapters.model.openai_compatible import API_KEY_VARIABLE, BASE_URL_VARIABLE, MODEL_VARIABLE
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SCHEMA_VERSION, SqliteStore
from minutehand.application.checkpoint import CHECKPOINT, read_checkpoint
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.refusals import RunRefused
from minutehand.application.replier_model import PeopleReplier
from minutehand.application.rewind import changed_scenario, fork_run
from minutehand.application.run_clock import RunClock
from minutehand.application.state_hooks import run_hook, wake_dir
from minutehand.checks.runner import RunResult, evaluate, evaluate_judged, view_of
from minutehand.domain.agent import AgentUnderTest, Booked, GoalByMessage, Polled, Reported
from minutehand.domain.checks import WakeRecord
from minutehand.domain.experiment import Fork
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import Answers, Model, ProviderKey, Scenario
from minutehand.domain.world import Actor, Operation
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
    """Where a fork may be taken: the end of a wake, as a seq in the world's log."""

    wake: int
    seq: int


def run_dir(state: Path, run_id: str) -> Path:
    return state / RUNS / run_id


# -- playing a scenario ---------------------------------------------------------------------------------------


async def play(
    scenario: Scenario,
    agent: AgentUnderTest,
    *,
    state: Path,
    samples: int = 1,
    command: Sequence[str] | None = None,
    telemetry: Telemetry | None = None,
    model: LanguageModel | None = None,
    judge: bool = False,
) -> list[Outcome]:
    """Run the scenario `samples` times from its start, each a run of its own, through one proxy.

    `model` writes the replies of `Answers` people and, with `judge`, runs the judged checks; a scenario
    with an `Answers` person and no model is refused before anything starts.

    `command`, when given, is the agent's own program: started before each run with only the proxy, its
    CA and the run's signing secrets added to this process's environment, waited for until it accepts
    connections, and stopped when the run ends.

    Each sample after the first starts from the agent's own state as the first found it, restored through
    its `StateHooks`. Without hooks the agent carries what it remembers from one sample into the next, and
    the samples are not independent.
    """
    if samples < 1:
        raise RunRefused(f"a run needs at least one sample, not {samples}")
    _refuse_unwritten(scenario, model)
    registry = Registry.installed()
    routing = Routing(registry)
    services = _services(scenario, agent, registry)
    outcomes: list[Outcome] = []
    first = _open(state, _new_run_id(), scenario)
    async with Proxy(routing, first[0], first[1], confdir=state / "ca") as proxy:
        for sample in range(samples):
            store, clock = first if sample == 0 else _open(state, _new_run_id(), scenario)
            if sample > 0 and agent.state is not None:
                await run_hook(agent.state.restore, wake_dir(state / RUNS, first[0].run_id, 0))
            directory = run_dir(state, store.run_id)
            _write_inputs(directory, scenario, agent)
            scorer = _Judge(scenario, model if judge else None, judging=judge)
            run_secrets = _secrets(agent)
            env = _agent_env(proxy, run_secrets)
            with _in_this_process(run_secrets):
                async with _agent_process(command, env, agent, directory / AGENT_LOG):
                    record = await run_scenario(
                        scenario=scenario,
                        agent=agent,
                        reach=reach_for(agent, env=env),
                        store=store,
                        clock=clock,
                        services=services,
                        replier=PeopleReplier(scenario, model),
                        telemetry=telemetry,
                        mounts=proxy,
                        scorer=scorer,
                        state_dir=state / RUNS,
                    )
            outcomes.append(_keep(directory, record, scorer))
    return outcomes


async def fork(
    parent_run: str,
    changes: Fork,
    *,
    state: Path,
    command: Sequence[str] | None = None,
    telemetry: Telemetry | None = None,
    model: LanguageModel | None = None,
    judge: bool = False,
) -> list[Outcome]:
    """Rerun a finished run from one of its checkpoints with `changes` applied, once per `Fork.samples`.

    The people, tickets and deadline change in the world; a `PromptPatch` or `ModelSwap` is applied on the
    wire, to the agent's own calls to its model, through the proxy's EDIT policy.
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
    run_secrets = _secrets(agent)

    def open_parent(clock: Clock) -> Store:
        return SqliteStore(world, parent_run, clock)

    holding = RunClock(scenario.starts_at)
    async with Proxy(routing, open_parent(holding), holding, confdir=state / "ca") as proxy:
        env = _agent_env(proxy, run_secrets)
        log = run_dir(state, child_id) / AGENT_LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with _in_this_process(run_secrets):
                async with _agent_process(command, env, agent, log):
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
                        scorer=scorer,
                    )
        finally:
            routing.apply(child_id, [])
    outcomes: list[Outcome] = []
    for record in records:
        child = run_dir(state, record.run_id)
        _write_inputs(child, changed, agent)
        outcomes.append(_keep(child, record, scorer))
    return outcomes


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
    """The checkpoints a world's log holds, each a point a fork may be taken from."""
    return [ForkPoint(wake=e.wake, seq=e.seq) for e in world.events() if e.entity == CHECKPOINT]


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


def _secrets(agent: AgentUnderTest) -> dict[str, str]:
    """A fresh signing secret for every variable the agent's inbound targets name."""
    return {t.secret_env: secrets.token_hex(16) for t in agent.inbound if t.secret_env is not None}


def _agent_env(proxy: Proxy, run_secrets: Mapping[str, str]) -> dict[str, str]:
    """What the agent's process needs to reach the fakes and trust them, and nothing else."""
    ca = str(proxy.ca_bundle)
    return {
        "HTTPS_PROXY": proxy.url,
        "HTTP_PROXY": proxy.url,
        "NO_PROXY": "localhost,127.0.0.1",
        **{name: ca for name in CA_VARIABLES},
        **run_secrets,
    }


@contextmanager
def _in_this_process(variables: Mapping[str, str]) -> Iterator[None]:
    """Set the run's secrets in this process's environment, where the providers read them, for the run."""
    before = {name: os.environ[name] if name in os.environ else None for name in variables}
    os.environ.update(variables)
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                del os.environ[name]
            else:
                os.environ[name] = value


def _listens_on(agent: AgentUnderTest) -> str | None:
    """The URL whose port tells that the agent is up: its wake endpoint, or else where it takes events."""
    for source in agent.wakes:
        if isinstance(source, Reported | Polled):
            return source.wake_url
    return agent.inbound[0].url if agent.inbound else None


def _tail(log: Path) -> str:
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    return text.strip()[-1500:] or "(it printed nothing)"


@asynccontextmanager
async def _agent_process(
    command: Sequence[str] | None,
    env: Mapping[str, str],
    agent: AgentUnderTest,
    log: Path,
) -> AsyncIterator[None]:
    if not command:
        yield
        return
    with log.open("ab") as out:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                env={**os.environ, **env},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out,
                stderr=out,
            )
        except OSError as e:
            raise RunRefused(f"the agent's command {command[0]} could not be started: {e}") from e
        try:
            url = _listens_on(agent)
            if url is not None:
                await _until_listening(process, url, log)
            yield
            if process.returncode is not None:
                raise AgentExited(
                    f"the agent's command exited {process.returncode} before the run ended:\n{_tail(log)}"
                )
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
                except TimeoutError:
                    process.kill()
                    await process.wait()


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
