"""The conformance suite's server and fixtures.

The suite is an outside consumer: it starts `minutehand serve` as its own process (the installed command, on free
loopback ports, its state in a temporary directory) and reaches it through the shipped pytest plugin, which reads
`MINUTEHAND_URL` when its session fixture is first set up. The proxy then runs in that process, never in this one,
so the suite shares a worker with the tests that start a proxy in-process.

Every case is marked `conformance`; a case listed in `known_failures.py` is expected to fail, strictly."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from minutehand.testing.client import MinutehandClient
from minutehand.testing.plugin import URL_VARIABLE
from tests.conformance.harness import ABSENT_PROPERTY, Case, Harness, Server, minutehand_serve
from tests.conformance.known_failures import KNOWN

HERE = Path(__file__).parent
KNOWN_BY_CASE = {(k.provider, k.prop, k.case): k for k in KNOWN}


@pytest.fixture(scope="session")
def conformance_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """The server process, its URL put where the plugin reads it until the plugin's client is made."""
    with minutehand_serve(tmp_path_factory.mktemp("conformance-serve")) as server:
        os.environ[URL_VARIABLE] = server.url
        try:
            yield server
        finally:
            os.environ.pop(URL_VARIABLE, None)


@pytest.fixture(scope="session")
def minutehand(conformance_server: Server, minutehand: MinutehandClient) -> MinutehandClient:
    """The plugin's own `minutehand` (requested by the same name), made against the server process; the variable
    is put back at once so nothing else in this process sees it."""
    os.environ.pop(URL_VARIABLE, None)
    assert minutehand.url == conformance_server.url, "the plugin did not reach the server process"
    return minutehand


@pytest.fixture
def harness(minutehand: MinutehandClient) -> Harness:
    return Harness(minutehand)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if not item.path.is_relative_to(HERE):
            continue
        item.add_marker(pytest.mark.conformance)
        callspec = getattr(item, "callspec", None)
        params = callspec.params if callspec is not None else {}
        case = params["case"] if "case" in params else None
        if isinstance(case, Case):
            known = KNOWN_BY_CASE.get((case.provider, case.prop, case.case))
            if known is not None:
                item.add_marker(pytest.mark.xfail(strict=True, reason=f"known failure: {known.observed}"))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    declared = sorted(
        {
            str(value)
            for kind in ("passed", "failed", "xfailed")
            for report in terminalreporter.stats.get(kind, [])
            for name, value in getattr(report, "user_properties", [])
            if name == ABSENT_PROPERTY
        }
    )
    if declared:
        terminalreporter.section("conformance: declared vendor exceptions (not applicable)")
        for line in declared:
            terminalreporter.line(line)
