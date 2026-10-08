"""A service's endpoint on loopback for what a messaging provider pushes (events, interactions), answering 200 to
everything and keeping each push's body."""

from __future__ import annotations

import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Receiver:
    url: str
    pushed: list[bytes] = field(default_factory=list)


class _Loopback(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        """Skip the reverse DNS lookup of this machine's name `HTTPServer.server_bind` makes, slow on macOS."""
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


@contextmanager
def receiver() -> Iterator[Receiver]:
    found: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"] or 0)
            found.append(self.rfile.read(length))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            return

    server = _Loopback(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield Receiver(url=f"http://127.0.0.1:{server.server_address[1]}/events", pushed=found)
    finally:
        server.shutdown()
        server.server_close()
