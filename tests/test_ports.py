"""The ports tests hand their agents: never one another worker hands out, never one in use."""

from __future__ import annotations

import itertools
import socket

import pytest

from minutehand import run_all
from tests import ports


def test_two_workers_never_hand_out_the_same_port(monkeypatch: pytest.MonkeyPatch) -> None:
    handed: dict[str, set[int]] = {}
    for worker in ("gw0", "gw1", "gw7"):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
        monkeypatch.setattr(ports, "_next", itertools.count())
        handed[worker] = {ports.free_port() for _ in range(50)}
    assert len(handed["gw0"]) == 50
    assert not handed["gw0"] & handed["gw1"]
    assert not handed["gw1"] & handed["gw7"]


def test_workers_twenty_apart_never_hand_out_the_same_port(monkeypatch: pytest.MonkeyPatch) -> None:
    handed: dict[str, set[int]] = {}
    for worker in ("gw0", "gw20", "gw40"):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
        monkeypatch.setattr(ports, "_next", itertools.count())
        handed[worker] = {ports.free_port() for _ in range(50)}
    assert not handed["gw0"] & handed["gw20"] and not handed["gw0"] & handed["gw40"]


def test_no_port_handed_out_is_one_run_all_picks_its_agents_ports_from(monkeypatch: pytest.MonkeyPatch) -> None:
    for worker in ("gw0", "gw19", "gw99"):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
        monkeypatch.setattr(ports, "_next", itertools.count())
        assert not {ports.free_port() for _ in range(120)} & set(run_all.PORTS)


def test_every_port_handed_out_is_below_the_systems_ephemeral_range() -> None:
    assert all(ports.free_port() < 32768 for _ in range(20))


def test_a_port_something_is_listening_on_is_passed_over(monkeypatch: pytest.MonkeyPatch) -> None:
    # count from the start of the block twice: the first free port, once held, must not be handed out again
    monkeypatch.setattr(ports, "_next", itertools.count())
    first = ports.free_port()
    with socket.socket() as held:
        held.bind(("127.0.0.1", first))
        held.listen()
        monkeypatch.setattr(ports, "_next", itertools.count())
        assert ports.free_port() != first
