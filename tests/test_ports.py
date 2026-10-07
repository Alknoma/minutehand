"""The ports tests hand their agents: never one another worker hands out, never one in use."""

from __future__ import annotations

import socket

import pytest

from tests import ports


def test_two_workers_never_hand_out_the_same_port(monkeypatch: pytest.MonkeyPatch) -> None:
    handed: dict[str, set[int]] = {}
    for worker in ("gw0", "gw1", "gw7"):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
        monkeypatch.setattr(ports, "_next", __import__("itertools").count())
        handed[worker] = {ports.free_port() for _ in range(50)}
    assert len(handed["gw0"]) == 50
    assert not handed["gw0"] & handed["gw1"]
    assert not handed["gw1"] & handed["gw7"]


def test_every_port_handed_out_is_below_the_systems_ephemeral_range() -> None:
    assert all(ports.free_port() < 32768 for _ in range(20))


def test_a_port_something_is_listening_on_is_passed_over(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "_next", __import__("itertools").count())
    first = ports.free_port()
    with socket.socket() as held:
        held.bind(("127.0.0.1", first + 1))
        held.listen()
        assert ports.free_port() == first + 2
