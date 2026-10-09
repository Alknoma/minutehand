"""Start and stop the proxy inside the running event loop.

    async with Proxy(routing, store, clock, confdir=state / "ca") as proxy:
        env = {"HTTPS_PROXY": proxy.url, "SSL_CERT_FILE": str(proxy.ca_bundle)}

One proxy runs in a process at a time. mitmproxy keeps its running master in a module global
(`mitmproxy.ctx.master`), and a second master started beside the first served certificates its own CA
did not sign; starting a second while one runs is refused. A process that plays several runs starts one
proxy and moves it from run to run with `mount`.

Two options keep a claimed or refused host from ever being contacted: connections
upstream are opened only when a request is forwarded (`connection_strategy="lazy"`),
and the certificate shown to the client is minted from the name it asked for rather
than copied from the real server (`upstream_cert=False`).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import TracebackType

from mitmproxy import options
from mitmproxy.addons import default_addons
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.master import Master
from mitmproxy.proxy import mode_specs

from minutehand.adapters import reaching
from minutehand.adapters.proxy.addon import ProxyAddon
from minutehand.adapters.proxy.base_url import BaseUrls
from minutehand.adapters.proxy.capture import Capturing
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.redirected import RedirectedMode
from minutehand.adapters.proxy.trust import BUNDLE, CA_CERT, write_bundle
from minutehand.application.traffic import SeenCall
from minutehand.domain.outbound import UnknownHosts
from minutehand.domain.scenario import ProviderKey, Scenario
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry


class ProxyRunning(RuntimeError):
    """A proxy is already running in this process."""


_running: list[Proxy] = []


class Proxy:
    def __init__(
        self,
        routing: Routing,
        store: Store,
        clock: Clock,
        *,
        confdir: Path,
        host: str = "127.0.0.1",
        port: int = 0,
        upstream_ca: Path | None = None,
        public_roots: str | None = None,
        telemetry: Telemetry | None = None,
        record_model_calls: bool = False,
        capturing: Capturing | None = None,
        capture_unknown: UnknownHosts = UnknownHosts.REFUSE,
        redirect_port: int | None = None,
    ) -> None:
        """`redirect_port`, when given, opens a second listener on `host` for connections the network redirects to
        the proxy with no proxy asked for (`adapters.proxy.redirected`; 0 lets the system pick it). `upstream_ca` replaces the system trust store when verifying the hosts an edited, recorded or
        passed-through call is sent on to. `public_roots` replaces certifi's roots in the bundle the agent is
        handed (`ca_bundle`). `record_model_calls` opens the agent's calls to model APIs and keeps each as a span.
        `capturing` is what every run mounted on this proxy captures of the hosts nobody claims;
        `capture_unknown` says which calls to an undeclared host are passed through and kept rather than refused."""
        self.addon = ProxyAddon(
            routing,
            store,
            clock,
            telemetry,
            record_model_calls=record_model_calls,
            capturing=capturing,
            capture_unknown=capture_unknown,
        )
        self.base_urls = BaseUrls(routing)
        self._confdir = confdir
        self._listen = (host, port)
        self._upstream_ca = upstream_ca
        self._public_roots = public_roots
        self._master: Master | None = None
        self.host = host
        self.port = port
        self.redirect_port = redirect_port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ca_cert(self) -> Path:
        """The proxy's own CA; created in `confdir` on first start and reused after."""
        return self._confdir / CA_CERT

    @property
    def ca_bundle(self) -> Path:
        """The file an agent trusts: the public roots and the proxy's CA, so a call the proxy answers and a call
        it tunnels to a real host both verify. Written when the proxy starts."""
        return self._confdir / BUNDLE

    def mount(
        self, world: Store, clock: Clock, apps: Mapping[ProviderKey, ASGIApp], *, scenario: Scenario | None = None
    ) -> None:
        """`application.orchestrator.Mounts`: the run the proxy now answers and records for, and the scenario a
        provider first called mid-run is seeded with."""
        self.addon.mount(world, clock, apps, scenario=scenario)

    def flush(self) -> None:
        """`application.orchestrator.Mounts`: every burst in progress on a tunnel it relays, recorded now."""
        self.addon.flush()

    def last_call(self) -> SeenCall | None:
        """`application.traffic.Traffic`: the agent's latest outbound call this proxy saw."""
        return self.addon.last_seen

    def waiting(self) -> list[str]:
        """`application.traffic.Traffic`: calls sent on and not answered yet, and tunnels whose last bytes went
        from the agent."""
        return self.addon.waiting()

    async def __aenter__(self) -> Proxy:
        if _running:
            raise ProxyRunning(
                f"a proxy is already running in this process on {_running[0].url}; "
                "mitmproxy allows one, so move it to the next run with mount()"
            )
        write_bundle(self._confdir, public_roots=self._public_roots)
        master = Master(
            options.Options(listen_host=self._listen[0], listen_port=self._listen[1], confdir=str(self._confdir))
        )
        master.addons.add(*default_addons())
        master.addons.add(self.addon, self.base_urls)  # in this order: a base-URL answer is rewritten after it is kept
        master.options.update(
            connection_strategy="lazy",
            upstream_cert=False,
            ssl_verify_upstream_trusted_ca=str(self._upstream_ca) if self._upstream_ca is not None else None,
        )
        server = master.addons.get("proxyserver")
        assert isinstance(server, Proxyserver)
        if not await server.setup_servers():
            raise OSError(f"the proxy could not listen on {self._listen[0]}:{self._listen[1]}")
        if self.redirect_port is not None:
            # Added beside the regular listener rather than in the `mode` option, whose check refuses two listeners
            # on one host when both ports are left to the system (0).
            redirected = mode_specs.ProxyMode.parse(f"redirected@{self._listen[0]}:{self.redirect_port}")
            if not await server.servers.update([*(instance.mode for instance in server.servers), redirected]):
                await server.servers.update([])
                raise OSError(f"the proxy could not listen on {self._listen[0]}:{self.redirect_port} for redirects")
        await master.running()
        bound = {type(instance.mode): instance.listen_addrs for instance in server.servers}
        regular, redirected = bound.get(mode_specs.RegularMode), bound.get(RedirectedMode)
        if not regular or (self.redirect_port is not None and not redirected):
            raise OSError("the proxy started but reports no listening address")
        self.host, self.port = regular[0][0], regular[0][1]
        if redirected:
            self.redirect_port = redirected[0][1]
        self._master = master
        _running.append(self)
        reaching.trust(self.ca_bundle)
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        master, self._master = self._master, None
        if master is None:
            return
        _running.remove(self)
        reaching.trust(None)
        server = master.addons.get("proxyserver")
        assert isinstance(server, Proxyserver)
        await server.servers.update([])
        await master.done()
