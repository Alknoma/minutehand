"""Steps in a standing world: where one go of the agent ends and the next begins, when the agent is driven from
outside and Minutehand owns no loop.

The run loop knows its wakes because it sends them. A standing world does not: a harness drives the agent and
moves the clock itself. So whoever drives says where a step begins and ends (`Stepping.begin`, `.end`), and each
is recorded exactly as the run loop records a wake's edges: the clock's wake moves on, so every event, call and
span written meanwhile carries the step's number; the store keeps the real moment each began and ended
(`Store.wake_began`, `wake_ended`), which places the services' spans; and a `StepMark` in the log keeps the
step's simulated moment, why it began, and whether it was marked or inferred, which is what the checks, the
scorecard and the viewer read back as `WakeRecord`s (`steps`).

Nobody marks a step: a step is inferred each time the clock is moved forward (`Stepping.moved`), so a harness that
only sets the clock gets steps without changing anything. Conservatively: the stretch from the world's opening to
the first forward move is the first step, and each forward move begins the next, at the moment it moves to; a
move to the moment the clock already shows begins nothing; a world whose clock never moves forward has no step.
Each is marked `inferred`. Once a step is marked by hand, inference stops, and the steps inferred before it are
not counted: what the harness says replaces what the server guessed.

Steps begun on a case are begun in every world of the case at once, numbered alike, so the case's merged record
reads them as one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, Field

from minutehand.application.refusals import RunRefused
from minutehand.application.run_clock import RunClock
from minutehand.domain.checks import WakeRecord
from minutehand.domain.scenario import Model
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation
from minutehand.ports.store import Store

STEP = EntityRef(provider="minutehand", kind=EntityKind.RECORD, external_id="step")
"""The entity a step's edges are written to. Actor SCENARIO, so no check counts it as the agent's work."""

NOT_CHANGES = frozenset({Operation.READ, Operation.SEARCH})


class StepEdge(StrEnum):
    BEGAN = "began"
    ENDED = "ended"


class StepMark(Model):
    """One edge of a step, as the log keeps it."""

    wake: int = Field(ge=1, description="The step's number: the wake every event in it carries")
    edge: StepEdge
    at: AwareDatetime = Field(description="Simulated time: when the step began, or the clock when it ended")
    reason: str | None = None
    inferred: bool = False


class StepRefused(RunRefused):
    """A step cannot be marked so: nothing was written."""


def write_mark(store: Store, mark: StepMark) -> int:
    operation = Operation.CREATE if store.get(STEP) is None else Operation.UPDATE
    event = store.apply(Change(entity=STEP, operation=operation, actor=Actor.SCENARIO, body=mark.model_dump_json()))
    return event.seq


def marks(store: Store) -> list[StepMark]:
    return [StepMark.model_validate_json(v.body) for v in store.versions(STEP)]


def steps(store: Store) -> list[WakeRecord]:
    """Every step the log holds, as the wakes the checks read: a marked one by its mark; an inferred one only when
    nothing was marked by hand. Each counts the agent's changes the events of its number carry."""
    found = marks(store)
    by_hand = any(not m.inferred for m in found)
    began: dict[int, StepMark] = {}
    for mark in found:
        if mark.edge is StepEdge.BEGAN and mark.wake not in began and not (by_hand and mark.inferred):
            began[mark.wake] = mark
    changes: dict[int, int] = {}
    for event in store.events():
        if event.actor is Actor.AGENT and event.operation not in NOT_CHANGES:
            changes[event.wake] = changes.get(event.wake, 0) + 1
    return [
        WakeRecord(
            index=wake,
            sim_time=mark.at,
            world_changes=changes.get(wake, 0),
            commitments_changed=False,
            inferred=mark.inferred,
            reason=mark.reason,
        )
        for wake, mark in sorted(began.items())
    ]


class Stepped(Protocol):
    """A world whose wake a `Stepping` moves: `StandingWorld`."""

    def enter_wake(self, wake: int) -> None: ...

    def end_wake(self) -> None: ...


class Stepping:
    """The steps of one run: a lone standing world's, or a case's across its worlds. `marks` is where the marks
    are written (the world's own store, or the case's); `own` is the clock of that store when it is not a world's
    (a case's), moved with the steps; `members` are the worlds each step is begun and ended in."""

    def __init__(
        self,
        marks: Store,
        *,
        start: datetime,
        members: Callable[[], Sequence[Stepped]],
        own: RunClock | None = None,
    ) -> None:
        self._marks = marks
        self._members = members
        self._own = own
        self.start = start
        self.wake = 1
        self.at = start
        self.begun = False
        self.open = False
        self.by_hand = False

    def begin(self, at: datetime | None, reason: str | None, *, inferred: bool = False) -> int:
        """Begin a step at `at` (simulated; the latest moment the run has reached when None), ending the one in
        progress. Answers its number."""
        when = at if at is not None else self.at
        if when < self.at:
            raise StepRefused(
                f"a step begins no earlier than the last: {when.isoformat()} is before {self.at.isoformat()}"
            )
        if not inferred and not self.by_hand:
            self.by_hand = True
        if self.open:
            self.end()
        if inferred and not self.begun:
            # The stretch from the opening to this first move was the first step.
            self._write(StepMark(wake=self.wake, edge=StepEdge.BEGAN, at=self.start, inferred=True))
            self.begun = True
            self._write(StepMark(wake=self.wake, edge=StepEdge.ENDED, at=when, inferred=True))
        if self.begun:
            self.wake += 1
            for world in self._members():
                world.enter_wake(self.wake)
            if self._own is not None:
                self._own.enter(self.wake)
                self._marks.wake_began(self.wake)
        self.begun = True
        self.open = True
        self.at = when
        if self._own is not None and when > self._own.now():
            self._own.jump(when)
        self._write(StepMark(wake=self.wake, edge=StepEdge.BEGAN, at=when, reason=reason, inferred=inferred))
        return self.wake

    def end(self) -> int:
        """End the step in progress; refused when none is."""
        if not self.open:
            raise StepRefused("no step is in progress: begin one first")
        for world in self._members():
            world.end_wake()
        if self._own is not None:
            self._marks.wake_ended(self.wake)
        self.open = False
        self._write(StepMark(wake=self.wake, edge=StepEdge.ENDED, at=self.at, inferred=not self.by_hand))
        return self.wake

    def moved(self, to: datetime) -> int | None:
        """A world's clock is about to move to `to`: a step is inferred when it moves forward past every moment the
        run has reached and nothing was ever marked by hand. Answers the step begun, if one was."""
        if to <= self.at:
            return None
        if self.by_hand:
            self.at = to
            if self._own is not None and to > self._own.now():
                self._own.jump(to)
            return None
        return self.begin(to, None, inferred=True)

    def _write(self, mark: StepMark) -> None:
        write_mark(self._marks, mark)
