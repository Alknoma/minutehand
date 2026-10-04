"""A test-only intercepting proxy that answers `*.amazonaws.com` with one provider's ASGI app.

mitmproxy runs embedded (`DumpMaster` plus one addon) on its own event loop in a
background thread, on an ephemeral port, with its CA in a temporary directory. A
client reaches it the way an agent would: a stock boto3 client with no
`endpoint_url`, told only where the proxy is and which CA to trust. Any host the
addon does not claim is refused with 502, and `connection_strategy="lazy"` with
`upstream_cert=False` means no upstream connection is ever opened.

The provider's app runs on the proxy's loop, so everything that touches the run's
`SqliteStore` (a sqlite3 connection, bound to the thread that opened it) is run
there too, through `on_loop` and `await_on_loop`.

One proxy per process at a time: a second `DumpMaster` started while the first
was running served certificates its own CA did not sign, and the client refused
them. Two provider instances in one process share one proxy and are mounted in turn.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from pathlib import Path
from typing import TypeVar

from boto3.session import Session
from botocore.client import BaseClient
from botocore.config import Config
from mitmproxy import http, options
from mitmproxy.addons import asgiapp
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.tools.dump import DumpMaster

from minutehand.ports.provider import ASGIApp

T = TypeVar("T")


class _Serve:
    """The one addon: amazonaws.com goes to the mounted app; everything else is refused."""

    def __init__(self) -> None:
        self.app: ASGIApp | None = None
        self.refused: list[str] = []

    async def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        if self.app is not None and host.endswith(".amazonaws.com"):
            await asgiapp.serve(self.app, flow)
            return
        self.refused.append(host)
        flow.response = http.Response.make(502, b"no provider claims this host")


class AwsProxy:
    def __init__(self, confdir: Path) -> None:
        self._confdir = confdir
        self._serve = _Serve()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._master: DumpMaster | None = None
        self._failure: Exception | None = None
        self._thread = threading.Thread(target=self._main, name="aws-proxy", daemon=True)
        self.port = 0

    @property
    def ca(self) -> Path:
        return self._confdir / "mitmproxy-ca-cert.pem"

    @property
    def refused(self) -> list[str]:
        return self._serve.refused

    def start(self) -> AwsProxy:
        self._thread.start()
        if not self._ready.wait(timeout=20):
            raise TimeoutError("the proxy did not start listening within 20 seconds")
        if self._failure is not None:
            raise RuntimeError("the proxy failed to start") from self._failure
        return self

    def _main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as e:  # surfaced through start()
            self._failure = e
            self._ready.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        opts = options.Options(listen_host="127.0.0.1", listen_port=0, confdir=str(self._confdir))
        master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        master.options.update(connection_strategy="lazy", upstream_cert=False)
        master.addons.add(self._serve)
        self._master = master
        running = asyncio.create_task(master.run())
        server = master.addons.get("proxyserver")
        assert isinstance(server, Proxyserver)
        for _ in range(400):
            addresses = server.listen_addrs()
            if addresses and self.ca.exists():
                self.port = addresses[0][1]
                self._ready.set()
                break
            await asyncio.sleep(0.025)
        await running

    def mount(self, app: ASGIApp) -> None:
        self._serve.app = app

    def on_loop(self, fn: Callable[[], T]) -> T:
        """Run `fn` on the proxy's loop thread and return what it returns."""

        async def call() -> T:
            return fn()

        return self.await_on_loop(call())

    def await_on_loop(self, coro: Coroutine[object, object, T]) -> T:
        assert self._loop is not None
        future: Future[T] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)

    def client(self, service: str, region: str = "us-east-1") -> BaseClient:
        """A stock boto3 client: dummy credentials, the proxy, the CA. No endpoint_url."""
        session = Session(
            aws_access_key_id="test", aws_secret_access_key="test", region_name=region
        )
        return session.client(
            service,
            config=Config(proxies={"https": f"http://127.0.0.1:{self.port}"}, retries={"max_attempts": 1}),
            verify=str(self.ca),
        )

    def stop(self) -> None:
        if self._loop is not None and self._master is not None:
            self._loop.call_soon_threadsafe(self._master.shutdown)
        self._thread.join(timeout=10)
