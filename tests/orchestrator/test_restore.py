"""The procedure that puts the agent's own state back: settle, the restore sequence, and the verify step."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from minutehand.application.checkpoint import NotRestorable
from minutehand.application.refusals import AgentFailed, RunRefused
from minutehand.application.restore import (
    UNCONFIRMED,
    RestoreFailed,
    RestoreStep,
    SeenCall,
    Settled,
    differences,
    restore_agent,
    settle,
)
from minutehand.domain.agent import AgentReport, AgentStatus, Commitment, CommitmentStatus, StateHooks, WaitingOn

T0 = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
UNUSED = Path(__file__).parent
"""Where a busy command would run; these hooks declare none."""


class Calls:
    """`Traffic`: the calls a test makes the agent seem to make."""

    def __init__(self) -> None:
        self.seen: SeenCall | None = None
        self.open: list[str] = []

    def last_call(self) -> SeenCall | None:
        return self.seen

    def waiting(self) -> list[str]:
        return list(self.open)

    def call(self, what: str = "GET chat.test/inbox") -> None:
        self.seen = SeenCall(at=time.monotonic(), what=what)


class Answers:
    """`Reports`: answers each ask with the next of `reports` (the last one again once they run out); an
    exception in the list is raised instead."""

    def __init__(self, *reports: AgentReport | AgentFailed) -> None:
        self._reports = list(reports)
        self.asked = 0

    async def report(self) -> AgentReport:
        self.asked += 1
        answer = self._reports[0] if len(self._reports) == 1 else self._reports.pop(0)
        if isinstance(answer, AgentFailed):
            raise answer
        return answer


def hooks(**fields: object) -> StateHooks:
    base: dict[str, object] = {"snapshot": ["true"], "restore": ["true"], "quiet": timedelta(seconds=0.2)}
    base.update(fields)
    return StateHooks.model_validate(base)


IDLE = AgentReport(status=AgentStatus.IDLE, next_wake=T0 + timedelta(days=2))


# -- settle ---------------------------------------------------------------------------------------------------


async def test_a_call_inside_the_quiet_period_delays_the_checkpoint() -> None:
    calls = Calls()

    async def late_call() -> None:
        await asyncio.sleep(0.15)
        calls.call()

    began = time.monotonic()
    background = asyncio.create_task(late_call())
    settled = await settle(hooks(quiet=timedelta(seconds=0.3)), calls, Answers(IDLE), None, directory=UNUSED)
    await background

    assert settled == Settled(report=IDLE, unconfirmed=UNCONFIRMED)
    # quiet is measured from the call at 0.15 s, not from when settling began
    assert time.monotonic() - began >= 0.45


async def test_with_no_call_the_checkpoint_waits_the_quiet_period_once() -> None:
    began = time.monotonic()
    settled = await settle(hooks(quiet=timedelta(seconds=0.2)), Calls(), Answers(IDLE), None, directory=UNUSED)
    assert settled == Settled(report=IDLE, unconfirmed=UNCONFIRMED)
    assert 0.2 <= time.monotonic() - began < 1.0


async def test_a_checkpoint_that_cannot_settle_is_not_restorable_with_its_reason() -> None:
    calls = Calls()
    stop = asyncio.Event()

    async def keeps_calling() -> None:
        while not stop.is_set():
            calls.call("POST chat.test/messages")
            await asyncio.sleep(0.05)

    background = asyncio.create_task(keeps_calling())
    began = time.monotonic()
    settled = await settle(
        hooks(quiet=timedelta(seconds=0.2), settle_limit=timedelta(seconds=0.6)),
        calls,
        Answers(IDLE),
        None,
        directory=UNUSED,
    )
    stop.set()
    await background

    assert isinstance(settled, NotRestorable)
    assert "still making outbound calls when the settle limit (0.6 s) ran out" in settled.reason
    assert "POST chat.test/messages" in settled.reason
    assert time.monotonic() - began < 1.0


async def test_an_agent_still_working_is_settled_only_once_it_stops() -> None:
    working = AgentReport(status=AgentStatus.WORKING)
    answers = Answers(working, working, IDLE)
    settled = await settle(hooks(quiet=timedelta(0)), Calls(), answers, None, directory=UNUSED)
    assert settled == Settled(report=IDLE, unconfirmed=UNCONFIRMED) and answers.asked == 3


async def test_an_agent_working_past_the_settle_limit_is_not_restorable() -> None:
    settled = await settle(
        hooks(quiet=timedelta(0), settle_limit=timedelta(seconds=0.3)),
        Calls(),
        Answers(AgentReport(status=AgentStatus.WORKING)),
        None,
        directory=UNUSED,
    )
    assert isinstance(settled, NotRestorable) and "still reported WORKING" in settled.reason


async def test_an_agent_that_cannot_be_asked_settles_on_quiet_alone_with_its_last_report() -> None:
    settled = await settle(hooks(quiet=timedelta(seconds=0.05)), Calls(), None, IDLE, directory=UNUSED)
    assert settled == Settled(report=IDLE, unconfirmed=UNCONFIRMED)


async def test_a_report_endpoint_that_fails_while_settling_is_not_restorable() -> None:
    settled = await settle(
        hooks(quiet=timedelta(0)), Calls(), Answers(AgentFailed("GET /report answered 500")), None, directory=UNUSED
    )
    assert isinstance(settled, NotRestorable) and "answered 500" in settled.reason


# -- the restore sequence -------------------------------------------------------------------------------------


def step(log: Path, name: str, *, exit_code: int = 0, prints: str = "") -> list[str]:
    """A hook command that appends `name` to `log`, prints, and exits with `exit_code`."""
    code = (
        "import sys, pathlib\n"
        f"p = pathlib.Path({str(log)!r}); p.write_text(p.read_text() + {name + chr(10)!r} if p.exists() else {name + chr(10)!r})\n"
        f"print({prints!r}); sys.exit({exit_code})"
    )
    return [sys.executable, "-c", code]


class Program:
    """`OwnProgram`: logs its stops and starts beside the hooks; `refuses` makes `start` fail as an agent's
    command that exits does."""

    def __init__(self, log: Path, *, refuses: bool = False) -> None:
        self._log = log
        self._refuses = refuses

    @property
    def command(self) -> Sequence[str]:
        return ["python", "agent.py"]

    def _write(self, line: str) -> None:
        self._log.write_text((self._log.read_text() if self._log.exists() else "") + line + "\n")

    async def stop(self) -> None:
        self._write("program stopped")

    async def start(self) -> None:
        if self._refuses:
            raise RunRefused("the agent's command exited 4 before http://127.0.0.1:1/wake accepted connections")
        self._write("program started")


def sequence(log: Path, **failing: int) -> StateHooks:
    return hooks(
        stop=step(log, "stop", exit_code=failing.get("stop", 0), prints="stopping the agent"),
        restore=step(log, "restore", exit_code=failing.get("restore", 0), prints="no snapshot there"),
        start=step(log, "start", exit_code=failing.get("start", 0), prints="the agent would not start"),
        answer_limit=timedelta(seconds=0.5),
    )


async def test_the_restore_stops_restores_starts_waits_and_verifies_in_that_order(tmp_path: Path) -> None:
    log = tmp_path / "log"
    said: list[str] = []

    restored = await restore_agent(
        sequence(log),
        tmp_path / "snapshot",
        checkpoint_seq=7,
        recorded=IDLE,
        reports=Answers(AgentFailed("connection refused"), IDLE),
        own=Program(log),
        progress=said.append,
    )

    assert log.read_text().splitlines() == ["program stopped", "stop", "restore", "start", "program started"]
    assert restored.verified and restored.checkpoint_seq == 7
    assert [s.step for s in restored.steps] == [
        RestoreStep.STOP,
        RestoreStep.STOP,
        RestoreStep.RESTORE,
        RestoreStep.START,
        RestoreStep.START,
        RestoreStep.ANSWER,
        RestoreStep.VERIFY,
    ]
    assert restored.steps[1].output.strip() == "stopping the agent" and restored.steps[1].exit_code == 0
    assert said[0] == "stop: stopping the agent's command, python agent.py"
    assert said[-1] == "verify: the report equals the one at the checkpoint"


@pytest.mark.parametrize(
    ("failing", "ran", "shown"),
    [
        ("stop", ["stop"], "stopping the agent"),
        ("restore", ["stop", "restore"], "no snapshot there"),
        ("start", ["stop", "restore", "start"], "the agent would not start"),
    ],
)
async def test_a_failing_step_is_refused_naming_the_step_and_showing_its_output(
    tmp_path: Path, failing: str, ran: list[str], shown: str
) -> None:
    log = tmp_path / "log"
    with pytest.raises(RestoreFailed) as refused:
        await restore_agent(
            sequence(log, **{failing: 3}),
            tmp_path / "snapshot",
            checkpoint_seq=7,
            recorded=IDLE,
            reports=Answers(IDLE),
        )
    message = str(refused.value)
    assert f"from the checkpoint at seq 7 failed at step `{failing}`" in message
    assert "exited 3" in message and message.endswith(f"Its output:\n{shown}")
    assert log.read_text().splitlines() == ran
    assert refused.value.steps[-1].step is RestoreStep(failing)


async def test_a_command_that_cannot_be_started_fails_its_step(tmp_path: Path) -> None:
    with pytest.raises(RestoreFailed, match=r"failed at step `restore`: /nonexistent/restore exited 127"):
        await restore_agent(
            hooks(restore=["/nonexistent/restore"]), tmp_path, checkpoint_seq=1, recorded=IDLE, reports=None
        )


async def test_a_command_past_the_step_limit_is_killed_and_fails_its_step(tmp_path: Path) -> None:
    with pytest.raises(RestoreFailed, match=r"(?s)failed at step `stop`.*killed after the step limit"):
        await restore_agent(
            hooks(stop=[sys.executable, "-c", "import time; time.sleep(30)"], step_limit=timedelta(seconds=0.3)),
            tmp_path,
            checkpoint_seq=1,
            recorded=IDLE,
            reports=None,
        )


async def test_the_agents_own_program_that_does_not_start_fails_the_start_step(tmp_path: Path) -> None:
    with pytest.raises(RestoreFailed) as refused:
        await restore_agent(
            sequence(tmp_path / "log"),
            tmp_path,
            checkpoint_seq=2,
            recorded=IDLE,
            reports=Answers(IDLE),
            own=Program(tmp_path / "log", refuses=True),
        )
    assert "failed at step `start`: python agent.py failed" in str(refused.value)
    assert "exited 4 before http://127.0.0.1:1/wake" in str(refused.value)


async def test_an_agent_that_never_answers_after_the_restore_fails_the_answer_step(tmp_path: Path) -> None:
    with pytest.raises(RestoreFailed) as refused:
        await restore_agent(
            sequence(tmp_path / "log"),
            tmp_path,
            checkpoint_seq=2,
            recorded=IDLE,
            reports=Answers(AgentFailed("GET http://127.0.0.1:1/report: ConnectError")),
        )
    message = str(refused.value)
    assert "failed at step `answer`: its report endpoint did not answer within the answer limit (0.5 s)" in message
    assert "ConnectError" in message


# -- verify ---------------------------------------------------------------------------------------------------


def commitment(key: str, status: CommitmentStatus = CommitmentStatus.OPEN, **more: object) -> Commitment:
    fields: dict[str, object] = {
        "key": key,
        "description": f"waiting on {key}",
        "waiting_on": WaitingOn.PERSON,
        "opened_at": T0,
        "status": status,
    }
    fields.update(more)
    return Commitment.model_validate(fields)


async def test_a_restore_whose_report_differs_is_refused_field_by_field(tmp_path: Path) -> None:
    at_checkpoint = AgentReport(
        status=AgentStatus.IDLE,
        next_wake=T0 + timedelta(days=2),
        commitments=[commitment("rosa"), commitment("legal"), commitment("budget")],
    )
    after = AgentReport(
        status=AgentStatus.DONE,
        next_wake=None,
        commitments=[commitment("rosa", CommitmentStatus.MET), commitment("budget"), commitment("venue")],
    )
    with pytest.raises(RestoreFailed) as refused:
        await restore_agent(hooks(), tmp_path, checkpoint_seq=9, recorded=at_checkpoint, reports=Answers(after))
    assert str(refused.value).splitlines()[1:6] == [
        "  status: idle at the checkpoint, done after the restore",
        "  next_wake: 2026-08-26T10:00:00+00:00 at the checkpoint, none after",
        "  commitment legal: open at the checkpoint, missing after the restore",
        "  commitment rosa: open at the checkpoint, met after",
        "  commitment venue: absent at the checkpoint, open after the restore",
    ]
    assert refused.value.steps[-1].step is RestoreStep.VERIFY


def test_equal_is_the_same_status_instant_and_commitments_by_key_and_status() -> None:
    plus_two = timezone(timedelta(hours=2))
    recorded = AgentReport(
        status=AgentStatus.IDLE, next_wake=T0, commitments=[commitment("rosa", description="ask Rosa")]
    )
    same = AgentReport(
        status=AgentStatus.IDLE,
        next_wake=T0.astimezone(plus_two),
        commitments=[commitment("rosa", description="Rosa, again", expected_by=T0 + timedelta(days=1))],
    )
    assert differences(recorded, same) == []
    assert differences(AgentReport(status=AgentStatus.IDLE), AgentReport(status=AgentStatus.IDLE, commitments=[])) == []
    assert differences(recorded, same.model_copy(update={"next_wake": T0 + timedelta(seconds=1)})) != []


async def test_an_agent_without_a_report_endpoint_is_restored_unverified_and_says_why(tmp_path: Path) -> None:
    restored = await restore_agent(hooks(), tmp_path, checkpoint_seq=3, recorded=IDLE, reports=None)
    assert not restored.verified
    assert restored.unverified is not None and "no report endpoint" in restored.unverified
    assert [s.step for s in restored.steps] == [RestoreStep.RESTORE]


async def test_a_busy_command_holds_the_checkpoint_until_it_says_idle(tmp_path: Path) -> None:
    """The proxy and the report say nothing is happening; the agent's own `busy` says a job is still running (a
    database write on this machine the proxy never sees). The checkpoint waits for it, and is then confirmed."""
    flag = tmp_path / "busy"
    flag.write_text("1")
    busy = [sys.executable, "-c", f"import pathlib, sys; sys.exit(0 if pathlib.Path({str(flag)!r}).exists() else 1)"]

    async def finish() -> None:
        await asyncio.sleep(0.4)
        flag.unlink()

    began = time.monotonic()
    background = asyncio.create_task(finish())
    settled = await settle(
        hooks(quiet=timedelta(seconds=0.05), busy=busy), Calls(), Answers(IDLE), None, directory=tmp_path
    )
    await background

    assert settled == Settled(report=IDLE)
    assert time.monotonic() - began >= 0.4


async def test_a_busy_command_still_busy_at_the_limit_is_not_restorable(tmp_path: Path) -> None:
    busy = [sys.executable, "-c", "print('job 4 running'); raise SystemExit(0)"]
    settled = await settle(
        hooks(quiet=timedelta(0), settle_limit=timedelta(seconds=0.5), busy=busy),
        Calls(),
        Answers(IDLE),
        None,
        directory=tmp_path,
    )
    assert isinstance(settled, NotRestorable)
    assert "busy command still said it was busy" in settled.reason and "job 4 running" in settled.reason


async def test_a_call_awaiting_its_answer_holds_the_checkpoint() -> None:
    """A request sent on (a model call on a tunnel already open) and not yet answered: no new call is seen while
    it waits, and the checkpoint is held until the answer comes."""
    calls = Calls()
    calls.open.append("a request on the open tunnel to model.example")

    async def answered() -> None:
        await asyncio.sleep(0.4)
        calls.open.clear()

    began = time.monotonic()
    background = asyncio.create_task(answered())
    settled = await settle(hooks(quiet=timedelta(seconds=0.05)), calls, Answers(IDLE), None, directory=UNUSED)
    await background

    assert isinstance(settled, Settled)
    assert time.monotonic() - began >= 0.4


async def test_a_call_still_awaiting_its_answer_at_the_limit_is_not_restorable_naming_it() -> None:
    calls = Calls()
    calls.open.append("a request on the open tunnel to model.example")
    settled = await settle(
        hooks(quiet=timedelta(0), settle_limit=timedelta(seconds=0.3)), calls, Answers(IDLE), None, directory=UNUSED
    )
    assert isinstance(settled, NotRestorable)
    assert "a request on the open tunnel to model.example" in settled.reason


async def test_the_report_kept_is_the_one_after_the_busy_command_says_idle(tmp_path: Path) -> None:
    """The report says IDLE while a job still runs; the job ends and changes it to DONE before the busy command
    reports idle. Before, the stale IDLE was kept, and a run whose agent had finished was woken again a day later."""
    done = AgentReport(status=AgentStatus.DONE)
    flag = tmp_path / "busy"
    flag.write_text("1")
    answers = Answers(IDLE, IDLE, done)
    busy = [
        sys.executable,
        "-c",
        f"import pathlib, sys; f = pathlib.Path({str(flag)!r}); e = f.exists(); f.unlink(missing_ok=True); sys.exit(0 if e else 1)",
    ]

    settled = await settle(hooks(quiet=timedelta(0), busy=busy), Calls(), answers, None, directory=tmp_path)

    assert settled == Settled(report=done)
