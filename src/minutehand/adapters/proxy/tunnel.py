"""What the bytes on a tunnel the proxy never decrypts can tell about whether the agent is awaiting an answer.

A tunnel to a model API is relayed as bytes (`ProxyAddon.next_layer`). The bytes are encrypted, but TLS frames
them in records whose five-byte headers are in the clear: a content type, a version, a length. That is enough to
tell the one thing that made the last bytes' direction a wrong answer: on a new TLS 1.3 connection the server sends
its session tickets (`NewSessionTicket`) after it reads the client's `Finished`, unasked, and a client that sends its
first request right after its `Finished` sees them arrive after the request, where they read as its answer.

- TLS 1.3 is known by the server sending an encrypted record (`application_data`, 23) before the client has sent
  one: its handshake flight after `ServerHello` is encrypted. In TLS 1.2 no `application_data` record moves before
  the client's first request.
- The client's first `application_data` record on TLS 1.3 is its `Finished`, not a request.
- The first message of `application_data` the server sends after that `Finished` is its ticket flight: it does not
  answer anything. Only server bytes after it answer the request.

What the record headers cannot tell, and so remains: a server that sends no tickets and answers the first request
so fast that the answer is read as the ticket flight; a server that sends its tickets in two separate writes read
as two messages; an HTTP/2 server's own preface. The first holds the tunnel as awaiting an answer until the client
sends again or closes the connection, a refusal rather than a wrong checkpoint; the other two read as an answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

HANDSHAKE = 22
APPLICATION_DATA = 23
CONTENT_TYPES = frozenset({20, 21, 22, 23, 24})
HEADER = 5


@dataclass
class _Records:
    """One direction's stream cut into TLS records, as far as their headers say; `broken` once it is not TLS."""

    header: bytearray = field(default_factory=bytearray)
    left: int = 0
    current: int | None = None
    broken: bool = False

    def feed(self, content: bytes) -> list[int]:
        """The content type of each record `content` carries a byte of, in order: a record begun in an earlier
        message and continued here counts once more."""
        touched: list[int] = []
        at = 0
        if self.left and self.current is not None:
            touched.append(self.current)
        while at < len(content) and not self.broken:
            if self.left:
                step = min(self.left, len(content) - at)
                self.left -= step
                at += step
                continue
            need = HEADER - len(self.header)
            self.header += content[at : at + need]
            at += min(need, len(content) - at)
            if len(self.header) < HEADER:
                break
            kind, major, length = self.header[0], self.header[1], int.from_bytes(self.header[3:5])
            self.header.clear()
            if kind not in CONTENT_TYPES or major != 3:
                self.broken = True
                break
            self.current = kind
            self.left = length
            touched.append(kind)
        return touched


@dataclass
class Tunnel:
    """One tunnelled connection: whether the agent's last request on it is still awaiting its answer."""

    host: str
    asked: bool = False
    tls13: bool = False
    tickets_due: bool = False
    after_tickets: bool = False
    _client_finished: bool = False
    _client: _Records = field(default_factory=_Records)
    _server: _Records = field(default_factory=_Records)

    def moved(self, content: bytes, *, from_client: bool) -> None:
        """Bytes went one way: from the agent, a request (or a handshake message) now awaits its answer; from the
        server, the answer came, unless these are the TLS 1.3 server's session tickets."""
        if from_client:
            self._from_client(content)
        else:
            self._from_server(content)

    def _from_client(self, content: bytes) -> None:
        records = self._client.feed(content)
        if self._client.broken or self._server.broken:
            self.asked = True
            return
        asking = False
        for kind in records:
            if kind == APPLICATION_DATA and not self._client_finished:
                self._client_finished = True
                if self.tls13:
                    self.tickets_due = True
                    continue  # TLS 1.3's Finished: the end of the handshake, not a request
                asking = True
            elif kind in (HANDSHAKE, APPLICATION_DATA):
                asking = True
        if asking:
            self.asked = True

    def _from_server(self, content: bytes) -> None:
        records = self._server.feed(content)
        if self._client.broken or self._server.broken:
            self.asked = False
            return
        if APPLICATION_DATA in records and not self._client_finished:
            self.tls13 = True
        if self.tickets_due and APPLICATION_DATA in records:
            self.tickets_due = False
            self.after_tickets = self.asked
            return  # the session tickets, sent unasked: whatever the agent asked is still unanswered
        self.asked = False
        self.after_tickets = False

    def awaiting(self) -> str | None:
        """For a person, what the agent awaits on this tunnel, or None."""
        if not self.asked:
            return None
        if self.after_tickets:
            return f"a request on the new TLS 1.3 tunnel to {self.host}, unanswered since the server's session tickets"
        return f"a request on the open tunnel to {self.host}"
