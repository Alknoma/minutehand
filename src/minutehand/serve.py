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

Each world is a run in the state directory, so `minutehand findings`, `view` and the MCP tools read it:

    <state>/runs/<world_id>/world.db       its log, as any run's
    <state>/runs/<world_id>/scenario.json  the seed as the world plays it
    <state>/runs/<world_id>/world.json     `Kept`: its name and claims, which marks it as a standing world
    <state>/runs/<world_id>/record.json    once closed: `RunRecord`, stopped CLOSED
    <state>/runs/<world_id>/result.json    once closed: the checks over it as it was closed
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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from pydantic import Field
from starlette.applications import Starlette

from minutehand.adapters.control.wire import Claims, CreateWorld, Fault
from minutehand.adapters.proxy.capture import Capturing, refuse_claimed, replaying_for, write_recordings
from minutehand.adapters.proxy.policy import DEFAULT_MODEL_HOSTS, Routing
from minutehand.adapters.proxy.registry import ProviderConflict, Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.proxy.worlds import Mounted
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.telemetry.forward import Forwarding
from minutehand.adapters.telemetry.receiver import Receiver
from minutehand.application.outbound import outbound_uses
from minutehand.application.refusals import RunRefused, refuse_unheld_ticket_fields
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld, WorldRefused
from minutehand.checks.runner import RunResult
from minutehand.domain.provider import Manifest
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Model, ProviderKey
from minutehand.domain.world import Exchange
from minutehand.ports.provider import ASGIApp, Message, Scope
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


class Standing:
    """Every world the server holds, and which one each call belongs to: `adapters.proxy.worlds.Worlds`."""

    def __init__(self, state: Path, registry: Registry, *, keep: int) -> None:
        self._state = state
        self._registry = registry
        self._keep = keep
        self._manifests = {m.key: m for m in registry.manifests}
        self.worlds: dict[str, World] = {}
        self._tokens: dict[str, str] = {}
        self._hosts: dict[str, str] = {}
        self._traces: dict[str, str] = {}
        self._default: str | None = None
        lobby_id = f"{LOBBY}-{secrets.token_hex(6)}"
        directory = run_dir(state, lobby_id)
        directory.mkdir(parents=True)
        self._lobby_clock = RunClock(_now())
        self.lobby_store = SqliteStore(directory / WORLD, lobby_id, self._lobby_clock)
        self._lobby = Mounted(store=self.lobby_store, clock=self._lobby_clock, app_for=self._no_app)

    @staticmethod
    def _no_app(manifest: Manifest) -> ASGIApp:
        raise RunRefused(f"the lobby answers no provider; a call to {manifest.key} here is refused")

    # -- adapters.proxy.worlds.Worlds -------------------------------------------------------------------------

    def world_for(self, host: str, credentials: Sequence[str]) -> Mounted | None:
        found = self._hosts[host.lower()] if host.lower() in self._hosts else None
        if found is None:
            found = next((self._tokens[c] for c in credentials if c in self._tokens), None)
        if found is None:
            found = self._default
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
        )
        unknown = sorted(named - set(self._manifests))
        if unknown:
            raise WorldRefused(
                f"no installed provider is named {', '.join(unknown)}; installed: {', '.join(sorted(self._manifests))}"
            )
        try:
            refuse_claimed(spec.outbound, self._registry, DEFAULT_MODEL_HOSTS)
            capturing = Capturing(spec.outbound, replaying=replaying_for(spec.outbound, state=self._state))
        except (ProviderConflict, FileNotFoundError) as e:
            raise WorldRefused(f"this world's outbound hosts: {e}") from e
        try:
            refuse_unheld_ticket_fields(spec.seed.tickets, self._manifests)
        except RunRefused as refused:
            raise WorldRefused(str(refused)) from refused
        world_id = secrets.token_hex(6)
        scenario = spec.seed.starting(_now())
        directory = run_dir(self._state, world_id)
        directory.mkdir(parents=True)
        clock = RunClock(scenario.starts_at)
        store = SqliteStore(directory / WORLD, world_id, clock)
        try:
            standing = StandingWorld(
                scenario=scenario,
                store=store,
                clock=clock,
                provider=lambda key: self._registry.provider(self._manifests[key]),
                inbound=[i.to_target() for i in spec.inbound],
                signing={i.provider: i.secret or secrets.token_hex(16) for i in spec.inbound},
                scripted=spec.scripted_people,
            )
            named = sorted(
                {t.provider for t in scenario.tickets}
                | {d.provider for d in scenario.documents}
                | {i.provider for i in spec.inbound}
            )
            standing.open(named)
        except Exception:
            store.close()
            shutil.rmtree(directory)
            raise
        name = spec.seed.name
        (directory / SCENARIO).write_text(scenario.model_dump_json(indent=2), encoding="utf-8")
        kept = Kept(world_id=world_id, name=name, claims=spec.claims, scripted_people=spec.scripted_people)
        (directory / KEPT).write_text(kept.model_dump_json(indent=2), encoding="utf-8")
        world = World(
            world_id=world_id,
            name=name,
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
        )
        self.worlds[world_id] = world
        for token in spec.claims.tokens:
            self._tokens[token] = world_id
        for host in spec.claims.hosts:
            self._hosts[host.lower()] = world_id
        if spec.claims.default:
            self._default = world_id
        return world

    def _refuse_taken(self, claims: Claims) -> None:
        taken = [t for t in claims.tokens if t in self._tokens]
        taken += [h for h in claims.hosts if h.lower() in self._hosts]
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
        for trace in world.traces:
            del self._traces[trace]
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

    def environment(self, ca_path: str | None) -> dict[str, str]:
        """What a service needs to reach the fakes through this server; `ca_path` is where it finds the CA bundle
        (`GET /v1/ca.pem`), this machine's own file when None."""
        return agent_environment(
            self.listen(),
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
    standing = Standing(state, registry, keep=options.keep)
    lobby = standing.lobby
    try:
        async with Proxy(
            Routing(registry),
            lobby.store,
            lobby.clock,
            confdir=state / "ca",
            host=options.host,
            port=options.proxy_port,
            upstream_ca=options.upstream_ca,
            capture_unknown=options.capture_unknown,
        ) as proxy:
            proxy.addon.route(standing)
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
