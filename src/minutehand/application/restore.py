"""The agent's own state, put back: settled before every checkpoint, restored as a sequence, and verified.

Minutehand owns the procedure; the agent supplies the commands (`StateHooks`).

- **Settle.** A checkpoint is snapshotted only when the agent says it is not working AND no outbound call of its
  has been seen for `StateHooks.quiet`, measured from whichever is later: the moment settling began or its last
  call. One that has not settled after `StateHooks.settle_limit` is recorded as `NotRestorable`, with the reason.
- **Restore.** `stop` (the agent's program, when Minutehand started it, then the agent's own `stop`), `restore`,
  `start` (the agent's own `start`, then its program), then the report endpoint must answer within
  `StateHooks.answer_limit`. A failing step stops the fork, naming the step and showing its output.
- **Verify.** The report after the restore must equal the report recorded at the checkpoint (`differences`).

What the report cannot show, the comparison cannot see: see `differences`.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field

from minutehand.application.checkpoint import NotRestorable
from minutehand.application.refusals import AgentFailed, RunRefused
from minutehand.application.state_hooks import SNAPSHOT_DIR_ENV
from minutehand.domain.agent import AgentReport, AgentStatus, StateHooks
from minutehand.domain.scenario import Model
from minutehand.ports.agent import Reports

OUTPUT_KEPT = 20_000
"""Characters of a command's output kept in the run's restore record; the end, where an error is."""

OUTPUT_SHOWN = 3_000
"""Characters of a failing command's output shown in the refusal."""

ANSWER_EVERY = 0.2
"""Seconds between two asks for the report while the restarted agent is not answering yet."""

WORKING_EVERY = 0.1
"""Seconds between two asks for the report while the agent still says it is working."""


@dataclass(frozen=True)
class SeenCall:
    """The latest outbound call of the agent's that something saw: when, as `time.monotonic()` in this process,
    and what, for a person (`POST slack.com/api/chat.postMessage`)."""

    at: float
    what: str


class Traffic(Protocol):
    """Whoever sees the agent's outbound calls (the proxy): asked how long the agent has been quiet."""

    def last_call(self) -> SeenCall | None:
        """The latest call seen in this process, or None before the first."""
        ...


class OwnProgram(Protocol):
    """The agent's program, when Minutehand started it (`-- <command>`): stopped and started around a restore, so
    nothing it holds in memory survives the restore."""

    @property
    def command(self) -> Sequence[str]: ...

    async def stop(self) -> None: ...

    async def start(self) -> None:
        """Start it and wait until it accepts connections; raises `RunRefused` with what it printed if it does not."""
        ...


# -- settle ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Settled:
    report: AgentReport | None


async def settle(
    hooks: StateHooks, traffic: Traffic, reports: Reports | None, known: AgentReport | None
) -> Settled | NotRestorable:
    """Wait until the agent is settled: not working, and quiet for `hooks.quiet`. `known` is its last report,
    taken as its report when it cannot be asked (`reports` is None)."""
    quiet = hooks.quiet.total_seconds()
    limit = hooks.settle_limit.total_seconds()
    began = time.monotonic()
    give_up = began + limit
    calm_from = began
    working = False
    while True:
        last = traffic.last_call()
        if last is not None and last.at > calm_from:
            calm_from = last.at
        ready = calm_from + quiet
        now = time.monotonic()
        if now < ready:
            if ready > give_up:
                return NotRestorable(reason=_unsettled(hooks, last, working, give_up))
            await asyncio.sleep(ready - now)
            continue
        if reports is None:
            return Settled(report=known)
        try:
            report = await reports.report()
        except AgentFailed as e:
            return NotRestorable(reason=f"the agent's report endpoint did not answer while it settled: {e}")
        after = traffic.last_call()
        if after is not None and after.at > calm_from:
            continue  # it called out while it was asked: the quiet starts again from that call
        if report.status is not AgentStatus.WORKING:
            return Settled(report=report)
        working = True
        if time.monotonic() + max(quiet, WORKING_EVERY) > give_up:
            return NotRestorable(reason=_unsettled(hooks, last, working, give_up))
        await asyncio.sleep(WORKING_EVERY)
        calm_from = time.monotonic()


def _unsettled(hooks: StateHooks, last: SeenCall | None, working: bool, give_up: float) -> str:
    limit = _span(hooks.settle_limit.total_seconds())
    if working:
        return (
            f"the agent still reported WORKING when the settle limit ({limit}) ran out; its state was not "
            "snapshotted, because a snapshot of an agent at work is of no moment a fork can resume"
        )
    assert last is not None
    return (
        f"the agent was still making outbound calls when the settle limit ({limit}) ran out: its last, "
        f"{last.what}, came {_span(max(give_up - last.at, 0.0))} before it, and a checkpoint needs "
        f"{_span(hooks.quiet.total_seconds())} with none; work it does in the background outlives its report"
    )


# -- verify ---------------------------------------------------------------------------------------------------


def differences(recorded: AgentReport, now: AgentReport) -> list[str]:
    """Each way the report after a restore differs from the one recorded at the checkpoint, one line per field.

    Equal means: the same `status`; a `next_wake` that is the same instant, or both none; and the same
    commitments as a set keyed by `Commitment.key`, each with the same `status`, where none reported and an
    empty list are the same. A commitment's description, dates and person are not compared, and two
    commitments sharing a key are one.

    Blind to everything the report does not carry: what the agent remembers but does not report (a
    conversation, a cache, a half-written draft), state another service keeps for it, and a restore that
    put back a different moment whose report happens to read the same.
    """
    found: list[str] = []
    if recorded.status is not now.status:
        found.append(f"status: {recorded.status.value} at the checkpoint, {now.status.value} after the restore")
    if recorded.next_wake != now.next_wake:
        found.append(f"next_wake: {_instant(recorded.next_wake)} at the checkpoint, {_instant(now.next_wake)} after")
    was = {c.key: c.status for c in recorded.commitments or []}
    is_ = {c.key: c.status for c in now.commitments or []}
    for key in sorted(was.keys() | is_.keys()):
        if key not in is_:
            found.append(f"commitment {key}: {was[key].value} at the checkpoint, missing after the restore")
        elif key not in was:
            found.append(f"commitment {key}: absent at the checkpoint, {is_[key].value} after the restore")
        elif was[key] is not is_[key]:
            found.append(f"commitment {key}: {was[key].value} at the checkpoint, {is_[key].value} after")
    return found


def _instant(at: datetime | None) -> str:
    return "none" if at is None else at.isoformat()


# -- restore --------------------------------------------------------------------------------------------------


class RestoreStep(StrEnum):
    STOP = "stop"
    RESTORE = "restore"
    START = "start"
    ANSWER = "answer"
    VERIFY = "verify"


class StepResult(Model):
    step: RestoreStep
    command: list[str] = Field(default=[], description="Empty for a step Minutehand takes itself")
    exit_code: int | None = Field(default=None, description="None for a step that ran no command")
    output: str = Field(default="", description="What the command printed, or what Minutehand found")
    seconds: float = Field(ge=0)


class Restored(Model):
    """One restore of the agent, kept with the run it started (`restore.json`)."""

    checkpoint_seq: int
    snapshot: str = Field(description="The snapshot directory restored from")
    steps: list[StepResult]
    verified: bool
    unverified: str | None = Field(default=None, description="Why the restore could not be verified")


class RestoreFailed(RunRefused):
    """A step of a restore failed, or the restored agent is not the agent at the checkpoint."""

    def __init__(self, message: str, steps: list[StepResult]) -> None:
        super().__init__(message)
        self.steps = steps


Progress = Callable[[str], None]


async def restore_agent(
    hooks: StateHooks,
    snapshot: Path,
    *,
    checkpoint_seq: int,
    recorded: AgentReport | None,
    reports: Reports | None,
    own: OwnProgram | None = None,
    progress: Progress | None = None,
) -> Restored:
    """Put the agent back as it was at the checkpoint at `checkpoint_seq`, from `snapshot`, and prove it.

    Raises `RestoreFailed` naming the step that failed and showing its output, or listing field by field how
    the restored agent's report differs from `recorded`."""
    say = progress or (lambda _: None)
    steps: list[StepResult] = []

    async def command(step: RestoreStep, argv: list[str]) -> None:
        say(f"{step.value}: {shlex.join(argv)}")
        result = await run_command(step, argv, snapshot, hooks.step_limit.total_seconds())
        steps.append(result)
        if result.exit_code != 0:
            raise RestoreFailed(_failed(checkpoint_seq, result), steps)
        say(f"{step.value}: done in {_span(result.seconds)}")

    async def program(step: RestoreStep, act: Callable[[], Awaitable[None]], own: OwnProgram) -> None:
        verb = "stopping" if step is RestoreStep.STOP else "starting"
        say(f"{step.value}: {verb} the agent's command, {shlex.join(own.command)}")
        began = time.monotonic()
        try:
            await act()
        except RunRefused as e:
            result = StepResult(
                step=step, command=list(own.command), output=str(e)[-OUTPUT_KEPT:], seconds=time.monotonic() - began
            )
            steps.append(result)
            raise RestoreFailed(_failed(checkpoint_seq, result), steps) from e
        steps.append(StepResult(step=step, command=list(own.command), seconds=time.monotonic() - began))

    if own is not None:
        await program(RestoreStep.STOP, own.stop, own)
    if hooks.stop is not None:
        await command(RestoreStep.STOP, hooks.stop)
    await command(RestoreStep.RESTORE, hooks.restore)
    if hooks.start is not None:
        await command(RestoreStep.START, hooks.start)
    if own is not None:
        await program(RestoreStep.START, own.start, own)
    if reports is None:
        reason = (
            "the agent has no report endpoint to ask between wakes (only a `reported` wake source has one), so "
            "what its state holds after the restore was not compared with the checkpoint"
        )
        say(f"{RestoreStep.VERIFY.value}: not verified: {reason}")
        return Restored(
            checkpoint_seq=checkpoint_seq, snapshot=str(snapshot), steps=steps, verified=False, unverified=reason
        )
    say(f"{RestoreStep.ANSWER.value}: waiting for the agent's report endpoint")
    report = await _answer(hooks, reports, checkpoint_seq, steps)
    say(f"{RestoreStep.ANSWER.value}: answered in {_span(steps[-1].seconds)}")
    if recorded is None:
        reason = "no report was recorded at the checkpoint to compare with"
        say(f"{RestoreStep.VERIFY.value}: not verified: {reason}")
        return Restored(
            checkpoint_seq=checkpoint_seq, snapshot=str(snapshot), steps=steps, verified=False, unverified=reason
        )
    found = differences(recorded, report)
    steps.append(StepResult(step=RestoreStep.VERIFY, output="\n".join(found), seconds=0.0))
    if found:
        raise RestoreFailed(
            f"the restore did not bring back the agent as it was at the checkpoint at seq {checkpoint_seq}: "
            "its report differs field by field:\n"
            + "\n".join(f"  {line}" for line in found)
            + "\nEvery restore step exited 0, so a step that did nothing, or restored another snapshot, is the "
            "likely cause. The fork was refused rather than run against an agent from another moment.",
            steps,
        )
    say(f"{RestoreStep.VERIFY.value}: the report equals the one at the checkpoint")
    return Restored(checkpoint_seq=checkpoint_seq, snapshot=str(snapshot), steps=steps, verified=True)


async def _answer(hooks: StateHooks, reports: Reports, checkpoint_seq: int, steps: list[StepResult]) -> AgentReport:
    began = time.monotonic()
    give_up = began + hooks.answer_limit.total_seconds()
    while True:
        try:
            report = await reports.report()
        except AgentFailed as e:
            if time.monotonic() + ANSWER_EVERY > give_up:
                result = StepResult(step=RestoreStep.ANSWER, output=str(e), seconds=time.monotonic() - began)
                steps.append(result)
                raise RestoreFailed(
                    f"the restore of the agent from the checkpoint at seq {checkpoint_seq} failed at step "
                    f"`answer`: its report endpoint did not answer within the answer limit "
                    f"({_span(hooks.answer_limit.total_seconds())}). The last attempt: {e}",
                    steps,
                ) from e
            await asyncio.sleep(ANSWER_EVERY)
            continue
        steps.append(
            StepResult(step=RestoreStep.ANSWER, output=report.model_dump_json(), seconds=time.monotonic() - began)
        )
        return report


async def run_command(step: RestoreStep, argv: list[str], directory: Path, limit: float) -> StepResult:
    """Run one hook command with MINUTEHAND_SNAPSHOT_DIR set to `directory`; its output, both streams, is kept.
    A command that cannot be started, or runs past `limit` seconds, is a failed step, not an exception."""
    directory.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env={**os.environ, SNAPSHOT_DIR_ENV: str(directory)},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        return StepResult(step=step, command=argv, exit_code=127, output=str(e), seconds=time.monotonic() - began)
    try:
        out, _ = await asyncio.wait_for(process.communicate(), limit)
    except TimeoutError:
        process.kill()
        out, _ = await process.communicate()
        text = out.decode(errors="replace")
        return StepResult(
            step=step,
            command=argv,
            exit_code=process.returncode,
            output=(text + f"\n(killed after the step limit, {_span(limit)})")[-OUTPUT_KEPT:],
            seconds=time.monotonic() - began,
        )
    return StepResult(
        step=step,
        command=argv,
        exit_code=process.returncode,
        output=out.decode(errors="replace")[-OUTPUT_KEPT:],
        seconds=time.monotonic() - began,
    )


def _failed(checkpoint_seq: int, result: StepResult) -> str:
    shown = result.output.strip()[-OUTPUT_SHOWN:] or "(it printed nothing)"
    how = "failed" if result.exit_code is None else f"exited {result.exit_code}"
    return (
        f"the restore of the agent from the checkpoint at seq {checkpoint_seq} failed at step "
        f"`{result.step.value}`: {shlex.join(result.command)} {how} after {_span(result.seconds)}. "
        f"Its output:\n{shown}"
    )


def _span(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds < 120 else f"{seconds / 60:.1f} min"
