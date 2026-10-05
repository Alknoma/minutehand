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
    <state>/runs/<run_id>/world.pool/    the agent's snapshots, each file once, for a root run and its forks
                                         (`adapters.store.sqlite`); `wake-<n>/` is where the snapshot command
                                         writes until the store has kept it, `restoring/` where a restore reads
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
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field

from minutehand.adapters.agent.reach import reach_for
from minutehand.adapters.agent.replies import CapturedReplies
from minutehand.adapters.model.openai_compatible import API_KEY_VARIABLE, BASE_URL_VARIABLE, MODEL_VARIABLE
from minutehand.adapters.proxy.base_url import base_url
from minutehand.adapters.proxy.capture import Capturing, refuse_claimed, replaying_for, write_recordings
from minutehand.adapters.proxy.hosts import LOOPBACK_NAME, loopback
from minutehand.adapters.proxy.policy import DEFAULT_MODEL_HOSTS, Routing
from minutehand.adapters.proxy.registry import ProviderConflict, Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.proxy.trust import write_bundle
from minutehand.adapters.store.sqlite import SCHEMA_VERSION, SqliteStore, truncate_log
from minutehand.adapters.telemetry.forward import Forwarding
from minutehand.adapters.telemetry.receiver import Receiver, exporter_environment
from minutehand.application.checkpoint import (
    CHECKPOINT,
    AgentState,
    NoHooks,
    NotRestorable,
    Restorable,
    checkpoints,
    read_checkpoint,
)
from minutehand.application.forks import (
    ForkAccount,
    Outcomes,
    Record,
    change_words,
    outcomes,
    restore_account,
    summary,
)
from minutehand.application.model_calls import is_model_call, model_call, per_wake
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.refusals import RunRefused, refuse_unheld
from minutehand.application.replier_model import PeopleReplier
from minutehand.application.restore import Progress, Restored, SeenCall, restore_agent
from minutehand.application.rewind import FORK_RECORD, RESTORE_RECORD, changed_scenario, fork_run
from minutehand.application.run_clock import RunClock
from minutehand.application.state_hooks import SNAPSHOT_PRUNED, materialised, restore_dir
from minutehand.checks.runner import RunResult, evaluate, evaluate_judged, view_of
from minutehand.domain.agent import AgentUnderTest, Booked, GoalByMessage, Polled, Reported
from minutehand.domain.checks import Finding, FindingKind, Severity, WakeRecord
from minutehand.domain.experiment import Fork, Override, TicketEdit
from minutehand.domain.outbound import Acknowledge
from minutehand.domain.people import GeneratedSecret, SecretFromEnvironment, SigningSecret
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import Answers, Model, ProviderKey, Scenario, WrittenScenario
from minutehand.domain.storage import AgentSnapshot, Freed, RunUsage
from minutehand.domain.world import Actor, Operation, TicketSnapshot
from minutehand.ports.agent import Reports, TakesReplies
from minutehand.ports.clock import Clock
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.provider import (
    ASGIApp,
    BooksWakes,
    EditsTickets,
    HoldsTickets,
    Provider,
    PushesEvents,
)
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry

RUNS = "runs"
TELEMETRY = "telemetry"
"""The check name a notice about the agent's telemetry is reported under."""
WORLD = "world.db"
RECORD = "record.json"
RESULT = "result.json"
SCENARIO = "scenario.json"
AGENT = "agent.json"
AGENT_LOG = "agent.log"

CA_VARIABLES = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HTTPLIB2_CA_CERTS",
    "AWS_CA_BUNDLE",
    "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
)
"""Each HTTP library's own name for the file of CAs it trusts. httplib2 reads only its own and ignores
`SSL_CERT_FILE`; gRPC reads only `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH`. stripe reads none: it passes its bundled CA."""

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
    listen = listen or Listen()
    registry = Registry.installed()
    routing = _routing(registry, listen)
    services = _services(scenario, agent, registry)
    capturing = capturing_for(agent, registry, state=state, model_hosts=listen.model_hosts)
    outcomes: list[Outcome] = []
    first = _open(state, _new_run_id(), scenario)
    opened = [first[0]]
    async with intercepting(routing, first[0], first[1], state, listen, capturing=capturing) as proxy:
        for sample in range(samples):
            store, clock = first if sample == 0 else _open(state, _new_run_id(), scenario)
            if sample > 0:
                opened.append(store)
            directory = run_dir(state, store.run_id)
            _write_inputs(directory, scenario, agent)
            scorer = _Judge(scenario, model if judge else None, judging=judge)
            scorer.receiver = proxy.receiver
            signing = signing_for(agent)
            env = agent_environment(
                listen, proxy.port, proxy.ca_bundle, signing.for_agent, telemetry_port=proxy.telemetry_port
            ) | base_url_environment(agent, listen.proxy_url(proxy.port))
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
                    channels=replies_for(agent, scenario, signing),
                )
            write_recordings(directory, store.calls())
            outcomes.append(_keep(directory, record, scorer))
    for store in opened:
        store.close()  # the run is over: its write-ahead log is cut to nothing
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
    kept = first.snapshot(restorable.snapshot_of, restorable.wake)
    if kept is None or kept.pruned:
        why = SNAPSHOT_PRUNED if kept is not None else "its snapshot was never kept"
        raise RunRefused(f"the next sample cannot start where run {first.run_id} started: {why}")
    with materialised(
        first, restorable.snapshot_of, restorable.wake, restore_dir(state / RUNS, directory.name)
    ) as snapshot:
        restored = await restore_agent(
            agent.state,
            snapshot,
            checkpoint_seq=seq,
            recorded=restorable.report,
            reports=main if isinstance(main, Reports) else None,
            own=own,
            progress=progress,
            fingerprint=restorable.fingerprint,
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
    listen = listen or Listen()
    registry = Registry.installed()
    routing = _routing(registry, listen)
    services = _services(changed, agent, registry)
    forked_after = next((p.wake for p in fork_points(state, parent_run) if p.seq == changes.at_seq), 0)
    capturing = capturing_for(
        agent, registry, state=state, parent=parent_run, after_wake=forked_after, model_hosts=listen.model_hosts
    )
    child_id = _new_run_id()
    scorer = _Judge(changed, model if judge else None, judging=judge)
    signing = signing_for(agent)

    def open_parent(clock: Clock) -> Store:
        return SqliteStore(world, parent_run, clock)

    holding = RunClock(scenario.starts_at)
    async with intercepting(routing, open_parent(holding), holding, state, listen, capturing=capturing) as proxy:
        scorer.receiver = proxy.receiver
        env = agent_environment(
            listen, proxy.port, proxy.ca_bundle, signing.for_agent, telemetry_port=proxy.telemetry_port
        ) | base_url_environment(agent, listen.proxy_url(proxy.port))
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
                    channels=replies_for(agent, changed, signing),
                    manifests=registry.manifests,
                )
        except RunRefused:
            _remove_refused(state, world, child_id, changes.samples)
            raise
        finally:
            routing.apply(child_id, [])
            truncate_log(world)
    outcomes: list[Outcome] = []
    for record in records:
        child = run_dir(state, record.run_id)
        _write_inputs(child, changed, agent)
        with reading(state, record.run_id) as written:
            write_recordings(child, written.calls())
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
    state there can be put back: a checkpoint recorded as restorable whose snapshot has since been pruned is not."""
    points: list[ForkPoint] = []
    for seq, checkpoint in checkpoints(world).items():
        agent = checkpoint.agent
        if isinstance(agent, Restorable):
            kept = world.snapshot(agent.snapshot_of, agent.wake)
            if kept is not None and kept.pruned:
                agent = NotRestorable(reason=SNAPSHOT_PRUNED)
        points.append(ForkPoint(wake=checkpoint.wake, seq=seq, agent=agent))
    return points


def restore_of(state: Path, run_id: str) -> Restored | None:
    """How the agent was restored to start this run: a fork, or a sample after the first. None for a run that
    started from the beginning."""
    path = run_dir(state, run_id) / RESTORE_RECORD
    return Restored.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


def fork_of(state: Path, run_id: str) -> Fork | None:
    """What a fork was asked to change, as kept with it. None for a run that was not forked."""
    path = run_dir(state, run_id) / FORK_RECORD
    return Fork.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


def fork_account(state: Path, run_id: str) -> ForkAccount | None:
    """A fork, told (`application.forks`): where it split, what it changed, whether its restore was proven, and,
    once both have finished, how its outcome differs from its parent's. None for a run that was not forked."""
    entry = find(state, run_id)
    if entry.parent_run is None or entry.forked_at is None:
        return None
    at_seq = entry.forked_at
    asked = fork_of(state, run_id)
    parent_scenario = scenario_of(state, entry.parent_run)
    with reading(state, entry.parent_run) as parent_world:
        held = checkpoints(parent_world)
        checkpoint = held[at_seq]
        ran_on = any(seq < at_seq and earlier.wake == checkpoint.wake for seq, earlier in held.items())
        parent_events = parent_world.events()
        parent_record = Record(
            events=parent_events, calls=parent_world.calls(), spans=parent_world.spans(), checkpoints=held
        )
        calls = [model_call(s).model for s in parent_world.spans() if is_model_call(s)]
        after = [model_call(s).model for s in parent_world.spans() if s.wake > checkpoint.wake and is_model_call(s)]
        # The models the parent asked for after the split; when it made no call after it, those it asked for at all.
        models = list(dict.fromkeys(m for m in (after or calls) if m is not None))
    with reading(state, run_id) as fork_world:
        fork_record = Record(
            events=fork_world.events(),
            calls=fork_world.calls(),
            spans=fork_world.spans(),
            checkpoints=checkpoints(fork_world),
        )

    def ticket_before(override: Override) -> TicketSnapshot | None:
        if not isinstance(override, TicketEdit):
            return None
        held = [
            e.after
            for e in parent_events
            if e.seq <= at_seq and e.entity == override.entity and isinstance(e.after, TicketSnapshot)
        ]
        return held[-1] if held else None

    restored = restore_of(state, run_id)
    outcome: Outcomes | None = None
    if entry.finished and find(state, entry.parent_run).finished:
        outcome = outcomes(
            load(state, entry.parent_run).result,
            load(state, run_id).result,
            parent_record=parent_record,
            fork_record=fork_record,
            at_seq=at_seq,
            after_wake=checkpoint.wake,
            scenario=parent_scenario,
            fork_scenario=scenario_of(state, run_id),
        )
    return ForkAccount(
        parent_run=entry.parent_run,
        at_seq=at_seq,
        after_wake=checkpoint.wake,
        at=checkpoint.now,
        ran_on=ran_on,
        changes=[
            change_words(o, parent_scenario, ticket_before=ticket_before(o), models_before=models)
            for o in (asked.overrides if asked is not None else [])
        ],
        summary=summary(asked, parent_scenario) if asked is not None else "Rerun; what it changed was not kept",
        restore=restore_account(restored) if restored is not None else None,
        outcome=outcome,
    )


class Checkpointed(Model):
    """One checkpoint of a run with what keeping the agent's state there costs."""

    point: ForkPoint
    snapshot: AgentSnapshot | None = Field(description="None when the agent's state there was never snapshotted")


def checkpoints_of(state: Path, run_id: str) -> list[Checkpointed]:
    """Every checkpoint of a run, whether a fork can be taken from it, and its snapshot's size."""
    with reading(state, run_id) as world:
        found: list[Checkpointed] = []
        for point in points_in(world):
            restorable = checkpoints(world)[point.seq].agent
            snapshot = (
                world.snapshot(restorable.snapshot_of, restorable.wake) if isinstance(restorable, Restorable) else None
            )
            found.append(Checkpointed(point=point, snapshot=snapshot))
        return found


def pin(state: Path, run_id: str, seq: int, *, pinned: bool) -> AgentSnapshot:
    """Pin, or unpin, the snapshot of a run's checkpoint at `seq`, so its agent's `StateHooks.keep` never prunes
    it. Refused for a checkpoint with no snapshot, or one already pruned."""
    entry = find(state, run_id)
    with reading(state, run_id) as world:
        found = checkpoints(world)
    if seq not in found:
        raise RunRefused(f"run {run_id} has no checkpoint at seq {seq}; its checkpoints are at {list(found)}")
    restorable = found[seq].agent
    if not isinstance(restorable, Restorable):
        raise RunRefused(f"the checkpoint at seq {seq} of run {run_id} has no snapshot of the agent to pin")
    store = SqliteStore(run_dir(state, entry.root) / WORLD, run_id, RunClock(datetime.fromtimestamp(0, UTC)))
    try:
        return store.pin(restorable.snapshot_of, restorable.wake, pinned=pinned)
    except (LookupError, ValueError) as e:
        raise RunRefused(f"the checkpoint at seq {seq} of run {run_id}: {e}") from e
    finally:
        store.close()


def usage_of(state: Path, run_id: str) -> RunUsage:
    """What one run costs on disk: its rows, the bodies and the snapshot files it alone holds."""
    with reading(state, run_id) as world:
        return world.usage()


class Collected(Model):
    """What one housekeeping pass removed."""

    freed: Freed = Field(description="Stored bodies and snapshot files nothing referred to, across every world file")
    removed: list[str] = Field(default=[], description="Run directories removed whole, as asked")
    removed_bytes: int = Field(default=0, ge=0)
    swept: int = Field(default=0, ge=0, description="World files swept")
    skipped: list[str] = Field(default=[], description="World files that could not be swept, and why")


def collect(state: Path, *, remove: Sequence[str] = ()) -> Collected:
    """Remove the run directories named in `remove`, then sweep every world file left under `state` of the stored
    bodies and snapshot files nothing refers to. `minutehand gc`, and a standing server's retention of closed
    worlds, are this."""
    removed_bytes = 0
    for run_id in remove:
        directory = run_dir(state, run_id)
        removed_bytes += sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
        shutil.rmtree(directory, ignore_errors=True)
    totals = Freed(bodies=0, body_bytes=0, files=0, file_bytes=0)
    swept = 0
    skipped: list[str] = []
    base = state / RUNS
    for directory in sorted(base.iterdir()) if base.is_dir() else []:
        path = directory / WORLD
        roots = [run for run, parent, _ in _ReadOnlyStore.runs_in(path) if parent is None] if path.is_file() else []
        if not roots:
            continue
        try:
            store = SqliteStore(path, roots[0], RunClock(datetime.fromtimestamp(0, UTC)))
        except (RuntimeError, sqlite3.Error) as e:
            skipped.append(f"{path}: {e}")
            continue
        try:
            freed = store.sweep()
        finally:
            store.close()
        swept += 1
        totals = Freed(
            bodies=totals.bodies + freed.bodies,
            body_bytes=totals.body_bytes + freed.body_bytes,
            files=totals.files + freed.files,
            file_bytes=totals.file_bytes + freed.file_bytes,
        )
    return Collected(freed=totals, removed=list(remove), removed_bytes=removed_bytes, swept=swept, skipped=skipped)


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


@contextmanager
def reading_file(path: Path, run_id: str) -> Iterator[Store]:
    """One run of a world file at `path` opened for reading only, closed on leaving: a standing world's stretch
    before a reset, kept beside it (`serve.RESETS`)."""
    store = _ReadOnlyStore(path, run_id, RunClock(datetime.fromtimestamp(0, UTC)))
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
        self._attach(path, run_id, clock, _read_only(path))
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
        self.receiver: Receiver | None = None
        """Whose notices about telemetry it could not receive end the run's findings."""

    async def score(self, record: RunRecord, world: Store) -> RunResult:
        last = read_checkpoint(world)
        view = view_of(
            self._scenario,
            world.events(),
            record.wakes,
            world.replies(),
            withdrawn=last.withdrawn if last is not None else [],
            commitments=last.commitments if last is not None else None,
            unmatched_calls=[call.exchange for call in world.calls() if call.refused],
            model_calls=per_wake(world.spans(), [w.index for w in record.wakes]),
        )
        result = (
            await evaluate_judged(view, self._model, stop=record.stop)
            if self._judging
            else evaluate(view, stop=record.stop)
        )
        heard = self.receiver.notices if self.receiver is not None else []
        if heard:
            told = [
                Finding(check=TELEMETRY, severity=Severity.WARNING, kind=FindingKind.REVIEW, message=notice)
                for notice in heard
            ]
            result = result.model_copy(update={"findings": [*result.findings, *told]})
        self.results[record.run_id] = result
        return result


def _routing(registry: Registry, listen: Listen) -> Routing:
    try:
        return Routing(registry, model_hosts=listen.model_hosts)
    except (ProviderConflict, ValueError) as e:
        raise RunRefused(f"the model hosts {', '.join(listen.model_hosts)}: {e}") from e


def capturing_for(
    agent: AgentUnderTest,
    registry: Registry,
    *,
    state: Path,
    parent: str | None = None,
    after_wake: int = 0,
    model_hosts: Sequence[str] = DEFAULT_MODEL_HOSTS,
) -> Capturing:
    """What a run captures of the hosts no provider claims: the agent's outbound declarations, refused when one
    names a host a provider claims or a model API, with the recordings each replay reads. In a fork of `parent`,
    a pass-through host replays the parent's answer to the same call unless it says otherwise (`in_forks`)."""
    try:
        refuse_claimed(agent.outbound, registry, model_hosts)
        replaying = replaying_for(agent.outbound, state=state, parent=parent, after_wake=after_wake)
        return Capturing(agent.outbound, replaying=replaying)
    except (ProviderConflict, FileNotFoundError, ValueError) as e:
        raise RunRefused(f"agent {agent.name}'s outbound hosts: {e}") from e


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
    named |= {s.provider for s in scenario.spaces} | {s.provider for s in scenario.provider_seeds}
    named |= {c.provider for c in scenario.channels}
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
    refuse_unheld(scenario, manifests)
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
    """The secrets a run signs pushed events with: one per provider, one per captured host whose replies are
    signed, and the ones the agent's command is given."""

    by_provider: dict[ProviderKey, str]
    for_agent: dict[str, str]
    by_host: dict[ProviderKey, str]


def replies_for(agent: AgentUnderTest, scenario: Scenario, signing: Signing) -> dict[ProviderKey, TakesReplies]:
    """A channel per captured host whose sends people can answer, keyed by the name its messages go under."""
    channels: dict[ProviderKey, TakesReplies] = {}
    for declared in agent.outbound:
        if isinstance(declared, Acknowledge) and declared.replies is not None:
            secret = signing.by_host[declared.key] if declared.key in signing.by_host else None
            channels[declared.key] = CapturedReplies(declared, scenario.people, secret=secret)
    return channels


def signing_for(agent: AgentUnderTest) -> Signing:
    """Each inbound target's secret for this run: generated and handed to the agent's command, read from this
    process's own variable (the secret an agent already running was configured with), or, when the target
    names none, generated and given to no one. A variable named and not set refuses the run."""
    for_agent: dict[str, str] = {}

    def resolve(source: SigningSecret | None, what: str) -> str:
        if isinstance(source, SecretFromEnvironment):
            if source.env not in os.environ:
                raise RunRefused(
                    f"the agent's {what} signing secret is read from {source.env}, which is not set "
                    "in Minutehand's environment; set it to the secret the agent was configured with"
                )
            return os.environ[source.env]
        value = secrets.token_hex(16)
        if isinstance(source, GeneratedSecret):
            for_agent[source.env] = value
        return value

    by_provider: dict[ProviderKey, str] = {}
    for target in agent.inbound:
        value = resolve(target.secret, target.provider)
        by_provider.setdefault(target.provider, value)
    by_host: dict[ProviderKey, str] = {}
    for declared in agent.outbound:
        if isinstance(declared, Acknowledge) and declared.replies is not None and declared.replies.signing is not None:
            by_host[declared.key] = resolve(declared.replies.signing.secret, f"{declared.host} reply")
    return Signing(by_provider=by_provider, for_agent=for_agent, by_host=by_host)


class Listen(Model):
    """Where the proxy and the telemetry receiver listen, how the agent reaches them, and what the proxy records.

    The defaults suit an agent Minutehand starts on this machine: loopback, ports the system picks. An agent in
    containers, or one already running, needs a fixed `port` and `telemetry_port`, a `host` it can reach
    (`0.0.0.0`), and `agent_host`, the name the AGENT uses for this machine, which is not the bind address
    (`host.docker.internal`)."""

    host: str = "127.0.0.1"
    port: int = Field(default=0, ge=0, le=65535, description="0 lets the system pick one for each run")
    agent_host: str | None = Field(
        default=None, description="The host in the proxy URL the agent is given; None is the bind host"
    )
    no_proxy: list[str] = Field(default=[], description="More hosts the agent reaches directly, not through the proxy")
    receive_telemetry: bool = Field(
        default=True, description="Serve an OTLP/HTTP endpoint on `host` and point the agent's exporter at it"
    )
    telemetry_port: int = Field(default=0, ge=0, le=65535, description="The receiver's port; 0 lets the system pick")
    record_model_calls: bool = Field(
        default=False,
        description="Open the agent's calls to model APIs, send them on unchanged, and keep each as a span",
    )
    capture_unknown: bool = Field(
        default=False,
        description="Pass through and keep every call to a host no provider claims and no declaration names, "
        "rather than refusing it: the first run of an agent, to see what it calls",
    )
    upstream_ca: Path | None = Field(
        default=None,
        description="The CAs a real host is verified against when a call is passed through, edited or recorded; "
        "None trusts the system's",
    )
    model_hosts: list[str] = Field(
        default=list(DEFAULT_MODEL_HOSTS),
        description="Hosts that are model APIs: tunnelled, or opened to edit or record their calls. The three "
        "public ones by default; a self-hosted or other provider's API is added here",
    )

    def _reached_at(self) -> str:
        """This machine as the agent names it. Binding every interface is not an address: it is reached on loopback."""
        return self.agent_host or ("127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host)

    def proxy_url(self, port: int) -> str:
        """The proxy as the agent reaches it."""
        return f"http://{self._reached_at()}:{port}"

    def telemetry_url(self, port: int) -> str:
        """The telemetry receiver as the agent reaches it."""
        return f"http://{self._reached_at()}:{port}"

    def elsewhere(self) -> bool:
        """Whether the agent runs on another machine than the proxy (a container): its `localhost` is then not the
        proxy's, and the proxy cannot forward a call to it."""
        return self.agent_host is not None and not loopback(self.agent_host)

    def direct(self) -> list[str]:
        """The hosts the agent reaches directly (`NO_PROXY`): this machine by its loopback ADDRESS, those named, and
        the receiver's host when it is on, whose OTLP endpoint is not reached through the proxy. `localhost` is
        named only for an agent `elsewhere`: on this machine the proxy forwards it (`ProxyAddon.forwarded`), and
        named it would send every `*.localhost` host direct under requests, urllib, aiohttp and curl."""
        hosts = [*DIRECT, *self.no_proxy]
        if self.receive_telemetry and not loopback(self._reached_at()):
            hosts.append(self._reached_at())
        if self.elsewhere():
            hosts.append(LOOPBACK_NAME)
        return list(dict.fromkeys(hosts))


DIRECT = ("127.0.0.1",)
"""What every agent reaches directly: this machine by address. Never a name, which requests, urllib, aiohttp and curl
read as covering every name under it, and never `::1`, which requests reads as a suffix of any IPv6 literal
(`2001:db8::1`). An IPv4 address is matched exactly by every client (docs/design.md, "What the agent reaches
directly")."""


def agent_environment(
    listen: Listen, port: int, ca_bundle: str | Path, secrets: Mapping[str, str], *, telemetry_port: int | None
) -> dict[str, str]:
    """What the agent's process needs to reach the fakes and trust them, and nothing else: the proxy in both
    spellings libraries read, the hosts it reaches directly (also as `no_grpc_proxy`: gRPC applies `http_proxy`
    even to an insecure channel to an in-stack emulator), the one CA file in each library's variable
    (`ca_bundle`, as the agent sees the path), the signing secrets it is handed, and, unless `telemetry_port`
    is None (receiving is off), its OTLP exporter pointed at the receiver."""
    proxy = listen.proxy_url(port)
    direct = ",".join(listen.direct())
    exporter = exporter_environment(listen.telemetry_url(telemetry_port)) if telemetry_port is not None else {}
    return {
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "NO_PROXY": direct,
        "https_proxy": proxy,
        "http_proxy": proxy,
        "no_proxy": direct,
        "no_grpc_proxy": direct,
        **{name: str(ca_bundle) for name in CA_VARIABLES},
        **exporter,
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
    generated = [f"{t.provider} ({t.secret.env})" for t in agent.inbound if isinstance(t.secret, GeneratedSecret)]
    generated += [
        f"{d.host} replies ({d.replies.signing.secret.env})"
        for d in agent.outbound
        if isinstance(d, Acknowledge)
        and d.replies is not None
        and d.replies.signing is not None
        and isinstance(d.replies.signing.secret, GeneratedSecret)
    ]
    if generated:
        names = ", ".join(generated)
        raise RunRefused(
            f"agent {agent.name} has its signing secret generated per run for {names}, and a generated secret "
            "reaches only a command Minutehand starts; an agent started on its own says "
            "`secret: {kind: from_env, env: <variable>}` with the secret it was configured with"
        )
    if listen.receive_telemetry and listen.telemetry_port == 0:
        raise RunRefused(
            "an agent configured before the run needs the telemetry receiver on a fixed port: give "
            "--telemetry-port, or --no-receive-telemetry to leave the agent's telemetry where it goes"
        )
    bundle = write_bundle(state / "ca")
    telemetry_port = listen.telemetry_port if listen.receive_telemetry else None
    handed = agent_environment(
        listen, listen.port, ca_bundle or str(bundle.resolve()), {}, telemetry_port=telemetry_port
    )
    return handed | base_url_environment(agent, listen.proxy_url(listen.port))


def base_url_environment(agent: AgentUnderTest, proxy: str) -> dict[str, str]:
    """Each base URL the agent file declares (`AgentUnderTest.base_urls`), in its variable, against the proxy as the
    agent reaches it (`adapters.proxy.base_url`)."""
    return {declared.env: base_url(proxy, declared.host) + declared.path for declared in agent.base_urls}


@dataclass(frozen=True)
class Intercepting:
    """`application.orchestrator.Mounts`: the proxy, and the telemetry receiver beside it when receiving is on,
    each moved to the next run together."""

    proxy: Proxy
    receiver: Receiver | None

    @property
    def port(self) -> int:
        return self.proxy.port

    @property
    def ca_bundle(self) -> Path:
        return self.proxy.ca_bundle

    @property
    def telemetry_port(self) -> int | None:
        """The receiver's port; None when receiving is off."""
        return self.receiver.port if self.receiver is not None else None

    def last_call(self) -> SeenCall | None:
        """`application.restore.Traffic`: the latest call the proxy saw from the agent."""
        return self.proxy.last_call()

    def waiting(self) -> list[str]:
        """`application.restore.Traffic`: what the agent sent and the proxy has not seen answered."""
        return self.proxy.waiting()

    def flush(self) -> None:
        self.proxy.flush()

    def mount(self, world: Store, clock: Clock, apps: Mapping[ProviderKey, ASGIApp], *, scenario: Scenario) -> None:
        self.proxy.mount(world, clock, apps, scenario=scenario)
        if self.receiver is not None:
            self.receiver.mount(world, clock)


@asynccontextmanager
async def intercepting(
    routing: Routing,
    store: Store,
    clock: Clock,
    state: Path,
    listen: Listen,
    *,
    capturing: Capturing | None = None,
) -> AsyncIterator[Intercepting]:
    """The proxy and, unless `listen` turns it off, the receiver, on the same host. The receiver passes what it
    takes on to wherever this process's own environment sent OTLP before (`Forwarding.from_environment`).
    """
    async with Proxy(
        routing,
        store,
        clock,
        confdir=state / "ca",
        host=listen.host,
        port=listen.port,
        upstream_ca=listen.upstream_ca,
        record_model_calls=listen.record_model_calls,
        capturing=capturing,
        capture_unknown=listen.capture_unknown,
    ) as proxy:
        if not listen.receive_telemetry:
            yield Intercepting(proxy, None)
            return
        receiver = Receiver(
            store,
            clock,
            host=listen.host,
            port=listen.telemetry_port,
            forwarding=Forwarding.from_environment(os.environ),
            agent_host=listen.agent_host,
        )
        async with receiver:
            yield Intercepting(proxy, receiver)


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
