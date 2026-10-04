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

from minutehand.adapters.proxy.addon import ProxyAddon
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.trust import BUNDLE, CA_CERT, write_bundle
from minutehand.domain.scenario import ProviderKey
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
    ) -> None:
        """`upstream_ca` replaces the system trust store when verifying the hosts an edited call is sent on to.
        `public_roots` replaces certifi's roots in the bundle the agent is handed (`ca_bundle`)."""
        self.addon = ProxyAddon(routing, store, clock, telemetry)
        self._confdir = confdir
        self._listen = (host, port)
        self._upstream_ca = upstream_ca
        self._public_roots = public_roots
        self._master: Master | None = None
        self.host = host
        self.port = port

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

    def mount(self, world: Store, clock: Clock, apps: Mapping[ProviderKey, ASGIApp]) -> None:
        """`application.orchestrator.Mounts`: the run the proxy now answers and records for."""
        self.addon.mount(world, clock, apps)

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
        master.addons.add(self.addon)
        master.options.update(
            connection_strategy="lazy",
            upstream_cert=False,
            ssl_verify_upstream_trusted_ca=str(self._upstream_ca) if self._upstream_ca is not None else None,
        )
        server = master.addons.get("proxyserver")
        assert isinstance(server, Proxyserver)
        if not await server.setup_servers():
            raise OSError(f"the proxy could not listen on {self._listen[0]}:{self._listen[1]}")
        await master.running()
        bound = server.listen_addrs()
        if not bound:
            raise OSError("the proxy started but reports no listening address")
        self.host, self.port = bound[0][0], bound[0][1]
        self._master = master
        _running.append(self)
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        master, self._master = self._master, None
        if master is None:
            return
        _running.remove(self)
        server = master.addons.get("proxyserver")
        assert isinstance(server, Proxyserver)
        await server.servers.update([])
        await master.done()
