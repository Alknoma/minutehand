"""An SQS long poll through the real proxy, from stock boto3 in the agent's own process, answered on the run's clock.

A poll that finds a message answers at once; one that would wait is held, and the run answers it when its clock
reaches the earlier of the wait's end and a message of the queue becoming visible: a delivery the scenario's
scheduler makes, or a visibility timeout running out. No poll waits its 20 seconds of real time.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.dues import due_entries
from minutehand.domain.agent import AgentUnderTest, Booked, Reported
from minutehand.domain.clock import DueClosed, DueEntry, DueSource
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Person, Scenario, Silent
from minutehand.domain.world import RecordedCall
from tests.e2e.support import OWNER, T0, free_port, world

AGENT = Path(__file__).parent / "agents" / "long_poll_agent.py"
SCENARIO = Scenario(
    name="long_polls",
    goal="Work the queue.",
    owner="owner",
    starts_at=T0,
    deadline_after=timedelta(days=1),
    people=[Person(key="owner", name="Olive Owner", email=OWNER, reply=Silent())],
)
WAIT = timedelta(seconds=20)


async def _played(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> tuple[list[RecordedCall], list[list[str]], list[DueEntry]]:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "agent")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    kept = tmp_path / "agent" / "state.json"
    agent = AgentUnderTest(
        name="long_poller", wakes=[Reported(wake_url=f"{base}/wake", report_url=f"{base}/report"), Booked()]
    )
    command = [sys.executable, str(AGENT), "--port", str(port), "--state", str(kept), "--mode", mode]
    state = tmp_path / "state"

    began = time.monotonic()
    [outcome] = await session.play(SCENARIO, agent, state=state, command=command)

    assert time.monotonic() - began < 15, "a poll waited its seconds of real time"
    assert outcome.record.stop is StopReason.NOTHING_PENDING
    store = world(state, outcome.record.run_id)
    polls = [c for c in store.calls() if c.exchange.request_body and '"WaitTimeSeconds": 20' in c.exchange.request_body]
    held = [e for e in due_entries(store) if e.source is DueSource.CALL]
    assert held, "no poll was held"
    assert all(e.due.at <= e.entered_at + WAIT for e in held), "held past the end of its wait"
    answers: list[list[str]] = json.loads(kept.read_text())["answers"]
    return polls, answers, held


def _bodies(call: RecordedCall) -> list[str]:
    answer = json.loads(call.exchange.response_body or "{}")
    return [m["Body"] for m in answer["Messages"]] if "Messages" in answer else []


async def test_a_long_poll_on_an_empty_queue_is_answered_empty_once_the_runs_clock_reaches_the_end_of_its_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    polls, answers, held = await _played(tmp_path, monkeypatch, "empty")

    assert [(c.sim_time, _bodies(c)) for c in polls] == [(T0 + WAIT * n, []) for n in (1, 2, 3)]
    assert answers == [[], [], []]
    assert [(e.entered_at, e.due.at, e.closed) for e in held] == [
        (T0 + WAIT * n, T0 + WAIT * (n + 1), DueClosed.FIRED) for n in (0, 1, 2)
    ], "each held from the moment the last ended to the end of its own wait"


async def test_a_long_poll_is_answered_when_the_scenarios_scheduler_delivers_to_its_queue_during_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    polls, answers, held = await _played(tmp_path, monkeypatch, "booked")

    assert [(c.sim_time, _bodies(c)) for c in polls] == [(T0 + timedelta(seconds=10), ["booked"])]
    assert answers == [["booked"]]
    assert [(e.due.at, e.closed, e.closed_at) for e in held] == [
        (T0 + WAIT, DueClosed.CANCELLED, T0 + timedelta(seconds=10))
    ], "answered before the end of its wait"


async def test_a_long_poll_is_answered_when_a_visibility_timeout_runs_out_during_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    polls, answers, held = await _played(tmp_path, monkeypatch, "visible")

    visible = T0 + timedelta(seconds=10, milliseconds=1)  # moto reads a message visible once now is past its timeout
    assert [(c.sim_time, _bodies(c)) for c in polls] == [(visible, ["again"])]
    assert answers == [["again"]]
    assert [(e.due.at, e.closed) for e in held] == [(visible, DueClosed.FIRED)]
