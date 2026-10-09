"""An agent's own product for the inbox tests: approvals waiting on people, over real HTTP on 127.0.0.1.

    GET  /approvals?approver=<email>[&cursor=N]   that person's pending approvals, `page` at a time
    GET  /everyone[?cursor=N]                     everyone's, each naming its approver
    POST /approvals/<id>/decision                  {"decision": "approve"|"reject", "reason": "..."}

Every call carries `Authorization: Bearer <token>`, and a person's list and decisions need that person's token. The
product keeps every call it was sent, with its headers, so a test can see what Minutehand sent and as whom. `refuse`
makes it answer every decision 409, as a product that will not take one does.
"""

from __future__ import annotations

import json
import socketserver
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


@dataclass
class Approval:
    id: str
    approver: str
    summary: str
    operation: str
    state: str = "pending"
    reason: str = ""


@dataclass
class Sent:
    method: str
    path: str
    authorization: str
    body: str


@dataclass
class Product:
    tokens: dict[str, str]
    page: int = 10
    refuse: bool = False
    numbered: bool = False
    """Answer each summary as a number: the product's contract changed under whoever reads it."""
    approvals: dict[str, Approval] = field(default_factory=dict)
    sent: list[Sent] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    """Each note left on an approval, by its id: what an approver writes without deciding."""
    on_decided: Callable[[Approval], None] | None = None
    """What the product does while it handles a decision, before it answers: going ahead with what it approves."""
    base: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def raise_approval(self, approval_id: str, approver: str, summary: str, operation: str) -> None:
        with self.lock:
            self.approvals[approval_id] = Approval(approval_id, approver, summary, operation)

    def take_back(self, approval_id: str) -> None:
        with self.lock:
            del self.approvals[approval_id]

    def state(self, approval_id: str) -> str:
        with self.lock:
            return self.approvals[approval_id].state if approval_id in self.approvals else "gone"

    def pending(self, approver: str | None) -> list[Approval]:
        with self.lock:
            return [
                a
                for a in self.approvals.values()
                if a.state == "pending" and (approver is None or a.approver == approver)
            ]


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def _handler(product: Product) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, status: int, payload: object) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _who(self) -> str | None:
            given = self.headers.get("authorization", "").removeprefix("Bearer ")
            return next((email for email, token in product.tokens.items() if token == given), None)

        def _listed(self, items: list[Approval], cursor: int, *, approver: bool) -> dict[str, object]:
            chunk = items[cursor : cursor + product.page]
            listed = [
                {
                    "id": a.id,
                    "summary": len(a.summary) if product.numbered else a.summary,
                    "operation": a.operation,
                    "actions": ["approve", "reject"],
                }
                | ({"approver": a.approver} if approver else {})
                for a in chunk
            ]
            following = cursor + product.page
            return {"items": listed, "next": str(following) if following < len(items) else None}

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            product.sent.append(Sent("GET", self.path, self.headers.get("authorization", ""), ""))
            who = self._who()
            cursor = int(query["cursor"]) if "cursor" in query else 0
            if parts.path == "/approvals":
                if who is None or query.get("approver") != who:
                    self._send(401, {"error": "not signed in as that approver"})
                    return
                self._send(200, self._listed(product.pending(who), cursor, approver=False))
            elif parts.path == "/inboxes/approvals/pending":  # Minutehand's default shape
                if who is None or query.get("person") != who:
                    self._send(401, {"error": "not signed in as that person"})
                    return
                page = self._listed(product.pending(who), cursor, approver=False)
                page["items"] = [
                    {"id": i["id"], "summary": i["summary"], "decisions": i["actions"]}
                    for i in page["items"]  # type: ignore[union-attr]
                ]
                self._send(200, page)
            elif parts.path == "/everyone":
                if who is None:
                    self._send(401, {"error": "not signed in"})
                    return
                self._send(200, self._listed(product.pending(None), cursor, approver=True))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
            product.sent.append(Sent("POST", self.path, self.headers.get("authorization", ""), raw))
            pieces = urlsplit(self.path).path.strip("/").split("/")
            if pieces[:3] == ["inboxes", "approvals", "items"] and len(pieces) == 5:  # Minutehand's default shape
                made = json.loads(raw)
                pieces = ["approvals", pieces[3], "decision"]
                raw = json.dumps({"decision": made["decision"], "reason": made["inputs"].get("reason", "")})
            if len(pieces) != 3 or pieces[0] != "approvals" or pieces[2] != "decision":
                self._send(404, {"error": "not found"})
                return
            with product.lock:
                found = product.approvals.get(pieces[1])
            who = self._who()
            if found is None:
                self._send(404, {"error": "no such approval"})
            elif who != found.approver:
                self._send(403, {"error": "not this approver's"})
            elif product.refuse or found.state != "pending":
                self._send(409, {"error": "this approval cannot be decided now"})
            elif json.loads(raw)["decision"] == "note":
                with product.lock:
                    product.notes.append((found.id, str(json.loads(raw)["text"])))
                self._send(200, {"ok": True, "state": found.state})
            else:
                body = json.loads(raw)
                with product.lock:
                    found.state = "approved" if body["decision"] == "approve" else "rejected"
                    found.reason = str(body.get("reason", ""))
                if product.on_decided is not None:
                    product.on_decided(found)
                self._send(200, {"ok": True, "state": found.state})

    return Handler


@contextmanager
def serving(product: Product) -> Iterator[Product]:
    server = _Server(("127.0.0.1", 0), _handler(product))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    product.base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield product
    finally:
        server.shutdown()
        server.server_close()
