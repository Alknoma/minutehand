"""An external emulator of the test's own: any program on a port. It answers every call 200 with what it was asked,
and with a limit given, it dies (exit 1, no answer) on the call after that many.

    python emulator.py PORT [LIMIT]
"""

import json
import os
import socketserver
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else None
answered = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def answer(self) -> None:
        global answered
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        if self.path != "/health":
            if LIMIT is not None and answered >= LIMIT:
                os._exit(1)
            answered += 1
        said = json.dumps({"method": self.command, "path": self.path, "body": body.decode(errors="replace")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(said)))
        self.end_headers()
        self.wfile.write(said)

    do_GET = do_POST = do_DELETE = answer


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    Server(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
