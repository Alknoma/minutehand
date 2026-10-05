"""An external emulator is started and stopped with its user, refused before anything starts when it never comes
up, and, when it dies or stops answering mid-run, never hangs the agent and never lets a call through to the real
host: the call is answered 502 or 504 naming it and kept as unavailable, which is not the service refusing. Its
answers are told apart: a faithful error, its own failure, an operation it has not implemented."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.adapters.emulator import answers
from minutehand.adapters.emulator.process import EmulatorRefused
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.emulators import findings, health_changes
from minutehand.application.outbound import emulator_uses
from minutehand.application.run_clock import RunClock
from minutehand.domain.checks import FindingKind
from minutehand.domain.emulator import (
    EmulatorHealth,
    ErrorMarker,
    ExternalEmulator,
    Health,
    ReadyHttp,
    ReadyLog,
    Upstream,
)
from minutehand.domain.outbound import Forward
from minutehand.domain.world import AnsweredBy, CallOutcome
from tests.emulators.support import Reply, forwarding, raw_upstream, stand_in
from tests.proxy.support import client

HOST = "api.tracker.test"


def _started(tmp_path: Path, **changes: object) -> ExternalEmulator:
    declared = ExternalEmulator(
        name="tracker",
        upstream=Upstream(url="http://127.0.0.1:{port}"),
        command=stand_in(tmp_path),
        ready=ReadyHttp(path="/ready"),
        ready_within=timedelta(seconds=10),
        health=Health(path="/ready", every=timedelta(milliseconds=100), fails=1),
    )
    return declared.model_copy(update=changes)


async def _refused(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return True
    writer.close()
    return False


async def test_a_started_emulator_is_ready_before_use_and_stopped_after(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with forwarding(
        registry, store, clock, tmp_path, [_started(tmp_path)], [Forward(host=HOST, emulator="tracker")]
    ) as running:
        [emulator] = running.emulators.running.values()
        port = emulator.where.port
        assert port is not None and not await _refused(port)
        async with client(running.proxy, running.proxy.ca_cert) as http:
            got = await http.get(f"https://{HOST}/issues")
        assert got.json() == {"ok": True, "world": store.run_id}
    assert await _refused(port)
    assert [h.health for h in health_changes(store, "tracker")] == [EmulatorHealth.READY, EmulatorHealth.STOPPED]


async def test_an_emulator_that_never_comes_up_is_refused_with_the_end_of_its_log(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEVER_READY", "1")
    declared = _started(tmp_path, ready=ReadyLog(line="^listening on"), ready_within=timedelta(seconds=1))
    with pytest.raises(EmulatorRefused) as refused:
        async with forwarding(registry, store, clock, tmp_path, [declared], [Forward(host=HOST, emulator="tracker")]):
            pytest.fail("the emulator never came up, and was used")
    said = str(refused.value)
    assert "emulator tracker was not ready" in said and "within 1 s" in said
    assert "stand-in will never listen" in said
    assert health_changes(store, "tracker") == []


async def test_an_emulator_that_dies_mid_run_is_unavailable_and_never_hangs_the_agent(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIES_AFTER", "2")  # the readiness check and the first call; the second kills it
    clock.begin_wake()
    declared = _started(tmp_path, health=Health(path="/ready", every=timedelta(seconds=30)))
    async with forwarding(
        registry, store, clock, tmp_path, [declared], [Forward(host=HOST, emulator="tracker")]
    ) as running:
        async with client(running.proxy, running.proxy.ca_cert) as http:
            first = await http.get(f"https://{HOST}/issues")
            dying = await http.post(f"https://{HOST}/issues", json={"title": "x"})
            after = await http.get(f"https://{HOST}/issues")
        failure = running.emulators.failure()
    assert first.status_code == 200
    assert dying.status_code == 502 and "external emulator tracker is unavailable" in dying.json()["error"]
    assert dying.json()["emulator"] == "tracker"
    assert after.status_code == 502 and failure is not None
    calls = [c for c in store.calls() if c.exchange.captured is not None]
    assert [c.exchange.outcome for c in calls] == [
        CallOutcome.ANSWERED,
        CallOutcome.UNAVAILABLE,
        CallOutcome.UNAVAILABLE,
    ]
    assert all(c.exchange.captured and c.exchange.captured.answered_by is AnsweredBy.REFUSAL for c in calls[1:])
    [use] = emulator_uses(store.calls())
    assert (use.answered, use.unavailable) == (1, 2) and use.first_unavailable == f"POST {HOST}/issues (wake 1)"
    [unavailable] = [f for f in findings(store) if f.kind is FindingKind.FAIL]
    assert f"the first call it failed: POST {HOST}/issues at wake 1, answered 502" in unavailable.message
    assert "dying on request 3" in unavailable.message  # the end of its log


async def test_an_emulator_that_stops_answering_is_answered_504(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with raw_upstream(Reply(silent=True)) as raw:
        declared = ExternalEmulator(
            name="tracker",
            upstream=Upstream(url=f"http://127.0.0.1:{raw.port}"),
            answer_within=timedelta(milliseconds=500),
            health=Health(every=timedelta(seconds=30)),
        )
        async with forwarding(
            registry, store, clock, tmp_path, [declared], [Forward(host=HOST, emulator="tracker")]
        ) as running:
            async with client(running.proxy, running.proxy.ca_cert) as http:
                got = await http.get(f"https://{HOST}/slow")
    assert got.status_code == 504 and "did not answer within 0.5 s" in got.json()["error"]
    [kept] = [c for c in store.calls() if c.exchange.captured is not None]
    assert kept.exchange.outcome is CallOutcome.UNAVAILABLE
    assert [h.health for h in health_changes(store, "tracker")][-2:] == [
        EmulatorHealth.UNHEALTHY,
        EmulatorHealth.STOPPED,
    ]


GRAPHQL_ERROR = '{"errors": [{"message": "no", "extensions": {"code": "%s"}}]}'


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, '{"data": {}}', CallOutcome.ANSWERED),
        (404, '{"error": "no such issue"}', CallOutcome.REFUSED),
        (503, '{"error": "rate limited"}', CallOutcome.REFUSED),  # declared faithful
        (500, '{"error": "boom"}', CallOutcome.INTERNAL_ERROR),
        (501, "", CallOutcome.NOT_IMPLEMENTED),
        (200, GRAPHQL_ERROR % "NOT_IMPLEMENTED", CallOutcome.NOT_IMPLEMENTED),
        (200, GRAPHQL_ERROR % "BAD_USER_INPUT", CallOutcome.REFUSED),
    ],
)
def test_an_emulators_answers_are_told_apart(status: int, body: str, expected: CallOutcome) -> None:
    declared = ExternalEmulator(
        name="tracker",
        upstream=Upstream(url="http://127.0.0.1:1"),
        faithful=[ErrorMarker(status=503), ErrorMarker(at="errors[*].extensions.code", equals="BAD_USER_INPUT")],
        not_implemented=[
            ErrorMarker(status=501),
            ErrorMarker(at="errors[*].extensions.code", equals="NOT_IMPLEMENTED"),
        ],
    )
    assert answers.outcome(declared, status, body, "application/json") is expected


async def test_operations_the_emulator_has_not_implemented_are_counted_and_listed_for_review(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    reply = Reply(head=b"HTTP/1.1 501 Not Implemented\r\ncontent-length: 0\r\n\r\n", chunks=[])
    async with raw_upstream(reply) as raw:
        declared = ExternalEmulator(name="tracker", upstream=Upstream(url=f"http://127.0.0.1:{raw.port}"))
        async with forwarding(
            registry, store, clock, tmp_path, [declared], [Forward(host=HOST, emulator="tracker")]
        ) as running:
            async with client(running.proxy, running.proxy.ca_cert) as http:
                for _ in range(2):
                    await http.post(f"https://{HOST}/graphql", json={"query": "mutation IssueArchive { x }"})
                await http.post(f"https://{HOST}/graphql", json={"operationName": "CycleCreate", "query": "{}"})
    [use] = emulator_uses(store.calls())
    assert [(o.operation, o.calls) for o in use.not_implemented] == [("IssueArchive", 2), ("CycleCreate", 1)]
    said = findings(store)
    assert [f.kind for f in said] == [FindingKind.REVIEW, FindingKind.REVIEW]
    assert "has no implementation of IssueArchive: 2 call(s)" in said[0].message
