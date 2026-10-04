"""Start and stop the proxy inside the running event loop.

    async with Proxy(routing, store, clock, confdir=state / "ca") as proxy:
        env = {"HTTPS_PROXY": proxy.url, "SSL_CERT_FILE": str(proxy.ca_cert)}

Two options keep a claimed or refused host from ever being contacted: connections
upstream are opened only when a request is forwarded (`connection_strategy="lazy"`),
and the certificate shown to the client is minted from the name it asked for rather
than copied from the real server (`upstream_cert=False`).
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

from mitmproxy import options
from mitmproxy.addons import default_addons
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.master import Master

from minutehand.adapters.proxy.addon import ProxyAddon
from minutehand.adapters.proxy.policy import Routing
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry

CA_CERT = "mitmproxy-ca-cert.pem"


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
        telemetry: Telemetry | None = None,
    ) -> None:
        """`upstream_ca` replaces the system trust store when verifying the hosts an edited call is sent on to."""
        self.addon = ProxyAddon(routing, store, clock, telemetry)
        self._confdir = confdir
        self._listen = (host, port)
        self._upstream_ca = upstream_ca
        self._master: Master | None = None
        self.host = host
        self.port = port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ca_cert(self) -> Path:
        """The CA a client must trust; created in `confdir` on first start and reused after."""
        return self._confdir / CA_CERT

    async def __aenter__(self) -> Proxy:
        self._confdir.mkdir(parents=True, exist_ok=True)
        master = Master(options.Options(listen_host=self._listen[0], listen_port=self._listen[1],
                                        confdir=str(self._confdir)))
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
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        master, self._master = self._master, None
        if master is None:
            return
        server = master.addons.get("proxyserver")
        assert isinstance(server, Proxyserver)
        await server.servers.update([])
        await master.done()
