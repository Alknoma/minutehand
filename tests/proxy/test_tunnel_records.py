"""Whether a request on a new TLS 1.3 tunnel is answered, judged from record headers alone, for the shapes the bytes
were seen to take: each read below is the record lengths one read carried, as captured from this suite's model API
(Python's `ssl`, two tickets of 250) and from real model hosts (`tunnel` module docstring).

Before, the first server read with `application_data` after the client's `Finished` was taken whole as the ticket
flight: tickets and the answer in one read, or an answer with no tickets before it, left the request awaiting, and
the burst was neither written when it fell quiet nor a checkpoint restorable.
"""

from __future__ import annotations

import pytest

from minutehand.adapters.proxy.tunnel import (
    APPLICATION_DATA,
    CHANGE_CIPHER_SPEC,
    HANDSHAKE,
    TICKET_LARGEST,
    TICKET_SMALLEST,
    TICKETS_AT_MOST,
    Tunnel,
)

Read = list[tuple[int, int]]

CLIENT_HELLO: Read = [(HANDSHAKE, 1526)]
SERVER_FLIGHT: Read = [(HANDSHAKE, 1210), (CHANGE_CIPHER_SPEC, 1), *[(APPLICATION_DATA, n) for n in (23, 475, 96, 69)]]
FINISHED: Read = [(CHANGE_CIPHER_SPEC, 1), (APPLICATION_DATA, 69)]
REQUEST: Read = [(APPLICATION_DATA, 210), (APPLICATION_DATA, 31)]


def _bytes(read: Read) -> bytes:
    return b"".join(bytes([kind, 3, 3]) + length.to_bytes(2) + bytes(length) for kind, length in read)


def _data(*lengths: int) -> Read:
    return [(APPLICATION_DATA, n) for n in lengths]


def _asked() -> Tunnel:
    """A new TLS 1.3 tunnel whose client has sent its `Finished` and then its first request."""
    tunnel = Tunnel("model.localhost")
    tunnel.moved(_bytes(CLIENT_HELLO), from_client=True)
    tunnel.moved(_bytes(SERVER_FLIGHT), from_client=False)
    tunnel.moved(_bytes(FINISHED), from_client=True)
    tunnel.moved(_bytes(REQUEST), from_client=True)
    assert tunnel.tls13 and tunnel.awaiting() is not None
    return tunnel


def _server(tunnel: Tunnel, *reads: Read) -> list[bool]:
    """Whether the request still awaits its answer after each read from the server."""
    awaited: list[bool] = []
    for read in reads:
        tunnel.moved(_bytes(read), from_client=False)
        awaited.append(tunnel.awaiting() is not None)
    return awaited


def test_tickets_alone_leave_the_request_awaiting_and_the_answer_after_them_answers_it() -> None:
    tunnel = _asked()
    assert _server(tunnel, _data(250, 250), _data(103)) == [True, False]


def test_tickets_and_the_answer_in_one_read_answer_the_request() -> None:
    """The read `tests/proxy/test_tunnelled_calls.py` failed on under load: two tickets and the answer at once."""
    assert _server(_asked(), _data(250, 250, 103)) == [False]


def test_one_ticket_sent_with_the_first_answer_answers_the_request() -> None:
    """Cloudflare's shape: one ticket only with the first answer, then the answer's header, body and end."""
    assert _server(_asked(), _data(477, 675, 22, 19)) == [False]


def test_an_answer_with_no_tickets_before_it_answers_the_request() -> None:
    """A server that sends no tickets: the answer's head and its body, in one read."""
    assert _server(_asked(), _data(384, 120)) == [False]


def test_tickets_in_two_writes_are_both_tickets() -> None:
    tunnel = _asked()
    assert _server(tunnel, _data(250), _data(250), _data(103)) == [True, True, False]


def test_a_ticket_cut_across_two_reads_is_a_ticket() -> None:
    tunnel = _asked()
    whole = _bytes(_data(250, 250))
    for part in (whole[:3], whole[3:200], whole[200:]):
        tunnel.moved(part, from_client=False)
        assert tunnel.awaiting() is not None
    assert _server(tunnel, _data(103)) == [False]


def test_one_ticket_alone_leaves_the_request_awaiting_until_the_answer() -> None:
    """AWS's shape: one ticket alone right after the `Finished`, the answer later."""
    tunnel = _asked()
    assert _server(tunnel, _data(174)) == [True]
    assert _server(tunnel, _data(204, 19)) == [False]


def test_a_flight_ends_at_most_tickets() -> None:
    """A server with no tickets streaming records of one length is read as answering by the record after the most
    tickets a flight holds."""
    tunnel = _asked()
    awaited = _server(tunnel, *[_data(1386)] * (TICKETS_AT_MOST + 1))
    assert awaited == [True] * TICKETS_AT_MOST + [False]


@pytest.mark.parametrize("length", [TICKET_SMALLEST - 1, TICKET_LARGEST + 1], ids=["shorter", "longer"])
def test_a_lone_record_no_ticket_fits_answers_the_request(length: int) -> None:
    assert _server(_asked(), _data(length)) == [False]


def test_a_lone_answer_that_fits_the_flight_is_held_until_the_client_asks_again() -> None:
    """What the headers cannot tell (`tunnel` module docstring): no tickets and an answer in one record that fits
    the flight. The client's next request ends the flight, and the answer to it, of the same length, answers it."""
    tunnel = _asked()
    assert _server(tunnel, _data(384)) == [True]
    tunnel.moved(_bytes(REQUEST), from_client=True)
    assert _server(tunnel, _data(384)) == [False]


@pytest.mark.parametrize("read", [_data(250, 250), _data(103)], ids=["tickets-shaped", "answer-shaped"])
def test_tls_1_2_has_no_ticket_flight(read: Read) -> None:
    """In TLS 1.2 the client's first `application_data` record is its request: whatever the server sends answers."""
    tunnel = Tunnel("model.localhost")
    tunnel.moved(_bytes(CLIENT_HELLO), from_client=True)
    tunnel.moved(_bytes([(HANDSHAKE, 1210), (HANDSHAKE, 900)]), from_client=False)
    tunnel.moved(_bytes([(HANDSHAKE, 37), (CHANGE_CIPHER_SPEC, 1), (HANDSHAKE, 40)]), from_client=True)
    tunnel.moved(_bytes([(CHANGE_CIPHER_SPEC, 1), (HANDSHAKE, 40)]), from_client=False)
    tunnel.moved(_bytes(REQUEST), from_client=True)
    assert not tunnel.tls13 and tunnel.awaiting() is not None
    assert _server(tunnel, read) == [False]
