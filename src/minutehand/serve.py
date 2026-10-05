"""The standing mode: one long-lived process holding many worlds at once, with a control API, for test suites
that seed a fake, call their own services, and inspect what the fake saw.

    minutehand serve [--state DIR] [--host H] [--proxy-port 8080] [--control-port 8081] [--telemetry-port 4318]

One proxy, one OTLP receiver and one control API, each on its own port; every installed provider available,
built on its first call. No scenario, no run loop, no agent wakes.

Which world a call belongs to (`Standing.world_for`), in order: the host it went to, when a world claims that
host; else the first credential it carries that a world claims (`adapters.proxy.credentials`); else the
default world, when one is open; else none: it is refused with 502 and kept in the lobby, where
`GET /v1/unmatched` reads it. A span the services export is kept in the world whose calls carried its trace,
and in the lobby when none did.

A model API is decided by its host before its call is opened, so it is never decided per world by credentials:
the three public ones and every `--model-host` are model hosts for every world, tunnelled, or opened and kept as
spans under `--record-model-calls`. A world may declare more (`CreateWorld.model_hosts`), each tunnelled or
recorded as it says; such a host belongs to that world while it is open. A recorded call is kept in the world
that declared its host, else in the world whose calls carried its trace, else in the lobby.

Each world is a run in the state directory, so `minutehand findings`, `view` and the MCP tools read it:

    <state>/runs/<world_id>/world.db       its log, as any run's
    <state>/runs/<world_id>/scenario.json  the seed as the world plays it
    <state>/runs/<world_id>/world.json     `Kept`: its name and claims, which marks it as a standing world
    <state>/runs/<world_id>/record.json    once closed: `RunRecord`, stopped CLOSED
    <state>/runs/<world_id>/result.json    once closed: the checks over it as it was closed
    <state>/runs/<world_id>/resets/<n>.db  its log before its n-th reset, kept: a reset never discards the record
    <state>/runs/<lobby_id>/world.db       the calls no world claimed, and spans of traces none carried

Closing a world keeps the last `keep` closed worlds and removes the directories of older ones, then sweeps every
world file left of bodies and snapshot files nothing refers to (`session.collect`, which `minutehand gc` runs too).
A world still open when the server stops is closed then.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from pydantic import Field
from starlette.applications import Starlette

from minutehand.adapters.answering import injected
from minutehand.adapters.control.wire import Claims, CreateWorld, Fault, FurtherSeed, ProviderView, Quiet, Quieted
from minutehand.adapters.emulator.fleet import Emulators
from minutehand.adapters.emulator.process import EmulatorRefused
from minutehand.adapters.proxy.capture import Capturing, refuse_claimed, replaying_for, write_recordings
from minutehand.adapters.proxy.policy import DEFAULT_MODEL_HOSTS, Routing
from minutehand.adapters.proxy.registry import ProviderConflict, Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.proxy.worlds import Mounted
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.telemetry.forward import Forwarding
from minutehand.adapters.telemetry.receiver import Receiver, exporter_environment
from minutehand.application.cases import CASE, CaseKept, CaseStore, merged
from minutehand.application.emulators import findings as emulator_findings
from minutehand.application.emulators import record_health
from minutehand.application.outbound import emulator_uses, outbound_uses
from minutehand.application.refusals import RunRefused, refuse_unheld
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import (
    FIRST_WAKE,
    Fired,
    StandingWorld,
    UnknownCase,
    UnknownWorld,
    Unsupported,
    WorldRefused,
    score,
)
from minutehand.application.steps import Stepping, steps
from minutehand.checks.runner import RunResult
from minutehand.domain.emulator import EmulatorChange
from minutehand.domain.provider import Manifest
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Model, ProviderKey, Scenario
from minutehand.domain.telemetry import ReceivedSpan
from minutehand.domain.world import CallOutcome, Exchange
from minutehand.ports.clock import Clock
from minutehand.ports.provider import (
    ASGIApp,
    ChangesPeople,
    DeclaresFaults,
    DeletesTickets,
    GrantsPermissions,
    Message,
    MintsInboundCredentials,
    OwnsSeed,
    Scope,
)
from minutehand.ports.store import Store
from minutehand.session import (
    RECORD,
    RESULT,
    RUNS,
    SCENARIO,
    WORLD,
    Collected,
    Listen,
    agent_environment,
    collect,
    reading_file,
    run_dir,
    scenario_of,
)

KEPT = "world.json"
LOBBY = "lobby"
RESETS = "resets"
"""Where a standing world keeps its log from before each reset, `<n>.db` for the n-th, oldest first."""

DEFAULT_PROXY_PORT = 8080
DEFAULT_CONTROL_PORT = 8081
DEFAULT_TELEMETRY_PORT = 4318
DEFAULT_KEEP = 100
QUIET_POLL = 0.02
"""Seconds between two looks at whether a world has gone quiet."""


class Kept(Model):
    """What marks a run directory as a standing world's, and what it was opened with besides its seed."""

    world_id: str
    name: str
    claims: Claims
    scripted_people: bool
    case_id: str | None = Field(default=None, description="The case it belongs to; None when opened under no label")
    case: str | None = Field(default=None, description="The case label it was opened under")


class ServeOptions(Model):
    host: str = Field(default="127.0.0.1", description="Where all three listen; 0.0.0.0 for containers")
    proxy_port: int = Field(default=DEFAULT_PROXY_PORT, ge=0, le=65535)
    control_port: int = Field(default=DEFAULT_CONTROL_PORT, ge=0, le=65535)
    telemetry_port: int = Field(default=DEFAULT_TELEMETRY_PORT, ge=0, le=65535)
    receive_telemetry: bool = True
    agent_host: str | None = Field(
        default=None, description="The name services use for this server (a Compose service name); default the host"
    )
    no_proxy: list[str] = Field(default=[], description="Hosts services reach directly")
    keep: int = Field(default=DEFAULT_KEEP, ge=0, description="Closed worlds kept; older ones are removed")
    capture_unknown: bool = Field(
        default=False, description="Pass through and keep a call to a host nobody claims or declares, not refuse it"
    )
    upstream_ca: Path | None = Field(
        default=None, description="The CAs a real host is verified against when a call is passed through"
    )
    model_hosts: list[str] = Field(
        default=[],
        description="Model APIs besides api.openai.com, api.anthropic.com and generativelanguage.googleapis.com, "
        "for every world: tunnelled, or recorded with `record_model_calls`",
    )
    record_model_calls: bool = Field(
        default=False, description="Open every world's calls to model APIs, send them on unchanged, keep each as a span"
    )


@dataclass
class _Armed:
    fault: Fault
    left: int


@dataclass
class World:
    world_id: str
    name: str
    spec: CreateWorld
    standing: StandingWorld
    store: SqliteStore
    mounted: Mounted
    opened: float
    faults: list[_Armed] = field(default_factory=list)
    traces: set[str] = field(default_factory=set)
    open: bool = True
    signing: dict[ProviderKey, str] = field(default_factory=dict)
    capturing: Capturing = field(default_factory=Capturing)
    resets: int = 0
    case: Case | None = None
    stepping: Stepping | None = None
    """Its own steps, for a world opened under no case label; a case's world is stepped by its case."""

    @property
    def steps(self) -> Stepping:
        if self.case is not None:
            return self.case.stepping
        assert self.stepping is not None
        return self.stepping


@dataclass
class Case:
    """Worlds opened under one case label while any of them is open: one run (`application.cases`). Its own store
    holds its steps, the model traffic its worlds made, and the spans that came with no trace link while it was
    open."""

    case_id: str
    name: str
    store: SqliteStore
    clock: RunClock
    mounted: Mounted
    stepping: Stepping
    began: datetime = field(default_factory=lambda: _real_now())
    """The real moment it opened: with `ended`, the window that places the spans that reach it by time."""
    opened: float = field(default_factory=time.monotonic)
    worlds: list[str] = field(default_factory=list)
    members: dict[str, StandingWorld] = field(default_factory=dict)
    ended: datetime | None = None


def _nothing_relayed(world: Mounted) -> None:
    """Before a proxy routes to the worlds, no tunnel is relayed into any of them."""


def _nothing_in(world: Mounted) -> tuple[float | None, list[str]]:
    """Before a proxy routes to the worlds, no call has been seen in any of them."""
    return None, []


ENDED_KEPT_OPEN = 20
"""How many closed cases keep their store open, for the spans their services export after the close."""


def _no_provider(manifest: Manifest) -> ASGIApp:
    raise RunRefused(f"a case answers no provider: a call to {manifest.key} belongs to one of its worlds")


CLOSED_REMEMBERED = 1000
"""How many closed worlds' claims the server remembers, to tell a late call for one of them from a stray one."""


@dataclass(frozen=True)
class _Closed:
    """What a closed world claimed, so a call that comes for it after it closed is recorded as its late call."""

    world_id: str
    tokens: frozenset[str]
    hosts: frozenset[str]
    keys: frozenset[str]


class Standing:
    """Every world the server holds, and which one each call belongs to: `adapters.proxy.worlds.Worlds`."""

    def __init__(self, state: Path, registry: Registry, *, keep: int, routing: Routing | None = None) -> None:
        self._state = state
        self._registry = registry
        self.routing = routing or Routing(registry)
        self._keep = keep
        self._manifests = {m.key: m for m in registry.manifests}
        self.worlds: dict[str, World] = {}
        self._tokens: dict[str, str] = {}
        self._hosts: dict[str, str] = {}
        self._keys: dict[str, str] = {}
        self._traces: dict[str, str] = {}
        self._models: dict[str, str] = {}
        self._default: str | None = None
        lobby_id = f"{LOBBY}-{secrets.token_hex(6)}"
        directory = run_dir(state, lobby_id)
        directory.mkdir(parents=True)
        self._lobby_clock = RunClock(_now())
        self.lobby_store = SqliteStore(directory / WORLD, lobby_id, self._lobby_clock)
        self._shared = {h.lower(): m for m in registry.manifests for h in m.shared_hosts}
        self._shared_apps: dict[ProviderKey, ASGIApp] = {}
        self._lobby = Mounted(store=self.lobby_store, clock=self._lobby_clock, app_for=self._shared_app)
        self.flush_in: Callable[[Mounted], None] = _nothing_relayed
        """What records the calls still in progress in a world before it is closed or reset: the proxy's
        `ProxyAddon.flush_in`, once it routes to these worlds."""
        self.activity_in: Callable[[Mounted], tuple[float | None, list[str]]] = _nothing_in
        """When a call routed to a world was last seen and which are still in progress: the proxy's
        `ProxyAddon.activity_in`, once it routes to these worlds."""
        self._closed: list[_Closed] = []
        self._handed: set[str] = set()
        self.cases: dict[str, Case] = {}
        """Every open case, by its id: worlds opened under one case label while any of them is open."""
        self._labels: dict[str, str] = {}
        self._ended: list[Case] = []
        """The latest closed cases, their stores still open for spans that arrive after the close
        (`ENDED_KEPT_OPEN`)."""
        self.emulators = Emulators(state / "emulators", {}, self._emulator_changed)
        """Every external emulator a world has declared: one per server, shared by every world declaring it the
        same, started with the first and stopped with the server."""

    def _emulator_changed(self, change: EmulatorChange) -> None:
        """A health change, recorded in every open world that declares the emulator."""
        for world in self.worlds.values():
            if any(e.name == change.emulator for e in world.spec.emulators):
                record_health(world.store, change)

    async def start_emulators(self, spec: CreateWorld) -> None:
        """The emulators `spec` declares, running and ready before the world opens; refused (409) with the end of
        one's log when it does not come up, or when another world runs one of that name declared otherwise."""
        try:
            await self.emulators.start(spec.emulators)
        except EmulatorRefused as e:
            raise WorldRefused(str(e)) from e

    def shared(self, host: str) -> bool:
        """Whether a provider answers `host` the same in every world (`Manifest.shared_hosts`)."""
        return host.lower() in self._shared

    def _shared_app(self, manifest: Manifest) -> ASGIApp:
        """The lobby answers a provider only for its shared hosts (published keys, the same in every world), over the
        lobby's own store; any other call to it here is refused."""
        if manifest.key not in {m.key for m in self._shared.values()}:
            raise RunRefused(f"the lobby answers no provider; a call to {manifest.key} here is refused")
        if manifest.key not in self._shared_apps:
            self._shared_apps[manifest.key] = self._registry.provider(manifest).app(self.lobby_store, self._lobby_clock)
        return self._shared_apps[manifest.key]

    # -- adapters.proxy.worlds.Worlds -------------------------------------------------------------------------

    def world_for(self, host: str, credentials: Sequence[str], keys: Sequence[str]) -> Mounted | None:
        found = self._hosts[host.lower()] if host.lower() in self._hosts else None
        if found is None:
            found = next((self._keys[k.lower()] for k in keys if k.lower() in self._keys), None)
        if found is None:
            found = next((self._tokens[c] for c in credentials if c in self._tokens), None)
        if found is None:
            found = self._default
        if found is None and host.lower() in self._shared:
            return self._lobby
        return self.worlds[found].mounted if found is not None else None

    @property
    def lobby(self) -> Mounted:
        return self._lobby

    def late_for(self, host: str, credentials: Sequence[str], keys: Sequence[str]) -> str | None:
        """The most recently closed world whose host, world key or credential the call carries."""
        lowered = {k.lower() for k in keys}
        for closed in reversed(self._closed):
            if host.lower() in closed.hosts or lowered & closed.keys or set(credentials) & closed.tokens:
                return closed.world_id
        return None

    def answered(self, world: Mounted, exchange: Exchange, minted: Sequence[str]) -> None:
        owner = next((w for w in self.worlds.values() if w.mounted is world), None)
        if owner is None:
            return
        for token in minted:
            if token not in self._tokens:
                self._tokens[token] = owner.world_id
        if exchange.traceparent is not None:
            parts = exchange.traceparent.split("-")
            if len(parts) == 4 and parts[1] not in self._traces:
                self._traces[parts[1]] = owner.world_id
                owner.traces.add(parts[1])

    def keeping(self, host: str, trace_id: str | None) -> Mounted:
        """Where a model host's call is kept: the world that declared the host, else the world whose calls carried
        its trace, else the lobby. A world of a case keeps none: the model traffic its services make belongs to the
        case, not to one provider's world, and is kept in the case's own store."""
        declared = self.routing.declared(host)
        if declared is not None and declared in self._models:
            return self._model_traffic(self.worlds[self._models[declared]])
        if trace_id is not None and trace_id in self._traces:
            return self._model_traffic(self.worlds[self._traces[trace_id]])
        return self._lobby

    @staticmethod
    def _model_traffic(world: World) -> Mounted:
        return world.case.mounted if world.case is not None else world.mounted

    def by_trace(self, trace_id: str) -> Store | None:
        """The world whose calls carried this trace."""
        return self.worlds[self._traces[trace_id]].store if trace_id in self._traces else None

    def by_span(self, span: ReceivedSpan) -> Store | None:
        """`Receiver.route`: the world whose calls carried the span's trace; else, for a span with no trace link
        to any call, the one case whose real-time window (opened to closed, or now) holds the span's start, where
        it is placed in the step whose window holds it; else None, the lobby. Two cases open at once both holding
        it is ambiguous, and it stays in the lobby."""
        found = self.by_trace(span.trace_id)
        if found is not None:
            return found
        holding = [
            c
            for c in [*self.cases.values(), *self._ended]
            if c.began <= span.start and (c.ended is None or span.start <= c.ended)
        ]
        return holding[0].store if len(holding) == 1 else None

    # -- opening and closing ------------------------------------------------------------------------------------

    def create(self, spec: CreateWorld) -> World:
        self._refuse_taken(spec.claims)
        named = (
            {t.provider for t in spec.seed.tickets}
            | {d.provider for d in spec.seed.documents}
            | {i.provider for i in spec.inbound}
            | {f.provider for f in spec.faults}
            | {s.provider for s in spec.seed.provider_seeds}
            | {c.provider for c in spec.seed.channels}
        )
        unknown = sorted(named - set(self._manifests))
        if unknown:
            raise WorldRefused(
                f"no installed provider is named {', '.join(unknown)}; installed: {', '.join(sorted(self._manifests))}"
            )
        try:
            refuse_claimed(
                spec.outbound, self._registry, [*self.routing.model_hosts, *(m.host for m in spec.model_hosts)]
            )
            capturing = Capturing(
                spec.outbound,
                replaying=replaying_for(spec.outbound, state=self._state),
                emulators=self.emulators.running,
            )
            stopped = sorted({e.name for e in spec.emulators} - set(self.emulators.running))
            if stopped:
                raise FileNotFoundError(f"emulator {', '.join(stopped)} is not running: start it first")
        except (ProviderConflict, FileNotFoundError) as e:
            raise WorldRefused(f"this world's outbound hosts: {e}") from e
        try:
            refuse_unheld(spec.seed, self._manifests)
        except RunRefused as refused:
            raise WorldRefused(str(refused)) from refused
        unseeded = sorted(
            s.provider
            for s in spec.seed.provider_seeds
            if not isinstance(self._registry.provider(self._manifests[s.provider]), OwnsSeed)
        )
        if unseeded:
            raise Unsupported(f"{', '.join(unseeded)} has no seed of its own: give it no provider seed")
        world_id = self._fresh_id()
        self._declare_models(world_id, spec)
        directory = run_dir(self._state, world_id)
        directory.mkdir(parents=True)
        signing = {i.provider: i.secret or secrets.token_hex(16) for i in spec.inbound}
        now = _now()
        case = self._case_for(spec.case, now) if spec.case is not None else None
        try:
            world = self._open(world_id, spec, signing, capturing, now, case=case)
        except Exception:
            shutil.rmtree(directory)
            self._withdraw_models(world_id)
            if case is not None and not case.worlds:
                self._discard_case(case)
            raise
        name = spec.seed.name
        kept = Kept(
            world_id=world_id,
            name=name,
            claims=spec.claims,
            scripted_people=spec.scripted_people,
            case_id=case.case_id if case is not None else None,
            case=spec.case,
        )
        (directory / KEPT).write_text(kept.model_dump_json(indent=2), encoding="utf-8")
        self.worlds[world_id] = world
        if case is not None:
            case.worlds.append(world_id)
            case.members[world_id] = world.standing
            self._write_case(case)
        for token in spec.claims.tokens:
            self._tokens[token] = world_id
        for host in spec.claims.hosts:
            self._hosts[host.lower()] = world_id
        for key in spec.claims.keys:
            self._keys[key.lower()] = world_id
        if spec.claims.default:
            self._default = world_id
        return world

    def _declare_models(self, world_id: str, spec: CreateWorld) -> None:
        """The model hosts `spec` declares, routed as it says and this world's until it closes; all or none."""
        try:
            for model in spec.model_hosts:
                self.routing.declare(model.host, record=model.record)
                self._models[model.host] = world_id
        except (ProviderConflict, ValueError) as e:
            self._withdraw_models(world_id)
            raise WorldRefused(f"this world's model hosts: {e}") from e

    def _withdraw_models(self, world_id: str) -> None:
        for host in [h for h, w in self._models.items() if w == world_id]:
            self.routing.withdraw(host)
            del self._models[host]

    def _open(
        self,
        world_id: str,
        spec: CreateWorld,
        signing: dict[ProviderKey, str],
        capturing: Capturing,
        now: datetime,
        *,
        case: Case | None,
    ) -> World:
        """The world's store, seeded from `spec` as of `now`, with every provider its seed names seeded already; in
        the step its case is in, or, under no case label, stepped on its own."""
        scenario = spec.seed.starting(now)
        directory = run_dir(self._state, world_id)
        clock = RunClock(scenario.starts_at)
        store = SqliteStore(directory / WORLD, world_id, clock)
        try:
            standing = StandingWorld(
                scenario=scenario,
                store=store,
                clock=clock,
                provider=lambda key: self._registry.provider(self._manifests[key]),
                inbound=[i.to_target() for i in spec.inbound],
                signing=signing,
                scripted=spec.scripted_people,
            )
            named = sorted(
                {t.provider for t in scenario.tickets}
                | {d.provider for d in scenario.documents}
                | {i.provider for i in spec.inbound}
                | {s.provider for s in scenario.provider_seeds}
                | {c.provider for c in scenario.channels}
            )
            standing.open(named, wake=case.stepping.wake if case is not None else FIRST_WAKE)
        except Exception:
            store.close()
            (directory / WORLD).unlink(missing_ok=True)
            raise
        (directory / SCENARIO).write_text(scenario.model_dump_json(indent=2), encoding="utf-8")
        return World(
            world_id=world_id,
            name=spec.seed.name,
            spec=spec,
            standing=standing,
            store=store,
            mounted=Mounted(
                store=store,
                clock=clock,
                app_for=lambda m: self._faulted(world_id, standing.app_for(m), m),
                capturing=capturing.for_people(scenario.people),
            ),
            opened=time.monotonic(),
            faults=[_Armed(f, f.times) for f in spec.faults],
            signing=signing,
            capturing=capturing,
            case=case,
            stepping=None
            if case is not None
            else Stepping(store, start=scenario.starts_at, members=lambda: [standing]),
        )

    # -- cases ----------------------------------------------------------------------------------------------------

    def _case_for(self, label: str, now: datetime) -> Case:
        """The open case of this label, or a new one: worlds opened under one label while any of them is open are
        one case; once its last world closes, the label opens a new case."""
        if label in self._labels:
            return self.cases[self._labels[label]]
        case_id = f"case-{self._fresh_id()}"
        directory = run_dir(self._state, case_id)
        directory.mkdir(parents=True)
        clock = RunClock(now)
        store = SqliteStore(directory / WORLD, case_id, clock)
        clock.enter(FIRST_WAKE)
        store.wake_began(FIRST_WAKE)
        members: dict[str, StandingWorld] = {}
        case = Case(
            case_id=case_id,
            name=label,
            store=store,
            clock=clock,
            mounted=Mounted(store=store, clock=clock, app_for=_no_provider),
            stepping=Stepping(store, start=now, members=lambda: list(members.values()), own=clock),
            members=members,
        )
        self.cases[case_id] = case
        self._labels[label] = case_id
        return case

    def _discard_case(self, case: Case) -> None:
        case.store.close()
        shutil.rmtree(run_dir(self._state, case.case_id), ignore_errors=True)
        del self.cases[case.case_id]
        del self._labels[case.name]

    def case(self, case_id: str) -> Case:
        if case_id not in self.cases:
            raise UnknownCase(f"no open case {case_id}")
        return self.cases[case_id]

    def _scenarios(self, case: Case) -> list[Scenario]:
        """Each world's scenario as it plays it now: an open world's from memory, a closed one's from its file."""
        return [case.members[w].scenario if w in case.members else scenario_of(self._state, w) for w in case.worlds]

    def _write_case(self, case: Case) -> None:
        directory = run_dir(self._state, case.case_id)
        kept = CaseKept(case_id=case.case_id, name=case.name, worlds=case.worlds)
        (directory / CASE).write_text(kept.model_dump_json(indent=2), encoding="utf-8")
        scenario = merged(case.name, self._scenarios(case))
        (directory / SCENARIO).write_text(scenario.model_dump_json(indent=2), encoding="utf-8")

    @contextmanager
    def _reading_case(self, case: Case) -> Iterator[CaseStore]:
        """The case as one run: its open worlds' stores, its closed worlds' files, and its own store."""
        with ExitStack() as stack:
            parts: list[Store] = []
            for world_id in case.worlds:
                if world_id in case.members:
                    parts.append(case.members[world_id].store)
                else:
                    parts.append(stack.enter_context(reading_file(run_dir(self._state, world_id) / WORLD, world_id)))
            yield CaseStore(case.case_id, [*parts, case.store])

    async def case_checks(self, case: Case, *, stop: StopReason | None) -> RunResult:
        """Every check and the scorecard over the case as one run, as it stands."""
        for member in case.members.values():
            await member.observe()
        scenario = merged(case.name, self._scenarios(case))
        with self._reading_case(case) as world:
            return score(scenario, world, stop=stop, ended=self._case_now(case))

    def _case_now(self, case: Case) -> datetime:
        return max([case.clock.now(), *(m.clock.now() for m in case.members.values())])

    async def checks(self, world_id: str) -> RunResult:
        """A world's checks as it stands; for a world of a case, the case's."""
        world = self.get(world_id)
        if world.case is not None:
            return await self.case_checks(world.case, stop=None)
        return await world.standing.checks(stop=None)

    async def advance(self, world_id: str, to: datetime) -> list[Fired]:
        """Move a world's clock: a step is inferred first when the move goes forward and nobody marks steps
        (`application.steps`), so what falls due fires in the new step."""
        world = self.get(world_id)
        if to < world.standing.clock.now():
            raise WorldRefused(
                f"the clock only moves forward: {to.isoformat()} is before {world.standing.clock.now().isoformat()}"
            )
        world.steps.moved(to)
        return await world.standing.advance(to)

    def _close_case(self, case: Case, stop: StopReason, result: RunResult) -> None:
        """The last world of the case has closed: its own record is written, as any run's."""
        self.flush_in(case.mounted)
        if case.stepping.open:
            case.stepping.end()
        else:
            case.store.wake_ended(case.clock.wake())
        scenario = merged(case.name, self._scenarios(case))
        with self._reading_case(case) as world:
            calls = world.calls()
            record = RunRecord(
                run_id=case.case_id,
                scenario=scenario.name,
                seed=scenario.seed,
                started_at=scenario.starts_at,
                ended_at=self._case_now(case),
                wall_seconds=time.monotonic() - case.opened,
                stop=stop,
                providers=list(dict.fromkeys(c.provider for c in calls if c.provider is not None)),
                outbound=outbound_uses(calls),
                emulators=emulator_uses(calls),
                wakes=steps(world),
                worlds=case.worlds,
            )
        directory = run_dir(self._state, case.case_id)
        self._write_case(case)
        (directory / RECORD).write_text(record.model_dump_json(indent=2), encoding="utf-8")
        (directory / RESULT).write_text(result.model_dump_json(indent=2), encoding="utf-8")
        case.ended = _real_now()
        del self.cases[case.case_id]
        del self._labels[case.name]
        self._ended.append(case)
        for old in self._ended[:-ENDED_KEPT_OPEN]:
            old.store.close()
        del self._ended[:-ENDED_KEPT_OPEN]

    # -- steps ----------------------------------------------------------------------------------------------------

    def stepping_of(self, world_id: str | None, case_id: str | None) -> Stepping:
        if case_id is not None:
            return self.case(case_id).stepping
        assert world_id is not None
        return self.get(world_id).steps

    def reset(self, world_id: str) -> World:
        """The world back to the seed it was opened with, in place: the same id, the same claims, the same inbound
        targets and secrets, its clock back at its start, the faults it was opened with armed again, and nothing
        the agent, a person or the test did since in what it holds. Its record is not discarded: the log so far
        is kept as the stretch before this reset (`RESETS`), read again with `since_reset=false`. Tokens its fakes
        minted are no longer claimed: the world that minted them is gone."""
        old = self.get(world_id)
        self.flush_in(old.mounted)
        old.store.close()
        old.open = False
        directory = run_dir(self._state, world_id)
        kept = directory / RESETS / f"{len(self.stretches(world_id)) + 1}.db"
        kept.parent.mkdir(exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            found = directory / f"{WORLD}{suffix}"
            if found.exists():
                found.rename(kept.with_name(kept.name + suffix))
        world = self._open(
            world_id, old.spec, old.signing, old.capturing, old.standing.scenario.starts_at, case=old.case
        )
        world.resets = old.resets + 1
        self.worlds[world_id] = world
        if old.case is not None:
            old.case.members[world_id] = world.standing
        claimed = set(old.spec.claims.tokens)
        self._tokens = {t: w for t, w in self._tokens.items() if w != world_id or t in claimed}
        for trace in old.traces:
            del self._traces[trace]
        return world

    def stretches(self, world_id: str) -> list[Path]:
        """The world's log before each of its resets, oldest first: each a world file of its own."""
        kept = run_dir(self._state, world_id) / RESETS
        if not kept.is_dir():
            return []
        return sorted(kept.glob("*.db"), key=lambda p: int(p.stem))

    def extend(self, world_id: str, added: FurtherSeed) -> dict[ProviderKey, int]:
        """`StandingWorld.extend`, with a scratch store beside the world's own."""
        world = self.get(world_id)
        directory = run_dir(self._state, world_id)
        return world.standing.extend(
            people=added.people,
            tickets=added.tickets,
            documents=added.documents,
            spaces=added.spaces,
            sign_ins=added.sign_ins,
            channels=added.channels,
            provider_seeds=added.provider_seeds,
            directory=directory,
            scratch=_scratch,
        )

    def capabilities(self) -> list[ProviderView]:
        """What each installed provider can be asked to do in a world already open."""
        found: list[ProviderView] = []
        for key in sorted(self._manifests):
            provider = self._registry.provider(self._manifests[key])
            found.append(
                ProviderView(
                    key=key,
                    people_changes=list(provider.manifest.people_changes)
                    if isinstance(provider, ChangesPeople)
                    else [],
                    permissions=isinstance(provider, GrantsPermissions),
                    inbound_credentials=isinstance(provider, MintsInboundCredentials),
                    faults=isinstance(provider, DeclaresFaults),
                    deletes_tickets=isinstance(provider, DeletesTickets),
                    seed_model=isinstance(provider, OwnsSeed),
                )
            )
        return found

    def _refuse_taken(self, claims: Claims) -> None:
        taken = [t for t in claims.tokens if t in self._tokens]
        taken += [h for h in claims.hosts if h.lower() in self._hosts]
        taken += [k for k in claims.keys if k.lower() in self._keys]
        if taken:
            raise WorldRefused(f"already claimed by an open world: {', '.join(taken)}")
        if claims.default and self._default is not None:
            raise WorldRefused(f"world {self._default} is already the default; at most one open world is")

    def installed(self, key: str) -> ProviderKey:
        if key not in self._manifests:
            raise WorldRefused(f"no installed provider is named {key}; installed: {', '.join(sorted(self._manifests))}")
        return key

    def _fresh_id(self) -> str:
        """An id this server has never handed out and no world under its state directory has: a closed world's id
        is never a new world's, so a test still holding one reaches nothing rather than another test's world."""
        while True:
            found = secrets.token_hex(6)
            if found not in self._handed and not run_dir(self._state, found).exists():
                self._handed.add(found)
                return found

    def get(self, world_id: str) -> World:
        if world_id not in self.worlds:
            if world_id in self._handed:
                raise UnknownWorld(f"world {world_id} is closed, and a closed world is never open again")
            raise UnknownWorld(f"no open world {world_id}")
        return self.worlds[world_id]

    async def quiet(self, world_id: str, ask: Quiet) -> Quieted:
        """Wait until no call routed to the world has been seen for `ask.quiet_for` and no delivery to the service
        still awaits its answer, or until `ask.at_most` has passed; say which."""
        world = self.get(world_id)
        began = time.monotonic()
        while True:
            seen, busy = self.activity_in(world.mounted)
            busy = [*busy, *world.standing.delivering()]
            now = time.monotonic()
            if not busy and (seen is None or now - seen >= ask.quiet_for.total_seconds()):
                return Quieted(quiet=True, waited=timedelta(seconds=now - began), last_call=world.mounted.last)
            if now - began >= ask.at_most.total_seconds():
                if not busy and seen is not None:
                    busy = [f"a call {now - seen:.3f}s before the wait gave up: {world.mounted.last}"]
                return Quieted(
                    quiet=False, waited=timedelta(seconds=now - began), busy=busy, last_call=world.mounted.last
                )
            await asyncio.sleep(QUIET_POLL)

    async def close(self, world_id: str, *, quiet: Quiet | None = None) -> tuple[RunResult, Quieted | None]:
        """Wait for the world to go quiet when `quiet` says to, then score it as it stands, write its record,
        release its claims and its file, and remove the oldest closed worlds beyond `keep`. A call that comes for
        it afterwards is refused into the lobby as its late call (`Exchange.late_for`)."""
        quieted = await self.quiet(world_id, quiet) if quiet is not None else None
        world = self.get(world_id)
        self.flush_in(world.mounted)
        case = world.case
        stores = [world.store] if case is None else [m.store for m in case.members.values()]
        environment = any(c.exchange.outcome is CallOutcome.UNAVAILABLE for s in stores for c in s.calls())
        stop = StopReason.ENVIRONMENT_FAILED if environment else StopReason.CLOSED
        if case is None:
            result = await world.standing.checks(stop=stop)
            wakes = steps(world.store)
        else:
            if len(case.members) == 1:
                self.flush_in(case.mounted)
            result = await self.case_checks(case, stop=stop)
            with self._reading_case(case) as whole:
                wakes = steps(whole)
        result = result.model_copy(
            update={"findings": [*result.findings, *(f for s in stores for f in emulator_findings(s))]}
        )
        world.standing.close()
        record = RunRecord(
            run_id=world_id,
            scenario=world.standing.scenario.name,
            seed=world.standing.scenario.seed,
            started_at=world.standing.scenario.starts_at,
            ended_at=world.standing.clock.now(),
            wall_seconds=time.monotonic() - world.opened,
            stop=stop,
            providers=list(dict.fromkeys(c.provider for c in world.store.calls() if c.provider is not None)),
            outbound=outbound_uses(world.store.calls()),
            emulators=emulator_uses(world.store.calls()),
            wakes=wakes,
        )
        directory = run_dir(self._state, world_id)
        write_recordings(directory, world.store.calls())
        (directory / RECORD).write_text(record.model_dump_json(indent=2), encoding="utf-8")
        (directory / RESULT).write_text(result.model_dump_json(indent=2), encoding="utf-8")
        world.store.close()
        world.open = False
        del self.worlds[world_id]
        if case is not None:
            del case.members[world_id]
            if not case.members:
                self._close_case(case, stop, result)
        self._closed.append(
            _Closed(
                world_id=world_id,
                tokens=frozenset(t for t, w in self._tokens.items() if w == world_id),
                hosts=frozenset(h for h, w in self._hosts.items() if w == world_id),
                keys=frozenset(k for k, w in self._keys.items() if w == world_id),
            )
        )
        del self._closed[:-CLOSED_REMEMBERED]
        self._tokens = {t: w for t, w in self._tokens.items() if w != world_id}
        self._hosts = {h: w for h, w in self._hosts.items() if w != world_id}
        self._keys = {k: w for k, w in self._keys.items() if w != world_id}
        for trace in world.traces:
            del self._traces[trace]
        self._withdraw_models(world_id)
        if self._default == world_id:
            self._default = None
        self._retain()
        return result, quieted

    async def close_all(self) -> None:
        for world_id in list(self.worlds):
            await self.close(world_id)
        for case in self._ended:
            case.store.close()
        self._ended.clear()
        await self.emulators.stop()
        self.lobby_store.close()

    def _retain(self) -> Collected:
        """Keep the newest `keep` closed standing worlds, a closed case counting as one with its worlds; remove the
        rest, and sweep what is left as `minutehand gc` does."""
        base = self._state / RUNS
        closed: list[tuple[Path, list[str]]] = []
        for d in base.iterdir():
            if not (d / RECORD).is_file():
                continue
            if (d / CASE).is_file():
                kept = CaseKept.model_validate_json((d / CASE).read_text(encoding="utf-8"))
                closed.append((d, [d.name, *kept.worlds]))
            elif (d / KEPT).is_file():
                if Kept.model_validate_json((d / KEPT).read_text(encoding="utf-8")).case_id is None:
                    closed.append((d, [d.name]))
        closed.sort(key=lambda found: (found[0] / RECORD).stat().st_mtime_ns)
        old = closed[: max(0, len(closed) - self._keep)]
        still = {c.case_id for c in self._ended}
        return collect(self._state, remove=[n for d, names in old if d.name not in still for n in names])

    # -- faults -------------------------------------------------------------------------------------------------

    def arm(self, world_id: str, fault: Fault) -> None:
        if fault.provider not in self._manifests:
            raise WorldRefused(f"no installed provider is named {fault.provider}")
        self.get(world_id).faults.append(_Armed(fault, fault.times))

    def _faulted(self, world_id: str, app: ASGIApp, manifest: Manifest) -> ASGIApp:
        """The provider's app, answering instead of it while a fault armed on this world matches the call."""

        async def answer(
            scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]
        ) -> None:
            world = self.worlds[world_id] if world_id in self.worlds else None
            method = scope["method"] if "method" in scope else None
            path = scope["path"] if "path" in scope else None
            armed = (
                next(
                    (
                        a
                        for a in world.faults
                        if a.left > 0
                        and a.fault.provider == manifest.key
                        and (a.fault.method is None or a.fault.method.upper() == method)
                        and isinstance(path, str)
                        and path.startswith(a.fault.path)
                    ),
                    None,
                )
                if world is not None
                else None
            )
            if armed is None:
                await app(scope, receive, send)
                return
            armed.left -= 1
            injected()
            fault = armed.fault
            headers = [(b"content-type", fault.content_type.encode())]
            if fault.retry_after is not None:
                headers.append((b"retry-after", str(fault.retry_after).encode()))
            start: Message = {"type": "http.response.start", "status": fault.status, "headers": headers}
            body: Message = {"type": "http.response.body", "body": fault.body.encode()}
            await send(start)
            await send(body)

        return answer


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    """An empty store at `path` for `application.further_seed`, closed when done."""
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


def _real_now() -> datetime:
    """The real moment, for a case's window, which places spans that carry no trace link by their own real start."""
    return datetime.now(UTC)  # clock-lint: exempt a case's real-time window is compared with spans' real starts


def _now() -> datetime:
    """The moment a world with no `starts_at` starts, to the second."""
    return datetime.now(UTC).replace(microsecond=0)  # clock-lint: exempt the real start of a standing world, read once


# -- the server ---------------------------------------------------------------------------------------------------


@dataclass
class Serving:
    """A running server: where each part listens, and the worlds."""

    standing: Standing
    proxy: Proxy
    receiver: Receiver | None
    control_port: int
    options: ServeOptions

    def listen(self) -> Listen:
        return Listen(
            host=self.options.host,
            port=self.proxy.port,
            agent_host=self.options.agent_host,
            no_proxy=self.options.no_proxy,
            receive_telemetry=self.receiver is not None,
            telemetry_port=self.receiver.port if self.receiver is not None else 0,
        )

    def environment(self, ca_path: str | None, no_proxy: Sequence[str] = ()) -> dict[str, str]:
        """What a service needs to reach the fakes through this server; `ca_path` is where it finds the CA bundle
        (`GET /v1/ca.pem`), this machine's own file when None; `no_proxy` names more hosts the service reaches
        directly (the other services of its stack). This server's own name is always among them: its control API
        and OTLP receiver are plain HTTP, never reached through its proxy."""
        listen = self.listen()
        own = [listen.agent_host] if listen.agent_host is not None else []
        direct = listen.model_copy(update={"no_proxy": [*listen.no_proxy, *no_proxy, *own]})
        return agent_environment(
            direct,
            self.proxy.port,
            ca_path or str(self.proxy.ca_bundle.resolve()),
            {},
            telemetry_port=self.receiver.port if self.receiver is not None else None,
        )


@asynccontextmanager
async def serving(state: Path, options: ServeOptions) -> AsyncIterator[Serving]:
    """The proxy, the receiver and the control API, until the context ends; every world open then is closed."""
    from minutehand.adapters.control.app import create_app

    state.mkdir(parents=True, exist_ok=True)
    registry = Registry.installed()
    try:
        routing = Routing(registry, model_hosts=list(dict.fromkeys([*DEFAULT_MODEL_HOSTS, *options.model_hosts])))
    except ProviderConflict as e:
        raise RunRefused(f"the model hosts {', '.join(options.model_hosts)}: {e}") from e
    standing = Standing(state, registry, keep=options.keep, routing=routing)
    lobby = standing.lobby
    try:
        async with Proxy(
            routing,
            lobby.store,
            lobby.clock,
            confdir=state / "ca",
            host=options.host,
            port=options.proxy_port,
            upstream_ca=options.upstream_ca,
            capture_unknown=options.capture_unknown,
            record_model_calls=options.record_model_calls,
        ) as proxy:
            proxy.addon.route(standing)
            standing.flush_in = proxy.addon.flush_in
            standing.activity_in = proxy.addon.activity_in
            async with _receiver(standing, options) as receiver:
                serving_ = Serving(standing, proxy, receiver, options.control_port, options)
                async with _control(create_app(serving_), options.host, options.control_port) as port:
                    serving_.control_port = port
                    yield serving_
    finally:
        await standing.close_all()


@asynccontextmanager
async def _receiver(standing: Standing, options: ServeOptions) -> AsyncIterator[Receiver | None]:
    if not options.receive_telemetry:
        yield None
        return
    receiver = Receiver(
        standing.lobby.store,
        standing.lobby.clock,
        host=options.host,
        port=options.telemetry_port,
        forwarding=Forwarding.from_environment(os.environ),
        agent_host=options.agent_host,
    )
    receiver.route(standing.by_span)
    async with receiver:
        # An emulator exports to the receiver as the services do; its spans follow the forwarded trace to a world.
        standing.emulators.environment = exporter_environment(f"http://127.0.0.1:{receiver.port}")
        yield receiver


@asynccontextmanager
async def _control(app: Starlette, host: str, port: int) -> AsyncIterator[int]:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
    except OSError as e:
        listener.close()
        raise OSError(f"the control API could not listen on {host}:{port}: {e}") from e
    listener.listen(256)
    listener.setblocking(False)
    config = uvicorn.Config(app, lifespan="off", http="h11", log_config=None, access_log=False)
    config.load()
    server = uvicorn.Server(config)
    server.lifespan = config.lifespan_class(config)
    await server.startup(sockets=[listener])
    try:
        yield listener.getsockname()[1]
    finally:
        await server.shutdown(sockets=[listener])


async def serve_forever(state: Path, options: ServeOptions, *, announce: bool = True) -> None:
    """`minutehand serve`: run until interrupted."""
    async with serving(state, options) as running:
        if announce:
            print(
                f"minutehand serve: proxy {options.host}:{running.proxy.port}, control "
                f"http://{options.host}:{running.control_port}{'/v1'}, telemetry "
                f"{running.receiver.port if running.receiver is not None else 'off'} (state {state})",
                flush=True,
            )
        await asyncio.Event().wait()
