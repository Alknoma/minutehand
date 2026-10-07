"""The agent at the start of a fork: its memory checked, its program started, its report compared.

A fork needs nothing from the agent. Its memory is the run's log up to the checkpoint (`application.memory`), which
the fork shares with its parent, so nothing is copied, replayed or restarted for it. What is proven before the fork
plays on:

- **Memory.** The memory the fork sees, as one digest, must equal the one kept in the checkpoint.
- **Start.** An agent whose program Minutehand starts (`-- <command>`) is started once the fork exists, so whatever
  it reads and calls as it starts is the fork's (a second sample stops the first's program and starts its own); an
  agent already running is left as it is, and nothing is restarted for its memory.
- **Report.** The agent's report endpoint must answer within `ANSWER_LIMIT`, and its report must equal the one
  recorded at the checkpoint (`differences`). Its memory is the checkpoint's, so a report that differs comes from
  state the agent keeps outside the store, which no fork puts back: the fork is refused, saying so.

An agent with no report endpoint (`Command`, `Polled`, `Marked`, by message only) cannot be asked between wakes: the
fork is made with its memory proven and its report unverified, and says why.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import Field

from minutehand.application.checkpoint import Remembered
from minutehand.application.refusals import AgentFailed, RunRefused
from minutehand.domain.agent import ANSWER_LIMIT, AgentReport
from minutehand.domain.scenario import Model
from minutehand.ports.agent import Reports

OUTPUT_KEPT = 20_000
"""Characters of what a step found kept in the run's restore record; the end, where an error is."""

ANSWER_EVERY = 0.2
"""Seconds between two asks for the report while the agent is not answering yet."""

OUTSIDE = (
    "Its memory is the checkpoint's, so what differs comes from state the agent keeps outside "
    "`minutehand_agent.store` (a database of its own, a file, a cache, or a process holding another moment), which "
    "no fork puts back. Keep that state in the store, or rerun from the beginning."
)


class OwnProgram(Protocol):
    """The agent's program, when Minutehand starts it (`-- <command>`): started once the fork exists."""

    @property
    def command(self) -> Sequence[str]: ...

    async def start(self) -> None:
        """Start it and wait until it accepts connections; raises `RunRefused` with what it printed if it does not."""
        ...

    async def stop(self) -> None:
        """Stop it, if it runs: a fork's second sample starts its own."""
        ...


def differences(recorded: AgentReport, now: AgentReport) -> list[str]:
    """Each way the report after a fork differs from the one recorded at the checkpoint, one line per field.

    Equal means: the same `status`; a `next_wake` that is the same instant, or both none; and the same
    commitments as a set keyed by `Commitment.key`, each with the same `status`, where none reported and an
    empty list are the same. A commitment's description, dates and person are not compared, and two
    commitments sharing a key are one.

    Blind to everything the report does not carry: state outside the store that the report does not reflect.
    """
    found: list[str] = []
    if recorded.status is not now.status:
        found.append(f"status: {recorded.status.value} at the checkpoint, {now.status.value} after the fork")
    if recorded.next_wake != now.next_wake:
        found.append(f"next_wake: {_instant(recorded.next_wake)} at the checkpoint, {_instant(now.next_wake)} after")
    was = {c.key: c.status for c in recorded.commitments or []}
    is_ = {c.key: c.status for c in now.commitments or []}
    for key in sorted(was.keys() | is_.keys()):
        if key not in is_:
            found.append(f"commitment {key}: {was[key].value} at the checkpoint, missing after the fork")
        elif key not in was:
            found.append(f"commitment {key}: absent at the checkpoint, {is_[key].value} after the fork")
        elif was[key] is not is_[key]:
            found.append(f"commitment {key}: {was[key].value} at the checkpoint, {is_[key].value} after")
    return found


def _instant(at: datetime | None) -> str:
    return "none" if at is None else at.isoformat()


class RestoreStep(StrEnum):
    MEMORY = "memory"
    START = "start"
    ANSWER = "answer"
    VERIFY = "verify"


class StepResult(Model):
    step: RestoreStep
    command: list[str] = Field(default=[], description="The agent's program, for `start`; empty otherwise")
    output: str = Field(default="", description="What Minutehand found, or what the program printed")
    seconds: float = Field(ge=0)


class Verification(StrEnum):
    """What a fork's start was compared against the checkpoint by."""

    MEMORY = "memory"  # the memory the fork sees, as a digest
    REPORT = "report"  # the agent's report: status, next wake, commitments


class Restored(Model):
    """How a fork's agent was found at its start, kept with the fork (`restore.json`)."""

    checkpoint_seq: int
    memory: str = Field(description="The digest of the memory the fork started from")
    steps: list[StepResult]
    verified: bool = Field(description="Its report was compared with the checkpoint's and equal")
    verified_by: list[Verification]
    unverified: str | None = Field(default=None, description="Why the report could not be compared")
    outside: list[str] = Field(
        default=[], description="State outside the memory the run saw at the checkpoint, which the fork did not get"
    )
    replanned: AgentReport | None = Field(
        default=None,
        description="For a fork that changed the agent's memory: the report it gave once its memory was changed, whose "
        "next wake replaced the wakes it had planned",
    )


class RestoreFailed(RunRefused):
    """The fork's agent is not the agent at the checkpoint."""

    def __init__(self, message: str, steps: list[StepResult]) -> None:
        super().__init__(message)
        self.steps = steps


Progress = Callable[[str], None]


async def start_fork(
    state: Remembered,
    *,
    memory: str,
    checkpoint_seq: int,
    reports: Reports | None,
    own: OwnProgram | None = None,
    progress: Progress | None = None,
    edit: Callable[[], None] | None = None,
) -> Restored:
    """Prove the fork's agent is the agent at the checkpoint at `checkpoint_seq`: the fork's `memory` digest against
    the checkpoint's, then, once its program is started, its report against the one recorded there.

    With `edit`, the fork changes the agent's memory once it is proven the checkpoint's (`MemoryEdit`): the
    agent's report is then not compared but asked again, since its plan was made from the memory before the change,
    and kept as `Restored.replanned`. Raises `RestoreFailed` naming what differs, or when an agent whose memory a
    fork changes cannot be asked for its report."""
    say = progress or (lambda _: None)
    steps: list[StepResult] = []
    if memory != state.memory:
        steps.append(StepResult(step=RestoreStep.MEMORY, output=f"{state.memory} != {memory}", seconds=0.0))
        raise RestoreFailed(
            f"the fork's memory is not the agent's memory at the checkpoint at seq {checkpoint_seq}: its digest is "
            f"{memory}, and {state.memory} was kept there. The log the fork shares with its parent was changed "
            "after the checkpoint was written",
            steps,
        )
    steps.append(StepResult(step=RestoreStep.MEMORY, output=memory, seconds=0.0))
    say(f"{RestoreStep.MEMORY.value}: the fork's memory is the checkpoint's ({memory[:12]})")
    if edit is not None:
        if reports is None:
            raise RestoreFailed(
                "the fork changes the agent's memory, and the agent has no report endpoint to ask what it now plans "
                "(only a `reported` wake source has one): its wakes planned from the old memory would fire as they "
                "were",
                steps,
            )
        edit()
        say(f"{RestoreStep.MEMORY.value}: changed as the fork says")
    if own is not None:
        say(f"{RestoreStep.START.value}: starting the agent's command, {shlex.join(own.command)}")
        began = time.monotonic()
        try:
            await own.stop()
            await own.start()
        except RunRefused as e:
            steps.append(
                StepResult(
                    step=RestoreStep.START,
                    command=list(own.command),
                    output=str(e)[-OUTPUT_KEPT:],
                    seconds=time.monotonic() - began,
                )
            )
            raise RestoreFailed(f"the fork's agent did not start: {e}", steps) from e
        steps.append(StepResult(step=RestoreStep.START, command=list(own.command), seconds=time.monotonic() - began))

    def unverified(reason: str) -> Restored:
        say(f"{RestoreStep.VERIFY.value}: not verified: {reason}")
        return Restored(
            checkpoint_seq=checkpoint_seq,
            memory=memory,
            steps=steps,
            verified=False,
            verified_by=[Verification.MEMORY],
            unverified=reason,
            outside=state.outside,
        )

    if reports is None:
        return unverified(
            "the agent has no report endpoint to ask between wakes (only a `reported` wake source has one), so its "
            "report after the fork was not compared with the checkpoint's"
        )
    say(f"{RestoreStep.ANSWER.value}: waiting for the agent's report endpoint")
    report = await _answer(ANSWER_LIMIT.total_seconds(), reports, checkpoint_seq, steps)
    if edit is not None:
        say(f"{RestoreStep.ANSWER.value}: its plan from the changed memory replaces the one at the checkpoint")
        return Restored(
            checkpoint_seq=checkpoint_seq,
            memory=memory,
            steps=steps,
            verified=False,
            verified_by=[Verification.MEMORY],
            unverified="the fork changed the agent's memory, so its report was asked again and its planned wakes "
            "replaced by it, not compared with the checkpoint's",
            outside=state.outside,
            replanned=report,
        )
    if state.report is None:
        return unverified("no report was recorded at the checkpoint to compare with")
    found = differences(state.report, report)
    steps.append(StepResult(step=RestoreStep.VERIFY, output="\n".join(found), seconds=0.0))
    if found:
        seen = f" At the checkpoint the run saw: {'; '.join(state.outside)}." if state.outside else ""
        raise RestoreFailed(
            f"the agent after the fork is not the agent at the checkpoint at seq {checkpoint_seq}: its report differs "
            "field by field:\n"
            + "\n".join(f"  {line}" for line in found)
            + f"\n{OUTSIDE}{seen} The fork was refused rather than run against an agent from another moment.",
            steps,
        )
    say(f"{RestoreStep.VERIFY.value}: the report equals the one at the checkpoint")
    return Restored(
        checkpoint_seq=checkpoint_seq,
        memory=memory,
        steps=steps,
        verified=True,
        verified_by=[Verification.MEMORY, Verification.REPORT],
        outside=state.outside,
    )


async def _answer(limit: float, reports: Reports, checkpoint_seq: int, steps: list[StepResult]) -> AgentReport:
    began = time.monotonic()
    give_up = began + limit
    while True:
        try:
            report = await reports.report()
        except AgentFailed as e:
            if time.monotonic() + ANSWER_EVERY > give_up:
                steps.append(StepResult(step=RestoreStep.ANSWER, output=str(e), seconds=time.monotonic() - began))
                raise RestoreFailed(
                    f"the fork from the checkpoint at seq {checkpoint_seq} was refused: the agent's report endpoint "
                    f"did not answer within {limit:.0f} s. The last attempt: {e}",
                    steps,
                ) from e
            await asyncio.sleep(ANSWER_EVERY)
            continue
        steps.append(
            StepResult(step=RestoreStep.ANSWER, output=report.model_dump_json(), seconds=time.monotonic() - began)
        )
        return report
