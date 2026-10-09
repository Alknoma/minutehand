"""Ports for the agents and servers a test starts, which no other test running at the same time is handed.

Asking the system for a free port (binding port 0) and closing it races: the port goes back to the ephemeral
range, where the next process asking for one, a proxy Minutehand starts or another worker's agent, can take it
before the test's own process binds it ("Address already in use"). Here each pytest-xdist worker counts through
a block of its own below the ephemeral range (Linux starts it at 32768, macOS at 49152), so no two workers ever
hand out the same port and the system never hands out these.

The blocks sit below `minutehand run-all`'s own range (`run_all.PORTS`, from 20000), which a run-all test's runs
pick from at random, and there is one for each of the first hundred workers. Blocks above 20000, twenty of them
taken by the worker's number modulo twenty, gave gw0 and gw20 the same ports and put every block in run-all's
range: two agents handed one port, and a wake sent to the other test's agent.
"""

from __future__ import annotations

import itertools
import os
import socket

_FIRST = 10000
_BLOCK = 100
_BLOCKS = 100


def _worker() -> int:
    name = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    return int(name.removeprefix("gw")) if name.startswith("gw") and name[2:].isdigit() else 0


_next = itertools.count()


def free_port() -> int:
    """The next port of this worker's block that nothing is listening on."""
    worker = _worker()
    if worker >= _BLOCKS:
        raise RuntimeError(f"pytest-xdist worker {worker}: ports are blocked out for the first {_BLOCKS} workers only")
    start = _FIRST + worker * _BLOCK
    for _ in range(_BLOCK):
        port = start + next(_next) % _BLOCK
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"every port in {start}..{start + _BLOCK - 1} is taken")
