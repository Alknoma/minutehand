"""Which world an intercepted call is answered and recorded in.

`minutehand run` plays one world at a time, and every call belongs to it (`One`). `minutehand serve` holds
many at once and decides per call, from the credentials the call carries and the host it went to
(`serve.Standing`). Either way the proxy asks the same three questions of a `Worlds`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from minutehand.adapters.proxy.capture import Capturing
from minutehand.adapters.proxy.held import Waits
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import ProviderKey, Scenario
from minutehand.domain.world import Exchange
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp, HeldCalls, Provider
from minutehand.ports.store import Store


@dataclass
class Mounted:
    """One world the proxy answers for: where its calls are recorded, the clock they are stamped from, and how
    a provider's app, and the provider itself (for the gRPC and WebSocket servers `adapters.proxy.local` starts for
    it), are had in it. `lock` holds one answered call at a time in this world, so the events
    between two reads of its head are exactly the events that call produced; calls in different worlds do
    not wait on each other."""

    store: Store
    clock: Clock
    app_for: Callable[[Manifest], ASGIApp]
    provider_for: Callable[[Manifest], Provider]
    """The provider as this world has it: built, bound and seeded as `app_for` has it built."""
    capturing: Capturing = field(default_factory=Capturing)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seen: float | None = None
    """When a call routed to this world was last seen beginning or ending, as `time.monotonic()`; None: never."""
    last: str | None = None
    """That call, for a person (`GET slack.com/api/auth.test`)."""
    waits: Waits = field(default_factory=Waits)
    """The calls held in this world until it can answer them (`adapters.proxy.held`)."""


class Worlds(Protocol):
    """Where calls go: one world for `minutehand run`, many for `minutehand serve`."""

    def world_for(self, host: str, credentials: Sequence[str], keys: Sequence[str]) -> Mounted | None:
        """The world this call belongs to, by the host it went to, the world keys its provider reads in its URL
        (`Manifest.world_keys`), or the credentials it carries; None when it belongs to none, and is refused and
        kept in `lobby`."""
        ...

    @property
    def lobby(self) -> Mounted:
        """Where a call no world claims is recorded."""
        ...

    def answered(self, world: Mounted, exchange: Exchange, minted: Sequence[str]) -> None:
        """A call was answered in `world`; `minted` are credentials its answer handed out (an OAuth access
        token), which later calls will carry."""
        ...

    def keeping(self, host: str, trace_id: str | None) -> Mounted:
        """Where a model call to `host` recorded on the wire is kept as a span: the world that declared the host a
        model host, else the world whose calls carried `trace_id`, else `lobby`."""
        ...

    def late_for(self, host: str, credentials: Sequence[str], keys: Sequence[str]) -> str | None:
        """For a call no open world claims: the world, closed already, whose claims it carries, so the lobby
        records it as that world's late call; None when it carries no closed world's claim either."""
        ...


class One:
    """Every call is the one run's: `minutehand run` and `minutehand fork`, moved from run to run by `mount`."""

    def __init__(self, world: Mounted) -> None:
        self._world = world

    def world_for(self, host: str, credentials: Sequence[str], keys: Sequence[str]) -> Mounted | None:
        return self._world

    @property
    def lobby(self) -> Mounted:
        return self._world

    def answered(self, world: Mounted, exchange: Exchange, minted: Sequence[str]) -> None:
        """One world needs to learn nothing about which calls are its own."""

    def keeping(self, host: str, trace_id: str | None) -> Mounted:
        return self._world

    def late_for(self, host: str, credentials: Sequence[str], keys: Sequence[str]) -> str | None:
        """Every call is the one run's: none is ever late for another."""
        return None


def one_run(
    store: Store,
    clock: Clock,
    apps: Mapping[ProviderKey, ASGIApp],
    *,
    scenario: Scenario | None,
    provider: Callable[[Manifest], Provider],
    capturing: Capturing | None = None,
    holds: HeldCalls | None = None,
) -> One:
    """The world of one run: each of `apps` answers its provider's hosts; a provider claimed and not mounted is
    built on its first call over `store`, and seeded then with `scenario`'s people and things, unless the world
    already holds anything of it. `capturing` is what the run captures of the hosts nobody claims, its sends
    read against `scenario`'s people. `holds` holds a call that waits on the world until the run's clock answers it."""
    built = dict(apps)

    def app_for(manifest: Manifest) -> ASGIApp:
        if manifest.key not in built:
            found = provider(manifest)
            if scenario is not None and not any(e.entity.provider == manifest.key for e in store.events()):
                found.seed(scenario, store)
            built[manifest.key] = found.app(store, clock)
        return built[manifest.key]

    def provider_for(manifest: Manifest) -> Provider:
        app_for(manifest)
        return provider(manifest)

    captures = capturing or Capturing()
    if scenario is not None:
        captures = captures.for_people(scenario.people)
    waits = Waits(holds=holds)
    if holds is not None:
        holds.answering(waits.quiet)
    return One(
        Mounted(
            store=store,
            clock=clock,
            app_for=app_for,
            provider_for=provider_for,
            capturing=captures,
            waits=waits,
        )
    )
