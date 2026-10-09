"""The agent's endpoint on loopback for what GitHub pushes: it keeps each delivery's headers and body and answers with the
status it is told to."""

from __future__ import annotations

import json
import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Delivery:
    headers: dict[str, str]
    body: bytes

    @property
    def event(self) -> str:
        return self.headers["x-github-event"]

    def payload(self) -> dict[str, object]:
        found = json.loads(self.body)
        assert isinstance(found, dict)
        return found


@dataclass
class Receiver:
    url: str
    status: int = 200
    pushed: list[Delivery] = field(default_factory=list[Delivery])


class _Loopback(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


@contextmanager
def receiver(status: int = 200) -> Iterator[Receiver]:
    found = Receiver(url="", status=status)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"] or 0)
            found.pushed.append(Delivery({k.lower(): v for k, v in self.headers.items()}, self.rfile.read(length)))
            self.send_response(found.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = _Loopback(("127.0.0.1", 0), Handler)
    found.url = f"http://127.0.0.1:{server.server_address[1]}/webhook"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield found
    finally:
        server.shutdown()
        server.server_close()
