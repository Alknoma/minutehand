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
- After that `Finished`, the server's ticket flight is the leading run of its `application_data` records that are
  one length, each between `TICKET_SMALLEST` and `TICKET_LARGEST` bytes, at most `TICKETS_AT_MOST` of them. A
  server's tickets share one encoding, so they share one encrypted length; the first record that breaks the run
  answers, whether it arrives in the same read as the tickets or in a later one. The flight is judged record by
  record, never by read: how the bytes were cut into reads is the proxy's timing, not the server's.
- A request the client sends once records of the flight have arrived ends the flight: HTTP/1.1 sends no second
  request before the first is answered.

What real model hosts send on a fresh connection over HTTP/1.1, read from the headers (record lengths in bytes):
Cloudflare (`api.anthropic.com`, `api.openai.com` and others) and Google (`generativelanguage.googleapis.com`) send
one ticket only with their first answer, in the same read (`477`, then the answer's `675, 22, 19`); AWS
(`bedrock-runtime.*`) sends one ticket of `174` alone right after the `Finished`; some hosts send none; OpenSSL,
and so Python's `ssl`, sends two alone (`250, 250`).

What the record headers cannot tell, and so remains:
- A server that sends no tickets and answers the first request with one record that alone fits the flight (headers
  and body in one write, up to `TICKET_LARGEST` bytes) looks like a server that sends one ticket alone, as AWS does.
  It is held as awaiting until the client sends again or closes the connection: a checkpoint is not restorable (a
  refusal) rather than taken while a call is in flight, and the burst is written then, or at the run's end, not
  when it falls quiet. Releasing it after a bounded wait would release AWS's first call on every new connection
  however long its answer takes.
- An answer whose first records happen to have the tickets' length is read as answering from the first that
  differs; a server that sends no tickets and streams records of one length is read as answering at its fifth.
- A server that sends its tickets before the client's `Finished` (half-RTT tickets) has none after it: its first
  answer is judged as above.
- An HTTP/2 server's own preface (`SETTINGS`) and its acknowledgement of the client's read as an answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CHANGE_CIPHER_SPEC = 20
ALERT = 21
HANDSHAKE = 22
APPLICATION_DATA = 23
CONTENT_TYPES = frozenset({20, 21, 22, 23, 24})
HEADER = 5

TICKET_SMALLEST = 35
"""The shortest encrypted record a `NewSessionTicket` fits in (RFC 8446 4.6.1): its 4-byte header, lifetime, age
add, an empty nonce, a 1-byte ticket and no extensions (18 bytes), the inner content type and a 16-byte tag."""
TICKET_LARGEST = 2048
"""The longest record read as a ticket: every ticket seen from a model host or OpenSSL was 174 to 597 bytes."""
TICKETS_AT_MOST = 4
"""The most tickets one flight is read to hold: OpenSSL and BoringSSL send two, Cloudflare, Google and AWS one."""


@dataclass(frozen=True)
class _Record:
    """One TLS record bytes carried part of: its content type and length from its header, and whether that header
    was in these bytes (`begun`) or the record was begun in earlier ones."""

    kind: int
    length: int
    begun: bool


@dataclass
class _Records:
    """One direction's stream cut into TLS records, as far as their headers say; `broken` once it is not TLS."""

    header: bytearray = field(default_factory=bytearray)
    left: int = 0
    current: _Record | None = None
    broken: bool = False

    def feed(self, content: bytes) -> list[_Record]:
        """Each record `content` carries a byte of, in order: a record begun in earlier bytes and continued here
        comes first, not `begun`."""
        touched: list[_Record] = []
        at = 0
        if self.left and self.current is not None and content:
            touched.append(_Record(self.current.kind, self.current.length, begun=False))
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
            self.current = _Record(kind, length, begun=True)
            self.left = length
            touched.append(self.current)
        return touched


@dataclass
class Tunnel:
    """One tunnelled connection: whether the agent's last request on it is still awaiting its answer."""

    host: str
    asked: bool = False
    tls13: bool = False
    _after_tickets: bool = False
    _client_finished: bool = False
    _in_finished: bool = False
    _flight: bool = False
    _tickets: int = 0
    _ticket_length: int | None = None
    _in_ticket: bool = False
    _client: _Records = field(default_factory=_Records)
    _server: _Records = field(default_factory=_Records)

    def moved(self, content: bytes, *, from_client: bool) -> bool:
        """Bytes went one way: from the agent, a request (or a handshake message) now awaits its answer; from the
        server, the answer came, unless every record they carry is of the TLS 1.3 server's session tickets. True
        unless the bytes are TLS records that carry neither a request nor an answer: an alert (a `close_notify`)
        or a cipher change."""
        records = self._client.feed(content) if from_client else self._server.feed(content)
        if from_client:
            self._from_client(records)
        else:
            self._from_server(records)
        if self._client.broken or self._server.broken:
            return True
        return not {r.kind for r in records} <= {ALERT, CHANGE_CIPHER_SPEC}

    def _from_client(self, records: list[_Record]) -> None:
        if self._client.broken or self._server.broken:
            self.asked = True
            return
        asking = False
        for record in records:
            if not record.begun:
                asking = asking or (record.kind in (HANDSHAKE, APPLICATION_DATA) and not self._in_finished)
                continue
            self._in_finished = False
            if record.kind == APPLICATION_DATA and not self._client_finished:
                self._client_finished = True
                if self.tls13:
                    self._flight = self._in_finished = True
                    continue  # TLS 1.3's Finished: the end of the handshake, not a request
                asking = True
            elif record.kind in (HANDSHAKE, APPLICATION_DATA):
                asking = True
                if record.kind == APPLICATION_DATA and self._tickets:
                    self._flight = False  # a request after the flight began: what came before it answered
        if asking:
            self.asked = True

    def _from_server(self, records: list[_Record]) -> None:
        if self._client.broken or self._server.broken:
            self.asked = False
            return
        answered = ticketed = False
        for record in records:
            if record.kind == APPLICATION_DATA and not self._client_finished:
                self.tls13 = True
            if self._ticket(record):
                ticketed = True
            else:
                answered = True
        if answered:
            self.asked = False
            self._after_tickets = False
        elif ticketed:
            self._after_tickets = self.asked  # the session tickets, sent unasked: what was asked is unanswered

    def _ticket(self, record: _Record) -> bool:
        """Whether `record` is of the server's ticket flight, which it extends or ends (module docstring)."""
        if not record.begun:
            return self._in_ticket
        self._in_ticket = False
        if not self._flight:
            return False
        if (
            record.kind == APPLICATION_DATA
            and TICKET_SMALLEST <= record.length <= TICKET_LARGEST
            and self._tickets < TICKETS_AT_MOST
            and self._ticket_length in (None, record.length)
        ):
            self._tickets += 1
            self._ticket_length = record.length
            self._in_ticket = True
            return True
        self._flight = False
        return False

    def awaiting(self) -> str | None:
        """For a person, what the agent awaits on this tunnel, or None."""
        if not self.asked:
            return None
        if self._after_tickets:
            return f"a request on the new TLS 1.3 tunnel to {self.host}, unanswered since the server's session tickets"
        return f"a request on the open tunnel to {self.host}"
