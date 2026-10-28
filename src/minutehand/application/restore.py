"""The agent at the start of a fork: its memory checked, its sandbox started, and the agent brought to the checkpoint,
from its own state saved there or by its recorded inputs played into it.

A fork asks nothing of the agent. Its memory is the run's log up to the checkpoint (`application.memory`), which the
fork shares with its parent, so nothing is copied for it. Everything else the agent is (its processes, its timers,
whatever it holds in its own memory and files) lives in its sandbox, which a fork brings back one of two ways.

When the agent file names `sandbox.export_state` and it saved the agent's own state at the checkpoint
(`application.agent_state`), the agent's program is started from that state: told where it is
(`MINUTEHAND_AGENT_STATE`), its sandbox born at the checkpoint's moment, and waited for until it is idle. Nothing of
the record is played into it, so the program may be another than the one that made the checkpoint (a fix to the
agent's code): the difference is kept with the fork, and refuses nothing. What the agent imports is its own word, so
the fork is kept as unverified, saying so.

Otherwise the sandbox is brought back the way the world is: from the record. The run's record holds every input the
agent ever got, since every one came through Minutehand: the clock, the answer to every call it made (the fakes',
the model's, a captured host's), its memory read back, and every event, delivery and reply the world sent it. Playing
those inputs into a fresh sandbox, in order, up to the checkpoint, is the agent at the checkpoint
(`application.playback`). What is proven before the fork plays on:

- **Memory.** The memory the fork sees, as one digest, must equal the one kept in the checkpoint, either way.
- **Start.** The agent's program (`-- <command>`, the sandbox and the agent inside it) is started once the fork
  exists, so whatever it reads and calls as it starts is the fork's. A fork needs Minutehand to start it: a sandbox
  already running holds a moment of its own that nothing can prove is the checkpoint's.
- **Playback.** The agent's recorded inputs are played into the sandbox up to the checkpoint. The agent must make
  the same calls it made before, in the same order, with the same bodies: a call the record does not hold next is
  where the agent did not do what it did before (a changed program, or something it draws from outside the record:
  a random number, a thread's timing), and the fork is refused there, naming the call.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field

from minutehand.application.checkpoint import Remembered
from minutehand.application.refusals import RunRefused
from minutehand.domain.scenario import Model

OUTPUT_KEPT = 20_000
"""Characters of what a step found kept in the run's restore record; the end, where an error is."""

CODE_CHANGED = (
    "The agent's program is not the one that made the checkpoint ({changes}), so the likelier cause is the new "
    "code: it calls something else, or in another order. A fork continues the agent that made the checkpoint; for "
    "the changed one, play the run again from its start (`minutehand replay`), which goes live where the two part."
)

OUTSIDE_RECORD = (
    "Its program is the one that made the checkpoint, so what differs comes from something the agent draws from "
    "outside the record (a random number, a thread's timing, a clock it reads around the sandbox's), which no fork "
    "can play back. Make the agent deterministic in it, or rerun from the beginning."
)


class OwnProgram(Protocol):
    """The agent's program, when Minutehand starts it (`-- <command>`): started once the fork exists."""

    @property
    def command(self) -> Sequence[str]: ...

    async def start(self, environment: Mapping[str, str] | None = None) -> None:
        """Start it, with `environment` added to its own, and wait until it accepts connections; raises `RunRefused`
        with what it printed if it does not."""
        ...

    async def stop(self) -> None:
        """Stop it, if it runs: a fork's second sample starts its own."""
        ...


class ImportsState(Protocol):
    """The agent's own state saved at the checkpoint, which its program is started from in place of a playback
    (`application.agent_state.StateImport`)."""

    @property
    def directory(self) -> Path: ...

    async def start(self, own: OwnProgram) -> None:
        """Start `own` from the state and wait until its sandbox is idle; raises `RunRefused` when it does not."""
        ...


class Played(Model):
    """What a playback put into the sandbox."""

    calls_answered: int = Field(ge=0, description="The agent's calls answered from the record")
    deliveries_resent: int = Field(ge=0, description="What the world had sent the agent, sent again")
    jumps: int = Field(ge=0, description="Moves of the sandbox's clock")


class PlaybackParted(RunRefused):
    """The agent did not do what it did before: a call the record does not hold next."""

    def __init__(self, message: str, *, sent: str, held: str | None) -> None:
        super().__init__(message)
        self.sent = sent
        self.held = held


class Playback(Protocol):
    """The agent's recorded inputs up to a checkpoint, played into its fresh sandbox (`application.playback`)."""

    async def play(self, progress: Callable[[str], None], *, start: Callable[[], Awaitable[None]]) -> Played:
        """`start` starts the agent's program, and is called once the record answers: whatever the agent does on
        starting is its first wake's, answered from the record like the rest. Raises `PlaybackParted` where the
        agent's calls part from the record, `RunRefused` for anything the record cannot play back."""
        ...


class RestoreStep(StrEnum):
    MEMORY = "memory"
    START = "start"
    PLAYBACK = "playback"
    STATE = "state"
    EDIT = "edit"


class StepResult(Model):
    step: RestoreStep
    command: list[str] = Field(default=[], description="The agent's program, for `start`; empty otherwise")
    output: str = Field(default="", description="What Minutehand found, or what the program printed")
    seconds: float = Field(ge=0)


class Verification(StrEnum):
    """What a fork's start was proven by."""

    MEMORY = "memory"  # the memory the fork sees, as a digest
    PLAYBACK = "playback"  # the agent's recorded inputs played into its sandbox, every call matching the record


class Restored(Model):
    """How a fork's agent was brought back at its start, kept with the fork (`restore.json`)."""

    checkpoint_seq: int
    memory: str = Field(description="The digest of the memory the fork started from")
    steps: list[StepResult]
    verified: bool = Field(description="Its memory is the checkpoint's and its sandbox was played back to it")
    verified_by: list[Verification]
    unverified: str | None = Field(default=None, description="Why it could not be verified")
    outside: list[str] = Field(
        default=[], description="State outside the memory the run saw at the checkpoint, which the fork did not get"
    )
    replayed_calls: int = Field(default=0, ge=0, description="The agent's calls answered from the record")
    deliveries_resent: int = Field(default=0, ge=0, description="What the world had sent it, sent again")
    agent_state: str | None = Field(
        default=None,
        description="The folder of the agent's own state, saved at the checkpoint, that its program was started "
        "from in place of a playback; None when it was played back",
    )
    program_changes: list[str] = Field(
        default=[], description="How the agent's program differed from the one that made the checkpoint, in words"
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
    playback: Playback | None,
    own: OwnProgram | None,
    imported: ImportsState | None = None,
    progress: Progress | None = None,
    edit: Callable[[], None] | None = None,
    code_changed: Sequence[str] = (),
) -> Restored:
    """Bring the fork's agent to the checkpoint at `checkpoint_seq`: the fork's `memory` digest against the
    checkpoint's, then its program started from its own state saved there (`imported`), or, with none, started and
    its recorded inputs played into the sandbox (`playback`). `code_changed` says, in words, how the agent's program
    differs from the one that made the checkpoint (`program_changes`): kept with a fork started from saved state, and
    named as the likely cause when a playback parts.

    With `edit`, the fork changes the agent's memory once its sandbox stands at the checkpoint (`MemoryEdit`): the
    agent finds the change on its next read, and is asked nothing. Raises `RestoreFailed` naming what differs."""
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
    if own is None:
        raise RestoreFailed(
            "a fork starts the agent's sandbox itself, from its saved state or playing its recorded inputs into it, so "
            "it needs the agent's command (`-- <command>`): a sandbox already running holds a moment of its own that "
            "nothing can prove is the checkpoint's",
            steps,
        )
    await own.stop()
    steps.append(StepResult(step=RestoreStep.START, command=list(own.command), seconds=0.0))
    if imported is not None:
        return await _from_state(
            state,
            imported,
            own,
            memory=memory,
            checkpoint_seq=checkpoint_seq,
            steps=steps,
            say=say,
            edit=edit,
            code_changed=code_changed,
        )
    if playback is None:
        raise RestoreFailed(
            f"the checkpoint at seq {checkpoint_seq} holds no saved state of the agent's, and the fork was given no "
            "playback of its recorded inputs: nothing would bring the agent back to it",
            steps,
        )
    say(
        f"{RestoreStep.PLAYBACK.value}: starting the agent's command, {shlex.join(own.command)}, and playing its "
        "recorded inputs into its sandbox"
    )
    began = time.monotonic()
    try:
        played = await playback.play(say, start=own.start)
    except PlaybackParted as e:
        steps.append(
            StepResult(step=RestoreStep.PLAYBACK, output=str(e)[-OUTPUT_KEPT:], seconds=time.monotonic() - began)
        )
        cause = CODE_CHANGED.format(changes="; ".join(code_changed)) if code_changed else OUTSIDE_RECORD
        held = f" The record held next: {e.held}." if e.held is not None else " The record held nothing more."
        raise RestoreFailed(
            f"the fork's agent did not do what it did before the checkpoint at seq {checkpoint_seq}: it sent "
            f"{e.sent}, which the record does not hold there.{held} {cause} The fork was refused rather than run "
            "against an agent from another moment.",
            steps,
        ) from e
    except RunRefused as e:
        steps.append(
            StepResult(step=RestoreStep.PLAYBACK, output=str(e)[-OUTPUT_KEPT:], seconds=time.monotonic() - began)
        )
        raise RestoreFailed(f"the fork's agent could not be played back to the checkpoint: {e}", steps) from e
    steps.append(
        StepResult(step=RestoreStep.PLAYBACK, output=played.model_dump_json(), seconds=time.monotonic() - began)
    )
    say(
        f"{RestoreStep.PLAYBACK.value}: at the checkpoint, {played.calls_answered} call(s) answered from the record "
        f"and {played.deliveries_resent} delivery(ies) sent again"
    )
    if edit is not None:
        edit()
        steps.append(StepResult(step=RestoreStep.EDIT, output="changed as the fork says", seconds=0.0))
        say(f"{RestoreStep.EDIT.value}: the agent's memory changed as the fork says")
    return Restored(
        checkpoint_seq=checkpoint_seq,
        memory=memory,
        steps=steps,
        verified=True,
        verified_by=[Verification.MEMORY, Verification.PLAYBACK],
        outside=state.outside,
        replayed_calls=played.calls_answered,
        deliveries_resent=played.deliveries_resent,
        program_changes=list(code_changed),
    )


async def _from_state(
    state: Remembered,
    imported: ImportsState,
    own: OwnProgram,
    *,
    memory: str,
    checkpoint_seq: int,
    steps: list[StepResult],
    say: Progress,
    edit: Callable[[], None] | None,
    code_changed: Sequence[str],
) -> Restored:
    """`start_fork` from the agent's own state saved at the checkpoint: its program started from it, and nothing
    played back. Unverified, saying why: the agent imported what it saved, which nothing compares with its parent."""
    say(
        f"{RestoreStep.STATE.value}: starting the agent's command, {shlex.join(own.command)}, from its own state saved "
        f"at the checkpoint ({imported.directory})"
    )
    began = time.monotonic()
    try:
        await imported.start(own)
    except RunRefused as e:
        steps.append(StepResult(step=RestoreStep.STATE, output=str(e)[-OUTPUT_KEPT:], seconds=time.monotonic() - began))
        raise RestoreFailed(
            f"the fork's agent could not be started from its own state saved at the checkpoint at seq "
            f"{checkpoint_seq}: {e}",
            steps,
        ) from e
    steps.append(StepResult(step=RestoreStep.STATE, output=str(imported.directory), seconds=time.monotonic() - began))
    changed = (
        f"; its program is not the one that made the checkpoint ({'; '.join(code_changed)})" if code_changed else ""
    )
    say(f"{RestoreStep.STATE.value}: the agent started from its saved state and is idle{changed}")
    if edit is not None:
        edit()
        steps.append(StepResult(step=RestoreStep.EDIT, output="changed as the fork says", seconds=0.0))
        say(f"{RestoreStep.EDIT.value}: the agent's memory changed as the fork says")
    return Restored(
        checkpoint_seq=checkpoint_seq,
        memory=memory,
        steps=steps,
        verified=False,
        verified_by=[Verification.MEMORY],
        unverified="its sandbox was not played back: the agent was started from its own state saved at the "
        f"checkpoint ({imported.directory}), which nothing compares with the agent that saved it",
        outside=state.outside,
        agent_state=str(imported.directory),
        program_changes=list(code_changed),
    )
