"""`replay`: a call answered from an earlier run's recording, matched on method, host, path, query and the body's
hash with declared fields left out; on a miss, passed through and kept, or refused, as declared."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minutehand.adapters.proxy.capture import Asked, Capturing, Recordings, replaying_for, write_recordings
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.outbound import OnMiss, PassThrough, RecordedRun, RecordingsDirectory, Replay
from minutehand.domain.world import AnsweredBy, CaptureMode
from minutehand.session import RUNS
from tests.capture.support import START, V6, Answered, Call, by_environment
from tests.proxy.upstream import Authority, Upstream, model_api

JSON = {"content-type": "application/json"}


async def _record(
    registry: Registry, tmp_path: Path, authority: Authority, real: Upstream, calls: list[Call], into: Path
) -> list[Answered]:
    """An earlier run: `calls` passed through to the real host, its recordings written into `into`."""
    clock = RunClock(START)
    into.mkdir(parents=True)
    store = SqliteStore(into / "world.db", "earlier", clock)
    capturing = Capturing([PassThrough(host=V6)])
    async with Proxy(
        Routing(registry), store, clock, confdir=tmp_path / "ca", upstream_ca=authority.ca_cert, capturing=capturing
    ) as proxy:
        answered = await by_environment(proxy, calls)
    write_recordings(into, store.calls())
    store.close()
    return answered


async def _replay(
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    authority: Authority,
    declared: Replay,
    calls: list[Call],
) -> list[Answered]:
    capturing = Capturing([declared], replaying=replaying_for([declared], state=tmp_path / "state"))
    async with Proxy(
        Routing(registry), store, clock, confdir=tmp_path / "ca", upstream_ca=authority.ca_cert, capturing=capturing
    ) as proxy:
        return await by_environment(proxy, calls)


def _search(port: int, query: str = "q=pricing") -> Call:
    return Call("GET", f"https://[::1]:{port}/search?{query}")


async def test_a_replayed_call_is_answered_from_the_recording_and_marked_so(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    recordings = tmp_path / "recordings"
    async with model_api(authority, host=V6) as real:
        [recorded] = await _record(registry, tmp_path, authority, real, [_search(real.port)], recordings)
        declared = Replay(host=V6, source=RecordingsDirectory(directory=str(recordings)))
        [replayed] = await _replay(registry, store, clock, tmp_path, authority, declared, [_search(real.port)])
    assert len(real.received) == 1  # the earlier run's call only
    assert (replayed.status, replayed.body) == (recorded.status, recorded.body)
    assert replayed.header("x-minutehand-replayed") == f"recordings in {recordings}"
    [call] = store.calls()
    assert call.exchange.captured is not None
    captured = call.exchange.captured
    assert (captured.mode, captured.answered_by, captured.replayed_from) == (
        CaptureMode.REPLAY,
        AnsweredBy.RECORDING,
        f"recordings in {recordings}",
    )


async def test_a_miss_is_passed_through_and_kept_when_declared_so(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    recordings = tmp_path / "recordings"
    async with model_api(authority, host=V6) as real:
        await _record(registry, tmp_path, authority, real, [_search(real.port)], recordings)
        declared = Replay(host=V6, source=RecordingsDirectory(directory=str(recordings)), on_miss=OnMiss.PASS_THROUGH)
        [missed] = await _replay(
            registry, store, clock, tmp_path, authority, declared, [_search(real.port, "q=something+else")]
        )
    assert (missed.status, json.loads(missed.body)) == (200, {"received": 2})
    [call] = store.calls()
    assert call.exchange.captured is not None
    assert (call.exchange.captured.mode, call.exchange.captured.answered_by) == (
        CaptureMode.REPLAY,
        AnsweredBy.REAL_HOST,
    )
    assert (
        call.exchange.captured.note is not None
        and "holds no GET ::1/search with this query" in call.exchange.captured.note
    )


async def test_a_miss_declared_to_refuse_is_refused_without_reaching_the_real_host(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    recordings = tmp_path / "recordings"
    async with model_api(authority, host=V6) as real:
        await _record(registry, tmp_path, authority, real, [_search(real.port)], recordings)
        declared = Replay(host=V6, source=RecordingsDirectory(directory=str(recordings)))
        [refused] = await _replay(
            registry, store, clock, tmp_path, authority, declared, [_search(real.port, "q=something+else")]
        )
    assert refused.status == 502 and "no recording answers this call" in json.loads(refused.body)["error"]
    assert len(real.received) == 1
    [call] = store.calls()
    assert call.exchange.captured is not None and call.exchange.captured.answered_by is AnsweredBy.REFUSAL
    assert not call.refused  # captured: it was declared, and answered as its declaration says


@pytest.mark.parametrize("ignoring", [True, False])
async def test_declared_fields_are_left_out_of_the_match_and_nothing_else_is(
    ignoring: bool, registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    """A timestamp in the body and a nonce in the query differ between the runs; the rest of the call does not."""
    recordings = tmp_path / "recordings"

    def lookup(port: int, n: int) -> Call:
        body = json.dumps({"q": "pricing", "sent_at": f"2026-08-2{n}T10:00:00Z"})
        return Call("POST", f"https://[::1]:{port}/search?nonce={n}&q=pricing", body, JSON)

    async with model_api(authority, host=V6) as real:
        [recorded] = await _record(registry, tmp_path, authority, real, [lookup(real.port, 1)], recordings)
        declared = Replay(
            host=V6,
            source=RecordingsDirectory(directory=str(recordings)),
            ignore_query=["nonce"] if ignoring else [],
            ignore_body=["sent_at"] if ignoring else [],
        )
        [again] = await _replay(registry, store, clock, tmp_path, authority, declared, [lookup(real.port, 2)])
    if ignoring:
        assert (again.status, again.body) == (recorded.status, recorded.body)
    else:
        assert again.status == 502
    assert len(real.received) == 1


async def test_the_nth_identical_call_gets_the_nth_answer_and_the_last_after_that(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    """Recorded by run id: the real host answered the same call twice with a different count each time."""
    state = tmp_path / "state"
    async with model_api(authority, host=V6) as real:
        await _record(
            registry, tmp_path, authority, real, [_search(real.port), _search(real.port)], state / RUNS / "earlier"
        )
        declared = Replay(host=V6, source=RecordedRun(run="earlier"))
        answers = await _replay(registry, store, clock, tmp_path, authority, declared, [_search(real.port)] * 3)
    assert [json.loads(a.body)["received"] for a in answers] == [1, 2, 2]
    assert len(real.received) == 2


def test_a_replay_of_a_run_that_kept_no_recordings_is_refused_at_load(tmp_path: Path) -> None:
    declared = Replay(host=V6, source=RecordedRun(run="nowhere"))
    with pytest.raises(FileNotFoundError, match="no recordings for run nowhere"):
        replaying_for([declared], state=tmp_path)


def test_recordings_name_why_a_call_does_not_match() -> None:
    empty = Recordings(source="run x", calls=[])

    found, why = empty.answer(
        Asked(method="GET", host="h", path="/p?a=1", body=None, content_type=None, raw_digest=""),
        ignore_query=[],
        ignore_body=[],
    )
    assert found is None and why == "run x holds no GET h/p with this query"
