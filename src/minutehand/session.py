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
    <state>/runs/<run_id>/own/           a fresh empty SQLite file per variable the agent file names under
                                         `own_databases`: the agent's state beside its memory, outside forks
    <state>/runs/<run_id>/restore.json   for a fork: how its agent was found at its start (its memory, its report)

One proxy per process: mitmproxy keeps its master in a module global, so `play` and `fork` each start one
and move it from sample to sample with `Proxy.mount`, and never two at once.

A provider signs the events it pushes with the secret its `InboundTarget.secret` resolves to for the run:
one generated per run and handed to the agent's command, or the agent's own, read from a variable of this
process. The secret is given to the provider with each push; it is never set in this process's environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import signal
import sqlite3
from collections.abc import AsyncIterator, Collection, Iterator, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import Field, JsonValue

from minutehand.adapters.agent.inboxes import HttpInboxReach
from minutehand.adapters.agent.openapi import OperationUnresolved
from minutehand.adapters.agent.reach import reach_for
from minutehand.adapters.agent.replies import CapturedReplies
from minutehand.adapters.emulator.fleet import Emulators
from minutehand.adapters.emulator.process import Running
from minutehand.adapters.model.openai_compatible import API_KEY_VARIABLE, BASE_URL_VARIABLE, MODEL_VARIABLE
from minutehand.adapters.proxy.base_url import base_url
from minutehand.adapters.proxy.capture import Capturing, refuse_claimed, replaying_for, write_recordings
from minutehand.adapters.proxy.hosts import LOOPBACK_NAME, loopback
from minutehand.adapters.proxy.policy import DEFAULT_MODEL_HOSTS, Routing
from minutehand.adapters.proxy.registry import ProviderConflict, Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.proxy.trust import write_bundle
from minutehand.adapters.pushing import HttpPushes
from minutehand.adapters.store.sqlite import SCHEMA_VERSION, SqliteStore, truncate_log
from minutehand.adapters.telemetry.forward import Forwarding
from minutehand.adapters.telemetry.receiver import AGENT_PATH, MCP_PATH, MCP_URL_ENV, Receiver, exporter_environment
from minutehand.agent._wire import ON as AGENT_ON
from minutehand.agent._wire import URL as AGENT_URL
from minutehand.application.around_proxy import around_proxy, uncalled_providers
from minutehand.application.cases import CASE, CaseKept, CaseStore
from minutehand.application.checkpoint import CHECKPOINT, AgentState, checkpoints, read_checkpoint
from minutehand.application.dues import due_entries
from minutehand.application.emulators import findings as emulator_findings
from minutehand.application.emulators import record_health
from minutehand.application.files import FileRefused, load_document, read_yaml
from minutehand.application.forks import (
    ForkAccount,
    Outcomes,
    Record,
    change_words,
    outcomes,
    restore_account,
    summary,
)
from minutehand.application.inboxes import Inboxes
from minutehand.application.items import provided_types, typed_items
from minutehand.application.items import rhythm as declared_rhythm
from minutehand.application.kept import KeptModel
from minutehand.application.model_calls import is_model_call, model_call, per_wake
from minutehand.application.orchestrator import Services, run_scenario
from minutehand.application.people import needs_model
from minutehand.application.refusals import RunRefused, refuse_unheld
from minutehand.application.replier import PeopleReplier, unspoken
from minutehand.application.restore import Progress, Restored
from minutehand.application.rewind import (
    FORK_RECORD,
    RESTORE_RECORD,
    changed_scenario,
    fork_run,
    memory_writes,
    refused_by,
)
from minutehand.application.run_clock import RunClock
from minutehand.application.services import ServiceDesk, unvoiced
from minutehand.application.steps import steps
from minutehand.application.traffic import SeenCall
from minutehand.checks.patterns import PATTERNS
from minutehand.checks.runner import (
    ChecksRefused,
    RunResult,
    broken,
    contract_breaks,
    evaluate,
    evaluate_judged,
    finding_names,
    load_checks,
    view_of,
)
from minutehand.domain.agent import (
    AgentUnderTest,
    Booked,
    Contained,
    GoalByMessage,
    Marked,
    OwnDatabaseForm,
    Polled,
    Reported,
)
from minutehand.domain.assessments import IntegrityCheck, Rule, integrity_fails, merged, refuse_unknown_people
from minutehand.domain.checks import (
    Check,
    CommitmentsReported,
    DeclaredCollection,
    Finding,
    FindingKind,
    RunView,
    Severity,
    Stability,
    WakeRecord,
)
from minutehand.domain.common import GeneratedSecret, SecretFromEnvironment, SigningSecret
from minutehand.domain.emulator import EmulatorChange
from minutehand.domain.experiment import Fork, Override, TicketEdit
from minutehand.domain.items import TypedItem
from minutehand.domain.outbound import Acknowledge, DeclaredStore, UnknownHosts
from minutehand.domain.people import Delivery
from minutehand.domain.provider import Manifest
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Model, Person, ProviderKey, Scenario, WrittenScenario
from minutehand.domain.storage import Freed, RunUsage
from minutehand.domain.world import Actor, Operation, TicketSnapshot
from minutehand.ports.agent import TakesReplies
from minutehand.ports.clock import Clock
from minutehand.ports.model import JudgedCheck
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.provider import (
    ASGIApp,
    BooksWakes,
    HeldCalls,
    Provider,
    PushesEvents,
    ServesSockets,
    TypesItems,
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

NODE_PROXY = "NODE_USE_ENV_PROXY"
"""Node's built-in `fetch` (undici, and so `@slack/web-api` v8 and every SDK on it) reads no proxy variable unless this
is `1` (Node 24 and later; Node 25 applies it to `http` and `https` too). Set for every agent, whatever its command:
a Node process started by a shell script or by another program is reached all the same, and every other runtime
ignores it. An older Node may not read it: such an agent needs a base URL or transparent capture."""

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


class Played(Model):
    """What `run`, `fork` and `findings` print with --json: one shape, so a script reads any of them the same way.
    `findings` prints the one run asked for; `stability` is set only for several samples."""

    outcomes: list[Outcome]
    stability: Stability | None = None


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
    seed: int | None = None,
) -> list[Outcome]:
    """Run the scenario `samples` times from its start, each a run of its own, through one proxy.

    `model` writes what people say (conversing, and each step of a script in the person's words) and, with
    `judge`, runs the judged checks; a scenario with anyone a model speaks for and no model is refused before
    anything starts. `seed` replaces the scenario's own seed, where every draw of the run comes from.

    `command`, when given, is the agent's own program: started before each run with only the proxy, its
    CA and the run's signing secrets added to this process's environment, waited for until it accepts
    connections, and stopped when the run ends.

    Each sample is a run of its own, so its memory (`minutehand.agent.store`) starts from the scenario's and
    nothing else, and so does every own database the agent file names. What the agent keeps anywhere else, in a
    process Minutehand did not start, is carried from one sample into the next.

    A scenario with no `starts_at` starts now: the instant is taken once, here, and every sample plays and
    records it, so a fork of any of them starts from the same moment.
    """
    if samples < 1:
        raise RunRefused(f"a run needs at least one sample, not {samples}")
    if _contained(agent) and written.starts_at is not None:
        raise RunRefused(
            "a contained agent's sandbox clock starts at the real present and only moves forward: leave starts_at out "
            "of the scenario, so the run starts now"
        )
    scenario = written.starting(_now()).seeded(seed)
    _refuse_unwritten(scenario, agent, model)
    own_checks = _own_checks(agent)
    rules = rules_for(agent, scenario, own_checks)
    listen = listen or Listen()
    _refuse_unmodeled(listen, model)
    registry = Registry.installed()
    routing = _routing(registry, listen)
    services = _services(scenario, agent, registry)
    routes: dict[str, Running] = {}
    desk = desk_for(scenario, agent, model, undeclared=listen.capture_unknown is UnknownHosts.MODEL)
    capturing = (
        capturing_for(agent, registry, state=state, model_hosts=listen.model_hosts)
        .with_emulators(routes)
        .with_services(desk)
    )
    outcomes: list[Outcome] = []
    first = _open(state, _new_run_id(), scenario)
    opened = [first[0]]
    async with (
        intercepting(routing, first[0], first[1], state, listen, capturing=capturing) as proxy,
        emulating(agent, proxy, listen, run_dir(state, first[0].run_id), telemetry, routes) as emulators,
    ):
        for sample in range(samples):
            store, clock = first if sample == 0 else _open(state, _new_run_id(), scenario)
            if sample > 0:
                opened.append(store)
            directory = run_dir(state, store.run_id)
            _write_inputs(directory, scenario, agent)
            scorer = _Judge(
                scenario,
                model if judge else None,
                judging=judge,
                own=own_checks,
                rules=rules,
                fail_on_integrity=integrity_fails(agent.fail_on_integrity, scenario.fail_on_integrity),
                claims=_claims(registry, services),
                rhythm=declared_rhythm(agent),
                collections=declared_collections(agent),
            )
            scorer.receiver = proxy.receiver
            signing = signing_for(agent, scenario.people)
            own_files = OwnDatabases(agent, directory)
            scorer.own_files = own_files
            env = (
                agent_environment(
                    listen,
                    proxy.port,
                    proxy.ca_bundle,
                    signing.for_agent,
                    telemetry_port=proxy.telemetry_port,
                    agent_url=proxy.agent_url(listen),
                )
                | base_url_environment(agent, listen.proxy_url(proxy.port))
                | own_files.environment()
            )
            reach = reach_for(agent, env=env)
            async with _agent_process(command, env, agent, directory / AGENT_LOG):
                record = await run_scenario(
                    scenario=scenario,
                    agent=agent,
                    reach=reach,
                    store=store,
                    clock=clock,
                    services=services,
                    replier=PeopleReplier(scenario, model, agent.inboxes),
                    telemetry=telemetry,
                    mounts=proxy,
                    scorer=scorer,
                    signing=signing.by_provider,
                    traffic=proxy,
                    channels=replies_for(agent, scenario, signing),
                    environment=emulators,
                    inboxes=inboxes_for(agent, scenario, signing),
                    outside=own_files,
                    model=model,
                    desk=desk,
                )
            write_recordings(directory, store.calls())
            outcomes.append(_keep(directory, record, scorer))
    for store in opened:
        store.close()  # the run is over: its write-ahead log is cut to nothing
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
    listen: Listen | None = None,
    progress: Progress | None = None,
) -> list[Outcome]:
    """Rerun a finished run from one of its checkpoints with `changes` applied, once per `Fork.samples`.

    The people, tickets and deadline change in the world; a `PromptPatch` or `ModelSwap` is applied on the
    wire, to the agent's own calls to its model, through the proxy's EDIT policy. The agent's memory is its
    parent's at the checkpoint, with nothing restored; its program, when `command` is given, is started once the
    fork exists, and its report compared with the checkpoint's (`application.restore`); `progress` hears each step.

    A fork that is refused leaves no run behind: no row in the world file and no directory.
    """
    if changes.parent_run != parent_run:
        raise RunRefused(f"the changes are for run {changes.parent_run}, not {parent_run}")
    if _contained(AgentUnderTest.model_validate_json((run_dir(state, parent_run) / AGENT).read_text(encoding="utf-8"))):
        raise RunRefused(
            "a contained agent's sandbox clock cannot go back to a checkpoint, so its run cannot be forked"
        )
    parent = load(state, parent_run)
    directory = run_dir(state, parent_run)
    scenario = Scenario.model_validate_json((directory / SCENARIO).read_text(encoding="utf-8"))
    agent = AgentUnderTest.model_validate_json((directory / AGENT).read_text(encoding="utf-8"))
    world = _root_dir(state, parent.record) / WORLD
    changed = changed_scenario(scenario, changes)
    _refuse_unwritten(changed, agent, model)
    own_checks = _own_checks(agent)
    rules = rules_for(agent, changed, own_checks)
    listen = listen or Listen()
    _refuse_unmodeled(listen, model)
    registry = Registry.installed()
    routing = _routing(registry, listen)
    services = _services(changed, agent, registry)
    forked_after = next((p.wake for p in fork_points(state, parent_run) if p.seq == changes.at_seq), 0)
    capturing = capturing_for(
        agent, registry, state=state, parent=parent_run, after_wake=forked_after, model_hosts=listen.model_hosts
    )
    child_id = _new_run_id()
    scorer = _Judge(
        changed,
        model if judge else None,
        judging=judge,
        own=own_checks,
        rules=rules,
        fail_on_integrity=integrity_fails(agent.fail_on_integrity, changed.fail_on_integrity),
        claims=_claims(registry, services),
        rhythm=declared_rhythm(agent),
        collections=declared_collections(agent),
    )
    signing = signing_for(agent, changed.people)

    def open_parent(clock: Clock) -> Store:
        return SqliteStore(world, parent_run, clock)

    holding = RunClock(scenario.starts_at)
    routes: dict[str, Running] = {}
    desk = desk_for(changed, agent, model, undeclared=listen.capture_unknown is UnknownHosts.MODEL)
    capturing = capturing.with_emulators(routes).with_services(desk)
    async with (
        intercepting(routing, open_parent(holding), holding, state, listen, capturing=capturing) as proxy,
        emulating(agent, proxy, listen, run_dir(state, child_id), telemetry, routes) as emulators,
    ):
        scorer.receiver = proxy.receiver
        proxy.hold()  # until the child is mounted, the agent's memory is not the parent's at its end
        own_files = OwnDatabases(agent, run_dir(state, child_id))
        scorer.own_files = own_files
        env = (
            agent_environment(
                listen,
                proxy.port,
                proxy.ca_bundle,
                signing.for_agent,
                telemetry_port=proxy.telemetry_port,
                agent_url=proxy.agent_url(listen),
            )
            | base_url_environment(agent, listen.proxy_url(proxy.port))
            | own_files.environment()
        )
        log = run_dir(state, child_id) / AGENT_LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with _agent_process(command, env, agent, log, started=False) as own:
                records = await fork_run(
                    fork=changes,
                    parent=parent.record,
                    open_parent=open_parent,
                    run_id=child_id,
                    scenario=scenario,
                    agent=agent,
                    reach=reach_for(agent, env=env),
                    services=services,
                    replier_for=lambda s, pins: PeopleReplier(s, model, agent.inboxes, pins=pins),
                    model=model,
                    desk=desk,
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
                    environment=emulators,
                    inboxes=inboxes_for(agent, changed, signing),
                    outside=own_files,
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


def recorded_view(state: Path, run_id: str) -> RunView:
    """What every check reads of a finished run, built again from its world file, its inputs and its record: the
    view a re-assessment or a test of a check reads."""
    outcome = load(state, run_id)
    directory = run_dir(state, run_id)
    scenario = Scenario.model_validate_json((directory / SCENARIO).read_text(encoding="utf-8"))
    agent = AgentUnderTest.model_validate_json((directory / AGENT).read_text(encoding="utf-8"))
    registry = Registry.installed()
    judge = _Judge(
        scenario,
        None,
        judging=False,
        rules=rules_for(agent, scenario),
        fail_on_integrity=integrity_fails(agent.fail_on_integrity, scenario.fail_on_integrity),
        claims=_Claims(registry, frozenset()),
        rhythm=declared_rhythm(agent),
        collections=declared_collections(agent),
    )
    world = SqliteStore(_root_dir(state, outcome.record) / WORLD, run_id, RunClock(scenario.starts_at))
    try:
        return judge.view(outcome.record, world)
    finally:
        world.close()


def runs(state: Path) -> list[Outcome]:
    """Every finished run under `state`, oldest first by the time its record was written."""
    base = state / RUNS
    if not base.is_dir():
        return []
    found = sorted((d for d in base.iterdir() if (d / RECORD).is_file()), key=lambda d: (d / RECORD).stat().st_mtime)
    return [load(state, d.name) for d in found]


KEPT = "world.json"
"""In a standing world's run directory: what marks it as one (`serve.Kept`)."""


def driven(state: Path, run_id: str) -> bool:
    """Whether the run was driven from outside (`minutehand serve`: a standing world, or a case of them), so its
    wakes are steps someone marked or the clock implied, not wakes Minutehand sent."""
    directory = run_dir(state, run_id)
    return (directory / KEPT).is_file() or (directory / CASE).is_file()


def case_of(state: Path, run_id: str) -> CaseKept | None:
    """The case a run directory is, when it is one (`application.cases`)."""
    path = run_dir(state, run_id) / CASE
    return CaseKept.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


def case_members(state: Path) -> dict[str, str]:
    """Every world that belongs to a case, with the case's id: read through its case, never listed alone."""
    base = state / RUNS
    found: dict[str, str] = {}
    for path in sorted(base.glob(f"*/{CASE}")) if base.is_dir() else []:
        kept = CaseKept.model_validate_json(path.read_text(encoding="utf-8"))
        found.update({w: kept.case_id for w in kept.worlds})
    return found


def probe(outcome: Outcome) -> bool:
    """A closed standing world no call ever reached: one a harness opened to look and left, not a run to read."""
    record = outcome.record
    return (
        record.stop is StopReason.CLOSED
        and not record.worlds
        and not record.providers
        and not record.outbound
        and not record.emulators
    )


def listed(state: Path) -> list[Outcome]:
    """`runs`, as a person lists them: a case once, never its worlds one by one, and no probe world."""
    members = case_members(state)
    return [o for o in runs(state) if o.record.run_id not in members and not probe(o)]


def fork_points(state: Path, run_id: str) -> list[ForkPoint]:
    """Every point this run can be forked from, in order."""
    with reading(state, run_id) as world:
        return points_in(world)


def points_in(world: Store) -> list[ForkPoint]:
    """The checkpoints a world's log holds, each a point a fork may be taken from, and whether it can be: one the
    agent went on writing its memory after, in the same wake, cannot (`rewind.not_restorable`)."""
    points: list[ForkPoint] = []
    writes = memory_writes(world.events())
    for seq, checkpoint in checkpoints(world).items():
        refused = refused_by(writes, seq, checkpoint)
        points.append(ForkPoint(wake=checkpoint.wake, seq=seq, agent=refused or checkpoint.agent))
    return points


def restore_of(state: Path, run_id: str) -> Restored | None:
    """How a fork's agent was found at its start. None for a run that started from the beginning."""
    path = run_dir(state, run_id) / RESTORE_RECORD
    return Restored.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


def fork_of(state: Path, run_id: str) -> Fork | None:
    """What a fork was asked to change, as kept with it. None for a run that was not forked."""
    path = run_dir(state, run_id) / FORK_RECORD
    return Fork.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None


def fork_account(state: Path, run_id: str) -> ForkAccount | None:
    """A fork, told (`application.forks`): where it split, what it changed, whether its agent was proven, and,
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


def checkpoints_of(state: Path, run_id: str) -> list[ForkPoint]:
    """Every checkpoint of a run, and whether a fork can be taken from it."""
    return fork_points(state, run_id)


def usage_of(state: Path, run_id: str) -> RunUsage:
    """What one run costs on disk: its rows and the bodies it alone holds."""
    with reading(state, run_id) as world:
        return world.usage()


class Collected(Model):
    """What one housekeeping pass removed."""

    freed: Freed = Field(description="Stored bodies nothing referred to, across every world file")
    removed: list[str] = Field(default=[], description="Run directories removed whole, as asked")
    removed_bytes: int = Field(default=0, ge=0)
    swept: int = Field(default=0, ge=0, description="World files swept")
    skipped: list[str] = Field(default=[], description="World files that could not be swept, and why")


def collect(state: Path, *, remove: Sequence[str] = (), settled: Collection[str] = ()) -> Collected:
    """Remove the run directories named in `remove`, then sweep every world file left under `state` of the stored
    bodies nothing refers to, but those of the directories in `settled`: swept already, and written by nothing
    since. `minutehand gc`, `minutehand rm`, and a standing server's retention of closed worlds, are this."""
    removed_bytes = 0
    for run_id in remove:
        directory = run_dir(state, run_id)
        removed_bytes += sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
        shutil.rmtree(directory, ignore_errors=True)
    totals = Freed(bodies=0, body_bytes=0)
    swept = 0
    skipped: list[str] = []
    base = state / RUNS
    for directory in sorted(base.iterdir()) if base.is_dir() else []:
        if directory.name in settled:
            continue
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
        totals = Freed(bodies=totals.bodies + freed.bodies, body_bytes=totals.body_bytes + freed.body_bytes)
    return Collected(freed=totals, removed=list(remove), removed_bytes=removed_bytes, swept=swept, skipped=skipped)


def remove(state: Path, run_ids: Sequence[str]) -> Collected:
    """`minutehand rm`: each run named, with every fork of it, since they share its world file; then `collect`.
    A fork alone is refused: its record is part of its root's world file, which is removed with the root."""
    whole: list[str] = []
    for run_id in run_ids:
        entry = find(state, run_id)
        if entry.root != run_id:
            raise RunRefused(
                f"run {run_id} is a fork of run {entry.root} and is kept in its world file: remove {entry.root}, "
                "which removes it with every other fork of that run"
            )
        whole += [r for r, _, _ in _ReadOnlyStore.runs_in(run_dir(state, run_id) / WORLD) if r not in whole]
    return collect(state, remove=whole)


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
    case = case_of(state, entry.root)
    if case is not None:
        with _reading_case(state, case) as merged:
            yield merged
        return
    store = _ReadOnlyStore(run_dir(state, entry.root) / WORLD, run_id, RunClock(datetime.fromtimestamp(0, UTC)))
    try:
        yield store
    finally:
        store.close()


@contextmanager
def _reading_case(state: Path, case: CaseKept) -> Iterator[Store]:
    """A case as one run: each of its worlds' files and its own, read as one log (`application.cases.CaseStore`)."""
    epoch = RunClock(datetime.fromtimestamp(0, UTC))
    with ExitStack() as stack:
        parts: list[Store] = []
        for world_id in [*case.worlds, case.case_id]:
            path = run_dir(state, world_id) / WORLD
            if path.is_file():
                store = _ReadOnlyStore(path, world_id, epoch)
                stack.callback(store.close)
                parts.append(store)
        yield CaseStore(case.case_id, parts)


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


def agent_of(state: Path, run_id: str) -> AgentUnderTest | None:
    """The agent file as this run played it, or its parent's for a fork still running; None for a run no agent file
    was written for (a standing world, a case)."""
    entry = find(state, run_id)
    while True:
        path = run_dir(state, entry.run_id) / AGENT
        if path.is_file():
            return AgentUnderTest.model_validate_json(path.read_text(encoding="utf-8"))
        if entry.parent_run is None:
            return None
        entry = find(state, entry.parent_run)


def wakes_of(state: Path, run_id: str, world: Store) -> list[WakeRecord]:
    """The run's wakes: from its record once it has finished, and from the checkpoints in its log while it
    runs. A wake still in progress has no checkpoint and is not listed. Whether a running wake changed the
    agent's commitments is not in the log, so those records say it did not."""
    if (run_dir(state, run_id) / RECORD).is_file():
        return load(state, run_id).record.wakes
    stepped = steps(world)
    if stepped:
        return stepped  # a standing world, or a case: its steps are its wakes
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


def reported_of(world: Store) -> list[CommitmentsReported] | None:
    """The agent's commitments as each wake ended, read from the checkpoints; None when it never reported any."""
    said = [
        CommitmentsReported(wake=c.wake, at=c.now, commitments=c.commitments)
        for c in checkpoints(world).values()
        if c.commitments is not None
    ]
    return said or None


def _contained(agent: AgentUnderTest) -> bool:
    return any(isinstance(w, Contained) for w in agent.wakes)


def _refuse_unmodeled(listen: Listen, model: LanguageModel | None) -> None:
    """`--capture-unknown model` with no model configured would refuse every write it means to answer."""
    if listen.capture_unknown is UnknownHosts.MODEL and model is None:
        raise RunRefused(
            "--capture-unknown model needs a model to stand in for undeclared services: set MINUTEHAND_MODEL and "
            "MINUTEHAND_MODEL_API_KEY, and MINUTEHAND_MODEL_BASE_URL for a service other than OpenAI's"
        )


def rules_for(agent: AgentUnderTest, scenario: Scenario, own: Sequence[Check] = ()) -> list[Rule]:
    """The team's rules the run is judged by (`domain.assessments.merged`), refused before anything starts when the
    scenario switches off a rule nobody wrote, a rule names someone the scenario does not have or a pattern there is
    not, or a rule takes the id of a check that also judges the run."""
    try:
        rules = merged(agent.assess, scenario.assess, scenario.assess_off)
        refuse_unknown_people(rules, [p.key for p in scenario.people])
    except ValueError as e:
        raise RunRefused(f"the assessments: {e}") from e
    unknown = sorted({r.pattern for r in rules if r.pattern is not None} - {p.key for p in PATTERNS})
    if unknown:
        raise RunRefused(f"the assessments: no pattern {', '.join(unknown)}; the patterns are in docs/patterns/")
    taken = sorted({r.id for r in rules} & finding_names(own))
    if taken:
        raise RunRefused(f"the assessments: a rule takes the id of a check: {', '.join(taken)}; rename the rule")
    return rules


def _own_checks(agent: AgentUnderTest) -> list[Check]:
    """The agent's own checks, loaded before anything of a run starts, so a file that cannot load refuses it."""
    try:
        return load_checks(agent.checks)
    except ChecksRefused as e:
        raise RunRefused(f"the agent's checks: {e}") from e


@dataclass(frozen=True)
class _Claims:
    """Which provider claims a host, and the providers the run names: what tells a call that went around the proxy
    (`application.around_proxy`)."""

    registry: Registry
    named: frozenset[ProviderKey]

    def provider(self, host: str) -> ProviderKey | None:
        found = self.registry.claimant(host)
        return found.key if found is not None else None


def _claims(registry: Registry, services: Services) -> _Claims:
    return _Claims(registry, frozenset(p.manifest.key for p in services.providers))


def declared_collections(agent: AgentUnderTest) -> list[DeclaredCollection]:
    """Every collection the agent file's `store` hosts declare, as the simulation's health names them."""
    return [
        DeclaredCollection(host=d.host, collection=c.key)
        for d in agent.outbound
        if isinstance(d, DeclaredStore)
        for c in d.collections
    ]


class _Judge:
    """`application.orchestrator.Scorer`: the run's view built from the world, and every check run over it;
    with `judging`, the judged checks too, by `model` or blocked for want of one. With `claims`, the calls the agent
    made around the proxy are counted too."""

    def __init__(
        self,
        scenario: Scenario,
        model: LanguageModel | None,
        *,
        judging: bool,
        own: Sequence[Check] = (),
        rules: Sequence[Rule] = (),
        fail_on_integrity: Sequence[IntegrityCheck] = (),
        claims: _Claims | None = None,
        rhythm: timedelta | None = None,
        collections: Sequence[DeclaredCollection] = (),
    ) -> None:
        self._claims = claims
        self._rhythm = rhythm
        self._collections = list(collections)
        self._rules = list(rules)
        self._fail_on_integrity = list(fail_on_integrity)
        self._scenario = scenario
        self._own = list(own)
        self._model = model
        self._judging = judging
        self.results: dict[str, RunResult] = {}
        self.receiver: Receiver | None = None
        """Whose notices about telemetry it could not receive end the run's findings."""
        self.own_files: OwnDatabases | None = None
        """The agent's own databases for the run being judged, which its notes name as outside forks."""

    def _manifests(self) -> list[Manifest]:
        return self._claims.registry.manifests if self._claims is not None else []

    def _typed(self, world: Store) -> list[TypedItem]:
        """The run's writes read as items of their kinds, each by the provider that holds it."""
        events = world.events()
        manifests = {m.key: m for m in self._manifests()}
        typers: dict[ProviderKey, TypesItems] = {}
        if self._claims is not None:
            for key in sorted({e.entity.provider for e in events} & set(manifests)):
                provider = self._claims.registry.provider(manifests[key])
                if isinstance(provider, TypesItems):
                    typers[key] = provider
        return typed_items(events, self._scenario, manifests, typers, world, world.calls())

    def view(self, record: RunRecord, world: Store) -> RunView:
        """What every check reads of the finished run."""
        last = read_checkpoint(world)
        calls = world.calls()
        spans = world.spans()
        claims = self._claims
        return view_of(
            self._scenario,
            world.events(),
            record.wakes,
            world.replies(),
            commitments=last.commitments if last is not None else None,
            unmatched_calls=[call.exchange for call in calls if call.refused],
            model_calls=per_wake(spans, [w.index for w in record.wakes]),
            broken_calls=broken(calls),
            contract_breaks=contract_breaks(calls),
            dues=due_entries(world),
            reported=reported_of(world),
            around_proxy=around_proxy(spans, calls, claims.provider) if claims is not None else None,
            uncalled_providers=(
                uncalled_providers(claims.named, calls, woken=bool(record.wakes)) if claims is not None else []
            ),
            rules=self._rules,
            fail_on_integrity=self._fail_on_integrity,
            stop=record.stop,
            calls=calls,
            typed=self._typed(world),
            item_types=provided_types(self._manifests()),
            rhythm=self._rhythm,
            person_calls=world.person_calls(),
            collections=self._collections,
        )

    async def score(self, record: RunRecord, world: Store) -> RunResult:
        view = self.view(record, world)
        model = self._model

        def kept(check: JudgedCheck) -> LanguageModel:
            assert model is not None
            return KeptModel(
                model,
                world,
                wrote=check.wrote,
                prompt_version=check.prompt_version,
                sim_time=record.ended_at,
                wake=len(record.wakes),
            )

        result = (
            await evaluate_judged(view, model, stop=record.stop, own=self._own, kept=kept)
            if self._judging
            else evaluate(view, stop=record.stop, own=self._own)
        )
        result = result.model_copy(
            update={
                "findings": [*result.findings, *emulator_findings(world)],
                "notes": [*result.notes, *(self.own_files.notes() if self.own_files is not None else [])],
            }
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


def desk_for(
    scenario: Scenario, agent: AgentUnderTest | None, model: LanguageModel | None, *, undeclared: bool = False
) -> ServiceDesk | None:
    """The scenario's declared services (`docs/services.md`), with each one's OpenAPI document read once, from its
    file or its URL, and, with `undeclared` (`--capture-unknown model`), every host nobody declared answered as a
    service with no description; None when there is neither. Refused when a service's host is also one of the
    agent's outbound hosts."""
    if not scenario.services and not undeclared:
        return None
    declared = {d.host for d in agent.outbound} if agent is not None else set()
    clashing = sorted(s.host for s in scenario.services if s.host in declared)
    if clashing:
        raise RunRefused(
            f"{', '.join(clashing)} is declared both as a service of the scenario and as an outbound host of the agent "
            "file: declare it once"
        )
    documents: dict[str, JsonValue] = {}
    for service in scenario.services:
        if service.openapi is None:
            continue
        try:
            if service.openapi.startswith(("http://", "https://")):
                fetched = httpx.get(service.openapi, timeout=30, follow_redirects=True)
                fetched.raise_for_status()
                documents[service.key] = json.loads(json.dumps(read_yaml(fetched.text, service.openapi), default=str))
            else:
                documents[service.key] = load_document(service.openapi)
        except (httpx.HTTPError, FileRefused) as e:
            raise RunRefused(f"service {service.key}: its OpenAPI document {service.openapi}: {e}") from e
    return ServiceDesk(scenario, model, documents=documents, pushes=HttpPushes(), undeclared=undeclared)


def _refuse_unwritten(scenario: Scenario, agent: AgentUnderTest, model: LanguageModel | None) -> None:
    """A person whose words a model writes (conversing, a script step's words, a decision's reasons) needs a model;
    without one the run is refused before it starts, naming each and why."""
    needing = [*unspoken(scenario, agent.inboxes), *needs_model(scenario), *unvoiced(scenario)]
    if needing and model is None:
        raise RunRefused(
            f"a model writes what {'; '.join(needing)} say, and no model is configured: set {MODEL_VARIABLE} and "
            f"{API_KEY_VARIABLE}, and {BASE_URL_VARIABLE} for a service other than OpenAI's"
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
    named |= set(scenario.transitions_on)
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
    schedulers: dict[ProviderKey, BooksWakes] = {}
    for provider in providers:
        key = provider.manifest.key
        if provider.manifest.pushes_events:
            if not isinstance(provider, PushesEvents):
                raise RunRefused(f"provider {key} declares pushes_events and does not implement PushesEvents")
            pushes[key] = provider
        if provider.manifest.books_wakes or provider.manifest.books_work:
            if not isinstance(provider, BooksWakes):
                raise RunRefused(f"provider {key} declares books_wakes or books_work and does not implement BooksWakes")
            schedulers[key] = provider
        socket_mode = any(t.provider == key and t.delivery is Delivery.SOCKET_MODE for t in agent.inbound)
        if socket_mode and not isinstance(provider, ServesSockets):
            raise RunRefused(f"agent {agent.name} takes {key}'s events in socket mode, and {key} serves no sockets")
    return Services(providers=providers, pushes=pushes, schedulers=schedulers)


@dataclass(frozen=True)
class Signing:
    """The secrets a run signs pushed events with: one per provider, one per captured host whose replies are
    signed, and the ones the agent's command is given."""

    by_provider: dict[ProviderKey, str]
    for_agent: dict[str, str]
    by_host: dict[ProviderKey, str]
    by_person: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    """Each person's `Person.credential` in the agent's own product, by `Person.key`: never stored."""


def replies_for(agent: AgentUnderTest, scenario: Scenario, signing: Signing) -> dict[ProviderKey, TakesReplies]:
    """A channel per captured host whose sends people can answer, keyed by the name its messages go under."""
    channels: dict[ProviderKey, TakesReplies] = {}
    for declared in agent.outbound:
        if isinstance(declared, Acknowledge) and declared.replies is not None:
            secret = signing.by_host[declared.key] if declared.key in signing.by_host else None
            channels[declared.key] = CapturedReplies(declared, scenario.people, secret=secret)
    return channels


def inboxes_for(agent: AgentUnderTest, scenario: Scenario, signing: Signing) -> Inboxes | None:
    """Every inbox the agent declares in its own product, reached as the scenario's people; None when it declares
    none."""
    if not agent.inboxes:
        return None
    try:
        return Inboxes(scenario, [HttpInboxReach(declared, signing.by_person) for declared in agent.inboxes])
    except OperationUnresolved as e:
        raise RunRefused(f"agent {agent.name}'s inboxes: {e}") from e


def signing_for(agent: AgentUnderTest, people: Sequence[Person] = ()) -> Signing:
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
    by_person = {
        person.key: resolve(person.credential, f"{person.key} credential")
        for person in people
        if person.credential is not None and agent.inboxes
    }
    return Signing(by_provider=by_provider, for_agent=for_agent, by_host=by_host, by_person=by_person)


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
    capture_unknown: UnknownHosts = Field(
        default=UnknownHosts.REFUSE,
        description="Which calls to a host no provider claims and no declaration names are passed through and kept "
        "rather than refused: none, reads (GET, HEAD, OPTIONS), or all, the first run of an agent, to see what it calls",
    )
    upstream_ca: Path | None = Field(
        default=None,
        description="The CAs a real host is verified against when a call is passed through, edited or recorded; "
        "None trusts the system's",
    )
    transparent_port: int | None = Field(
        default=None,
        ge=0,
        le=65535,
        description="A second listener, on `host`, for connections the agent's container redirects to the proxy "
        "without asking for one (`adapters.proxy.redirected`): a client that ignores HTTPS_PROXY is captured all the "
        "same. None serves none; 0 lets the system pick one",
    )
    model_hosts: list[str] = Field(
        default=list(DEFAULT_MODEL_HOSTS),
        description="Hosts that are model APIs: tunnelled, or opened to edit or record their calls. The three "
        "public ones by default; a self-hosted or other provider's API is added here",
    )

    def reached_at(self) -> str:
        """This machine as the agent names it. Binding every interface is not an address: it is reached on loopback."""
        return self.agent_host or ("127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host)

    def proxy_url(self, port: int) -> str:
        """The proxy as the agent reaches it."""
        return f"http://{self.reached_at()}:{port}"

    def telemetry_url(self, port: int) -> str:
        """The telemetry receiver as the agent reaches it."""
        return f"http://{self.reached_at()}:{port}"

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
        if not loopback(self.reached_at()):
            hosts.append(self.reached_at())
        if self.elsewhere():
            hosts.append(LOOPBACK_NAME)
        return list(dict.fromkeys(hosts))


DIRECT = ("127.0.0.1",)
"""What every agent reaches directly: this machine by address. Never a name, which requests, urllib, aiohttp and curl
read as covering every name under it, and never `::1`, which requests reads as a suffix of any IPv6 literal
(`2001:db8::1`). An IPv4 address is matched exactly by every client (docs/design.md, "What the agent reaches
directly")."""


def agent_environment(
    listen: Listen,
    port: int,
    ca_bundle: str | Path,
    secrets: Mapping[str, str],
    *,
    telemetry_port: int | None,
    agent_url: str | None = None,
) -> dict[str, str]:
    """What the agent's process needs to reach the fakes and trust them, and nothing else: the proxy in both
    spellings libraries read, the hosts it reaches directly (also as `no_grpc_proxy`: gRPC applies `http_proxy`
    even to an insecure channel to an in-stack emulator), Node's switch that makes its built-in `fetch` read them
    (`NODE_PROXY`), the one CA file in each library's variable
    (`ca_bundle`, as the agent sees the path), the signing secrets it is handed, unless `telemetry_port`
    is None (receiving is off) its OTLP exporter pointed at the receiver, and, when `agent_url` is given,
    MINUTEHAND_ON and the URL `minutehand.agent` reaches the run at (`AGENT_URL`)."""
    proxy = listen.proxy_url(port)
    direct = ",".join(listen.direct())
    exporter = exporter_environment(listen.telemetry_url(telemetry_port)) if telemetry_port is not None else {}
    if telemetry_port is not None:
        exporter = {**exporter, MCP_URL_ENV: listen.telemetry_url(telemetry_port) + MCP_PATH}
    return {
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "NO_PROXY": direct,
        "https_proxy": proxy,
        "http_proxy": proxy,
        "no_proxy": direct,
        "no_grpc_proxy": direct,
        NODE_PROXY: "1",
        **{name: str(ca_bundle) for name in CA_VARIABLES},
        **exporter,
        **({AGENT_ON: "1", AGENT_URL: agent_url} if agent_url is not None else {}),
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
    if listen.transparent_port == 0:
        raise RunRefused(
            "an agent configured before the run needs the redirected listener on a fixed port: give "
            "--transparent-port a port"
        )
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
        listen,
        listen.port,
        ca_bundle or str(bundle.resolve()),
        {},
        telemetry_port=telemetry_port,
        agent_url=listen.telemetry_url(listen.telemetry_port) + AGENT_PATH if listen.telemetry_port else None,
    )
    if listen.telemetry_port == 0:
        # No receiver port to name: the agent is still told it is played, so its store refuses rather than writing
        # to its own database (`minutehand.agent._wire`), and says which port to give.
        handed[AGENT_ON] = "1"
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
    receiver: Receiver
    telemetry: bool = True
    """Whether the agent's telemetry is received: its exporter pointed at the receiver. The receiver runs either way,
    since it holds the agent's memory."""

    @property
    def port(self) -> int:
        return self.proxy.port

    @property
    def ca_bundle(self) -> Path:
        return self.proxy.ca_bundle

    @property
    def telemetry_port(self) -> int | None:
        """The receiver's port for the agent's telemetry; None when receiving it is off."""
        return self.receiver.port if self.telemetry else None

    def agent_url(self, listen: Listen) -> str:
        """Where `minutehand.agent` reaches the run, as the agent reaches this machine."""
        return listen.telemetry_url(self.receiver.port) + AGENT_PATH

    def hold(self) -> None:
        """Hold the agent's calls to its memory until the next run is mounted (`Receiver.hold`)."""
        self.receiver.hold()

    def last_call(self) -> SeenCall | None:
        """`application.traffic.Traffic`: the latest call the proxy saw from the agent."""
        return self.proxy.last_call()

    def waiting(self) -> list[str]:
        """`application.traffic.Traffic`: what the agent sent and the proxy has not seen answered."""
        return self.proxy.waiting()

    def flush(self) -> None:
        self.proxy.flush()

    def mount(
        self,
        world: Store,
        clock: Clock,
        apps: Mapping[ProviderKey, ASGIApp],
        *,
        scenario: Scenario,
        holds: HeldCalls | None = None,
    ) -> None:
        self.proxy.mount(world, clock, apps, scenario=scenario, holds=holds)
        self.receiver.mount(world, clock)

    def memory_reads(self, wake: int) -> int:
        return self.receiver.memory_reads(wake)


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
    """The proxy and the receiver, on the same host. The receiver holds the agent's memory and, unless `listen`
    turns it off, takes its telemetry, passing it on to wherever this process's own environment sent OTLP before
    (`Forwarding.from_environment`).
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
        redirect_port=listen.transparent_port,
    ) as proxy:
        receiver = Receiver(
            store,
            clock,
            host=listen.host,
            port=listen.telemetry_port,
            forwarding=Forwarding.from_environment(os.environ),
            agent_host=listen.agent_host,
        )
        async with receiver:
            yield Intercepting(proxy, receiver, telemetry=listen.receive_telemetry)


@asynccontextmanager
async def emulating(
    agent: AgentUnderTest,
    intercepted: Intercepting,
    listen: Listen,
    logs: Path,
    telemetry: Telemetry | None,
    routes: dict[str, Running],
) -> AsyncIterator[Emulators]:
    """The agent file's external emulators, started (or attached to) and ready before the agent starts, each given
    the OTLP variables the agent is (its spans join the agent's trace through the forwarded `traceparent`), and
    stopped when the run ends. Each health change is recorded in the world the proxy records into, as it happens,
    and exported. One that does not come up refuses the run, with the end of its log."""
    receiver = intercepted.receiver
    environment = exporter_environment(listen.telemetry_url(receiver.port)) if receiver is not None else {}

    def changed(change: EmulatorChange) -> None:
        record_health(intercepted.proxy.addon.worlds.lobby.store, change)
        if telemetry is not None:
            telemetry.emulator_changed(change)

    emulators = Emulators(logs, environment, changed, running=routes)
    await emulators.start(agent.emulators)
    try:
        yield emulators
    finally:
        await emulators.stop()


def _listens_on(agent: AgentUnderTest) -> str | None:
    """The URL whose port tells that the agent is up: its wake endpoint, or else where it takes events."""
    for source in agent.wakes:
        if isinstance(source, Reported | Marked | Polled):
            return source.wake_url
    return next((t.url for t in agent.inbound if t.url is not None), None)


def _tail(log: Path) -> str:
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    return text.strip()[-1500:] or "(it printed nothing)"


OWN = "own"
"""The folder of a run's directory that holds the agent's own databases (`AgentUnderTest.own_databases`)."""


class OwnDatabases:
    """The databases the agent keeps state in beside its memory (`AgentUnderTest.own_databases`): a fresh empty SQLite
    file per variable for each run, handed to the agent in that variable. `application.orchestrator.OutsideState`:
    at each checkpoint it says which of them the agent has written to, since a fork does not get it back."""

    def __init__(self, agent: AgentUnderTest, directory: Path) -> None:
        self._declared = list(agent.own_databases)
        self._directory = directory / OWN
        self._files = {d.env: self._directory / f"{d.env}.sqlite" for d in self._declared}
        if self._declared:
            shutil.rmtree(self._directory, ignore_errors=True)  # fresh and empty, whatever a run before left here
            self._directory.mkdir(parents=True)

    def environment(self) -> dict[str, str]:
        return {
            d.env: (
                str(self._files[d.env])
                if d.form is OwnDatabaseForm.FILE
                else f"sqlite:///{self._files[d.env].resolve()}"
            )
            for d in self._declared
        }

    def outside(self) -> list[str]:
        held: list[str] = []
        for env, path in self._files.items():
            size = sum(f.stat().st_size for f in (path, path.with_name(path.name + "-wal")) if f.is_file())
            if size:
                held.append(f"the agent's own database in {env} held {size:,} bytes")
        return held

    def notes(self) -> list[str]:
        return [
            f"{env} was handed a fresh empty SQLite file for this run ({path}): what the agent keeps there is "
            "outside its memory, so it is not part of the run's record, and a fork starts with it empty, not as it "
            "stood at the checkpoint"
            for env, path in self._files.items()
        ]


class _Program:
    """The agent's own program, started by Minutehand: `application.restore.OwnProgram`, so a fork can start it once
    its run exists, with the same environment."""

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
                # a group of its own, so stopping it stops whatever it started too
                self._process = await asyncio.create_subprocess_exec(
                    *self._command,
                    env=self._env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=out,
                    stderr=out,
                    start_new_session=True,
                )
            except OSError as e:
                raise RunRefused(f"the agent's command {self._command[0]} could not be started: {e}") from e
        url = _listens_on(self._agent)
        if url is not None:
            await _until_listening(self._process, url, self._log)

    def exited(self) -> int | None:
        return self._process.returncode if self._process is not None else None

    async def stop(self) -> None:
        """Stop the command and every process it started. A child the command was still waiting on when the
        timeout ran out would otherwise outlive it, still listening on the agent's port, and answer for the agent
        the next run starts."""
        process, self._process = self._process, None
        if process is None:
            return
        _signal_group(process.pid, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
        _signal_group(process.pid, signal.SIGKILL)  # whatever is left of the group, the command itself included
        await process.wait()


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):  # the group is already gone
        os.killpg(pgid, sig)


@asynccontextmanager
async def _agent_process(
    command: Sequence[str] | None,
    env: Mapping[str, str],
    agent: AgentUnderTest,
    log: Path,
    *,
    started: bool = True,
) -> AsyncIterator[_Program | None]:
    """The agent's program, started before the block unless `started` is False (a fork starts it itself, once its
    run exists), and stopped after it."""
    if not command:
        yield None
        return
    program = _Program(command, env, agent, log)
    try:
        if started:
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
