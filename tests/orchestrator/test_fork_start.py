"""The agent at the start of a fork (`application.restore.start_fork`): its memory proven the checkpoint's, its
program started once the fork exists, and its report compared with the one recorded there."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from minutehand.application import restore
from minutehand.application.checkpoint import Remembered
from minutehand.application.refusals import AgentFailed, RunRefused
from minutehand.application.restore import RestoreFailed, RestoreStep, Verification, differences, start_fork
from minutehand.domain.agent import AgentReport, AgentStatus, Commitment, CommitmentStatus, WaitingOn

T0 = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
IDLE = AgentReport(status=AgentStatus.IDLE, next_wake=T0 + timedelta(days=2))
MEMORY = "a" * 64


def remembered(report: AgentReport | None = IDLE, *, outside: list[str] | None = None) -> Remembered:
    return Remembered(report=report, memory=MEMORY, outside=outside or [])


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


class Program:
    """`OwnProgram`: logs its stops and starts; `refuses` makes `start` fail as an agent's command that exits does."""

    def __init__(self, log: Path, *, refuses: bool = False) -> None:
        self._log = log
        self._refuses = refuses

    @property
    def command(self) -> Sequence[str]:
        return ["python", "agent.py"]

    def _write(self, line: str) -> None:
        self._log.write_text((self._log.read_text() if self._log.exists() else "") + line + "\n")

    async def stop(self) -> None:
        self._write("stopped")

    async def start(self) -> None:
        if self._refuses:
            raise RunRefused("the agent's command exited 4 before http://127.0.0.1:1/wake accepted connections")
        self._write("started")


async def test_a_fork_proves_its_memory_starts_the_agent_then_compares_its_report(tmp_path: Path) -> None:
    said: list[str] = []
    answers = Answers(AgentFailed("connection refused"), IDLE)
    restored = await start_fork(
        remembered(),
        memory=MEMORY,
        checkpoint_seq=7,
        reports=answers,
        own=Program(tmp_path / "log"),
        progress=said.append,
    )
    assert restored.verified and restored.verified_by == [Verification.MEMORY, Verification.REPORT]
    assert [s.step for s in restored.steps] == [
        RestoreStep.MEMORY,
        RestoreStep.START,
        RestoreStep.ANSWER,
        RestoreStep.VERIFY,
    ]
    assert (tmp_path / "log").read_text().splitlines() == ["stopped", "started"]
    assert answers.asked == 2
    assert said[-1] == "verify: the report equals the one at the checkpoint"


async def test_a_fork_whose_memory_is_not_the_checkpoints_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RestoreFailed, match=r"the fork's memory is not the agent's memory at the checkpoint at seq 3"):
        await start_fork(
            remembered(), memory="b" * 64, checkpoint_seq=3, reports=Answers(IDLE), own=Program(tmp_path / "log")
        )
    assert not (tmp_path / "log").exists(), "nothing was started for a fork refused on its memory"


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


async def test_a_report_that_differs_is_refused_field_by_field_naming_state_outside_the_store() -> None:
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
    held = remembered(at_checkpoint, outside=["the agent's own database in AGENT_DB held 8,192 bytes"])
    with pytest.raises(RestoreFailed) as refused:
        await start_fork(held, memory=MEMORY, checkpoint_seq=9, reports=Answers(after))
    message = str(refused.value)
    assert message.splitlines()[1:6] == [
        "  status: idle at the checkpoint, done after the fork",
        "  next_wake: 2026-08-26T10:00:00+00:00 at the checkpoint, none after",
        "  commitment legal: open at the checkpoint, missing after the fork",
        "  commitment rosa: open at the checkpoint, met after",
        "  commitment venue: absent at the checkpoint, open after the fork",
    ]
    assert restore.OUTSIDE in message
    assert "At the checkpoint the run saw: the agent's own database in AGENT_DB held 8,192 bytes." in message
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


async def test_an_agent_without_a_report_endpoint_starts_with_its_memory_proven_and_its_report_unverified() -> None:
    restored = await start_fork(remembered(), memory=MEMORY, checkpoint_seq=3, reports=None)
    assert not restored.verified and restored.verified_by == [Verification.MEMORY]
    assert restored.unverified is not None and "no report endpoint" in restored.unverified


async def test_the_agents_own_program_that_does_not_start_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RestoreFailed, match="the fork's agent did not start: the agent's command exited 4"):
        await start_fork(
            remembered(),
            memory=MEMORY,
            checkpoint_seq=2,
            reports=Answers(IDLE),
            own=Program(tmp_path / "l", refuses=True),
        )


async def test_an_agent_that_never_answers_its_report_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restore, "ANSWER_LIMIT", timedelta(seconds=0.3))
    with pytest.raises(RestoreFailed, match=r"(?s)did not answer within 0 s.*ConnectError"):
        await start_fork(
            remembered(),
            memory=MEMORY,
            checkpoint_seq=2,
            reports=Answers(AgentFailed("GET http://127.0.0.1:1/report: ConnectError")),
        )
