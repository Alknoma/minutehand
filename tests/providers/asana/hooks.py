"""A webhook target on loopback that answers Asana's handshake as the reference requires and keeps what it is sent."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Delivery:
    headers: dict[str, str]
    body: bytes

    def events(self) -> list[Any]:
        found = json.loads(self.body)["events"]
        assert isinstance(found, list)
        return found


@dataclass
class Target:
    url: str
    handshakes: list[str] = field(default_factory=list)
    deliveries: list[Delivery] = field(default_factory=list)
    echo: bool = True
    status: int = 200


class _Loopback(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        import socketserver

        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


@contextmanager
def target() -> Iterator[Target]:
    held: list[Target] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            mine = held[0]
            raw = self.rfile.read(int(self.headers["Content-Length"] or 0))
            secret = self.headers["X-Hook-Secret"]
            if secret is not None:
                mine.handshakes.append(secret)
                self.send_response(200)
                if mine.echo:
                    self.send_header("X-Hook-Secret", secret)
            else:
                mine.deliveries.append(Delivery({k: v for k, v in self.headers.items()}, raw))
                self.send_response(mine.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = _Loopback(("127.0.0.1", 0), Handler)
    found = Target(url=f"http://127.0.0.1:{server.server_address[1]}/receive/7654")
    held.append(found)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield found
    finally:
        server.shutdown()
        server.server_close()
