"""An echo server of the test's own, standing for an external emulator or a real upstream: it answers every call 200
with exactly what it received (method, path, every header in order, the body as base64). With a limit it dies (exit
1, no answer) on the call after that many; with a certificate it serves HTTPS.

    python emulator.py PORT [--limit N] [--tls CERT KEY]
"""

import argparse
import base64
import json
import os
import socketserver
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("port", type=int)
parser.add_argument("--limit", type=int)
parser.add_argument("--tls", nargs=2)
ARGS = parser.parse_args()
answered = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def answer(self) -> None:
        global answered
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        if self.path != "/health":
            if ARGS.limit is not None and answered >= ARGS.limit:
                os._exit(1)
            answered += 1
        said = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "headers": [[k, v] for k, v in self.headers.items()],
                "body": base64.b64encode(body).decode(),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(said)))
        self.end_headers()
        self.wfile.write(said)

    do_GET = do_POST = do_PUT = do_DELETE = answer


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    server = Server(("127.0.0.1", ARGS.port), Handler)
    if ARGS.tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*ARGS.tls)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
