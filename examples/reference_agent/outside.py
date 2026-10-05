"""The world outside the reference agent, on this machine: a model API and a venue search, each over HTTPS under a
certificate authority made here, so a run touches no real service.

    python outside.py --dir .outside            prints {"model": "https://model.localhost:…", "search": …, "ca": …} and serves

The model API answers OpenAI's chat completions shape, deterministically: what it answers depends only on the
request (the model asked for, the system prompt, the task in the user message), so five runs of one scenario get
five identical answers. `stream: true` is answered as server-sent events, one chunk per word. It is reached as
`model.localhost`, and Minutehand is told it is a model API with `--model-host model.localhost`.

The search is reached as `search.localhost`, a host the agent file declares `pass_through`.

Both listen on 127.0.0.1. A name under `localhost` reaches this machine and is still a host of its own: an agent
configured by Minutehand reaches `localhost` itself directly, and these through the proxy (with `httpx`;
`requests` and `urllib` read NO_PROXY=localhost as covering every name under it, and would go direct).

`MODEL_DELAY` (seconds, default 0) holds every model answer back that long: a model call in flight for a while.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socketserver
import ssl
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import BaseServer
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

VENUES = [
    {"name": "Lakeside Hall", "contact": "rosa@lakeside.example", "capacity": 80},
    {"name": "The Granary", "contact": "events@granary.example", "capacity": 40},
]
REFERENCE = re.compile(r"reference is ([A-Z]{2}-\d{3,6})")
DECLINED = re.compile(r"\b(taken|not available|unavailable|fully booked)\b", re.IGNORECASE)


# -- certificates --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Authority:
    ca: Path
    cert: Path
    key: Path


def authority(directory: Path) -> Authority:
    """A CA and a server certificate for every name the two servers are reached by, made once per directory."""
    made = Authority(directory / "ca.pem", directory / "server.pem", directory / "server.key")
    if made.ca.is_file() and made.cert.is_file() and made.key.is_file():
        return made
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "reference agent outside CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    names: list[x509.GeneralName] = [x509.DNSName(n) for n in ("localhost", "model.localhost", "search.localhost")]
    names += [x509.IPAddress(ipaddress.ip_address(a)) for a in ("127.0.0.1", "::1")]
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    made.ca.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    made.cert.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    made.key.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return made


# -- what the model answers ----------------------------------------------------------------------------------------


def decide(model: str, system: str, task: dict[str, object]) -> dict[str, object]:
    """The model's decision for one task. Deterministic: the same request is answered the same way every time.

    plan        which venue to ask and what to write; "planner-large" writes differently and asks for a reply
                within a day; a system prompt asking to "copy the owner" puts the owner on the email
    read_reply  whether the venue is confirmed, and the booking reference the reply holds
    follow_up   the reminder's text
    """
    kind = task["task"]
    if kind == "plan":
        venues = task["venues"]
        assert isinstance(venues, list) and venues
        venue = venues[0]
        assert isinstance(venue, dict)
        large = model == "planner-large"
        text = (
            f"Hello, could {venue['name']} host our team of 30 on Friday? Please confirm and send a booking reference."
            if not large
            else f"Good morning. We would like to book {venue['name']} for 30 people this Friday. Is it free? "
            "A booking reference would be appreciated."
        )
        decision: dict[str, object] = {
            "to": venue["contact"],
            "venue": venue["name"],
            "subject": f"Booking {venue['name']} for Friday",
            "text": text,
            "follow_up_after_hours": 24 if large else 48,
        }
        if "copy the owner" in system.lower():
            decision["cc"] = task["owner"]
        return decision
    if kind == "read_reply":
        reply = str(task["reply"])
        found = REFERENCE.search(reply)
        if DECLINED.search(reply) or found is None:
            return {
                "confirmed": False,
                "tell_owner": f"{task['venue']} cannot take us on Friday. Their answer: {reply}",
            }
        return {
            "confirmed": True,
            "reference": found.group(1),
            "tell_owner": f"{task['venue']} is confirmed for Friday, booking reference {found.group(1)}.",
        }
    if kind == "follow_up":
        return {"text": f"Following up on my question about {task['venue']} for Friday: is it free?"}
    return {"error": f"unknown task {kind}"}


def completion(body: dict[str, object]) -> tuple[str, str, dict[str, int]]:
    """The answer's text, the model that answered, and the token counts."""
    model = str(body["model"])
    messages = body["messages"]
    assert isinstance(messages, list)
    system = next((str(m["content"]) for m in messages if m["role"] in ("system", "developer")), "")
    user = next(str(m["content"]) for m in messages if m["role"] == "user")
    answer = json.dumps(decide(model, system, json.loads(user)), sort_keys=True)
    usage = {"prompt_tokens": len((system + user).split()), "completion_tokens": len(answer.split())}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return answer, model, usage


# -- the servers ---------------------------------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, format: str, *args: object) -> None:
        self.server.log.append(f"{self.command} {self.path}")

    def _json(self, status: int, payload: object) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/search":
            query = parse_qs(parts.query).get("q", [""])[0]
            self._json(200, {"query": query, "results": VENUES})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if urlsplit(self.path).path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        delay = float(os.environ.get("MODEL_DELAY", "0"))
        if delay:
            time.sleep(delay)
        answer, model, usage = completion(body)
        self.server.calls += 1
        made = f"chatcmpl-{self.server.calls}"
        if body.get("stream"):
            self._stream(made, model, answer)
            return
        self._json(
            200,
            {
                "id": made,
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                "usage": usage,
            },
        )

    def _stream(self, made: str, model: str, answer: str) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        words = answer.split(" ")
        for i, word in enumerate(words):
            piece = word if i == 0 else " " + word
            chunk = {"id": made, "model": model, "choices": [{"index": 0, "delta": {"content": piece}}]}
            self._chunk(f"data: {json.dumps(chunk)}\n\n".encode())
        done = {"id": made, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self._chunk(f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], family: int, context: ssl.SSLContext) -> None:
        self.address_family = family
        super().__init__(address, _Handler)
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self.log: list[str] = []
        self.calls = 0

    def server_bind(self) -> None:
        # HTTPServer.server_bind looks this machine's name up by reverse DNS, which takes over 30 seconds on some
        # Macs. The name is never used: bind and listen at once.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)

    def handle_error(self, request: object, client_address: object) -> None:
        """A client that hangs up mid-answer is not this server's error."""


@dataclass(frozen=True)
class Outside:
    model: str
    search: str
    ca: Path
    servers: tuple[BaseServer, ...]


@contextmanager
def serving(directory: Path) -> Iterator[Outside]:
    """Both servers, each on a port the system picks, until the block ends."""
    import socket

    made = authority(directory)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(made.cert, made.key)
    model = _Server(("127.0.0.1", 0), socket.AF_INET, context)
    search = _Server(("127.0.0.1", 0), socket.AF_INET, context)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (model, search)]
    for thread in threads:
        thread.start()
    try:
        yield Outside(
            model=f"https://model.localhost:{model.server_address[1]}",
            search=f"https://search.localhost:{search.server_address[1]}",
            ca=made.ca,
            servers=(model, search),
        )
    finally:
        for server in (model, search):
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=Path(".outside"))
    args = parser.parse_args()
    with serving(args.dir) as outside:
        print(json.dumps({"model": outside.model, "search": outside.search, "ca": str(outside.ca)}), flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            sys.exit(0)
