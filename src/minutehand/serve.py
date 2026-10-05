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
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from pydantic import Field
from starlette.applications import Starlette

from minutehand.adapters.control.wire import Claims, CreateWorld, Fault, FurtherSeed, ProviderView
from minutehand.adapters.proxy.capture import Capturing, refuse_claimed, replaying_for, write_recordings
from minutehand.adapters.proxy.policy import DEFAULT_MODEL_HOSTS, Routing
from minutehand.adapters.proxy.registry import ProviderConflict, Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.proxy.worlds import Mounted
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.telemetry.forward import Forwarding
from minutehand.adapters.telemetry.receiver import Receiver
from minutehand.application.outbound import outbound_uses
from minutehand.application.refusals import RunRefused, refuse_unheld
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld, Unsupported, WorldRefused
from minutehand.checks.runner import RunResult
from minutehand.domain.provider import Manifest
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Model, ProviderKey
from minutehand.domain.world import Exchange
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
    run_dir,
)

KEPT = "world.json"
LOBBY = "lobby"
RESETS = "resets"
"""Where a standing world keeps its log from before each reset, `<n>.db` for the n-th, oldest first."""

DEFAULT_PROXY_PORT = 8080
DEFAULT_CONTROL_PORT = 8081
DEFAULT_TELEMETRY_PORT = 4318
DEFAULT_KEEP = 100


class Kept(Model):
    """What marks a run directory as a standing world's, and what it was opened with besides its seed."""

    world_id: str
    name: str
    claims: Claims
    scripted_people: bool


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


def _nothing_relayed(world: Mounted) -> None:
    """Before a proxy routes to the worlds, no tunnel is relayed into any of them."""


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
        declared = self.routing.declared(host)
        if declared is not None and declared in self._models:
            return self.worlds[self._models[declared]].mounted
        if trace_id is not None and trace_id in self._traces:
            return self.worlds[self._traces[trace_id]].mounted
        return self._lobby

    def by_trace(self, trace_id: str) -> Store | None:
        """`Receiver.route`: the world whose calls carried this trace."""
        return self.worlds[self._traces[trace_id]].store if trace_id in self._traces else None

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
            capturing = Capturing(spec.outbound, replaying=replaying_for(spec.outbound, state=self._state))
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
        world_id = secrets.token_hex(6)
        self._declare_models(world_id, spec)
        directory = run_dir(self._state, world_id)
        directory.mkdir(parents=True)
        signing = {i.provider: i.secret or secrets.token_hex(16) for i in spec.inbound}
        try:
            world = self._open(world_id, spec, signing, capturing, _now())
        except Exception:
            shutil.rmtree(directory)
            self._withdraw_models(world_id)
            raise
        name = spec.seed.name
        kept = Kept(world_id=world_id, name=name, claims=spec.claims, scripted_people=spec.scripted_people)
        (directory / KEPT).write_text(kept.model_dump_json(indent=2), encoding="utf-8")
        self.worlds[world_id] = world
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
        self, world_id: str, spec: CreateWorld, signing: dict[ProviderKey, str], capturing: Capturing, now: datetime
    ) -> World:
        """The world's store, seeded from `spec` as of `now`, with every provider its seed names seeded already."""
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
            standing.open(named)
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
        )

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
        world = self._open(world_id, old.spec, old.signing, old.capturing, old.standing.scenario.starts_at)
        world.resets = old.resets + 1
        self.worlds[world_id] = world
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

    def get(self, world_id: str) -> World:
        if world_id not in self.worlds:
            raise LookupError(f"no open world {world_id}")
        return self.worlds[world_id]

    async def close(self, world_id: str) -> RunResult:
        """Score the world as it stands, write its record, release its claims and its file, and remove the
        oldest closed worlds beyond `keep`."""
        world = self.get(world_id)
        self.flush_in(world.mounted)
        result = await world.standing.checks(stop=StopReason.CLOSED)
        world.standing.close()
        record = RunRecord(
            run_id=world_id,
            scenario=world.standing.scenario.name,
            seed=world.standing.scenario.seed,
            started_at=world.standing.scenario.starts_at,
            ended_at=world.standing.clock.now(),
            wall_seconds=time.monotonic() - world.opened,
            stop=StopReason.CLOSED,
            providers=list(dict.fromkeys(c.provider for c in world.store.calls() if c.provider is not None)),
            outbound=outbound_uses(world.store.calls()),
            wakes=[],
        )
        directory = run_dir(self._state, world_id)
        write_recordings(directory, world.store.calls())
        (directory / RECORD).write_text(record.model_dump_json(indent=2), encoding="utf-8")
        (directory / RESULT).write_text(result.model_dump_json(indent=2), encoding="utf-8")
        world.store.close()
        world.open = False
        del self.worlds[world_id]
        self._tokens = {t: w for t, w in self._tokens.items() if w != world_id}
        self._hosts = {h: w for h, w in self._hosts.items() if w != world_id}
        self._keys = {k: w for k, w in self._keys.items() if w != world_id}
        for trace in world.traces:
            del self._traces[trace]
        self._withdraw_models(world_id)
        if self._default == world_id:
            self._default = None
        self._retain()
        return result

    async def close_all(self) -> None:
        for world_id in list(self.worlds):
            await self.close(world_id)
        self.lobby_store.close()

    def _retain(self) -> Collected:
        """Keep the newest `keep` closed standing worlds; remove the rest, and sweep what is left as `minutehand gc`
        does."""
        base = self._state / RUNS
        closed = [d for d in base.iterdir() if (d / KEPT).is_file() and (d / RECORD).is_file()]
        closed.sort(key=lambda d: (d / RECORD).stat().st_mtime_ns)
        return collect(self._state, remove=[d.name for d in closed[: max(0, len(closed) - self._keep)]])

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
    receiver.route(standing.by_trace)
    async with receiver:
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
