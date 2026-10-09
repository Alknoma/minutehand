"""What a finished run is, for the record."""

from __future__ import annotations

import math
from datetime import timedelta
from enum import StrEnum

from pydantic import AwareDatetime, Field

from minutehand.domain.agent import AgentReport, AgentUnderTest, Polled
from minutehand.domain.checks import WakeRecord
from minutehand.domain.scenario import Model, ProviderKey, Scenario
from minutehand.domain.world import CaptureMode


class StopReason(StrEnum):
    AGENT_DONE = "agent_done"  # the agent reported it had finished
    WAKE_LIMIT = "wake_limit"  # Scenario.max_wakes reached
    DEADLINE_PASSED = "deadline_passed"  # the clock passed the scenario's deadline
    NOTHING_PENDING = "nothing_pending"  # nothing is due and the agent named no next wake: it is stuck
    AGENT_FAILED = "agent_failed"  # the agent could not be reached or answered with an error
    CLOSED = "closed"  # a standing world (`minutehand serve`) was closed by whoever opened it
    ENVIRONMENT_FAILED = "environment_failed"  # an external emulator the run used was unavailable: not the agent


class VerdictKind(StrEnum):
    PASSED = "passed"  # no check failed, and the agent finished: it reported done, or nothing was left open
    UNFINISHED = "unfinished"  # no check failed, but the agent never reported done and work was still open
    FAILED = "failed"  # a check failed
    ENVIRONMENT_FAILED = "environment_failed"  # the run's environment failed under the agent: nothing is judged
    TOOL_FAILED = "tool_failed"  # Minutehand broke while answering a call; the agent is not scored on this run
    NOT_JUDGED = "not_judged"  # no check failed, and checks could not run or there was nothing to judge
    SIMULATION_INCOMPLETE = "simulation_incomplete"  # the simulated world did not play as declared: see `health`


EXIT_CODES = {
    VerdictKind.PASSED: 0,
    VerdictKind.FAILED: 1,
    VerdictKind.ENVIRONMENT_FAILED: 2,
    VerdictKind.UNFINISHED: 3,
    VerdictKind.TOOL_FAILED: 4,
    VerdictKind.NOT_JUDGED: 5,
    VerdictKind.SIMULATION_INCOMPLETE: 6,
}
"""What `minutehand run`, `fork` and `findings` exit with for each verdict. 2 is also a run that could not be
performed, which has no verdict: either way, the environment and not the agent. 4 is Minutehand's own failure. 6 is
a simulated world that did not play as its files declare (`checks.health`): the agent is judged on what did happen,
and that judgement is kept beside it (`Verdict.on_what_happened`)."""


class Verdict(Model):
    """Whether the run's checks held, kept apart from whether the agent finished.

    A run is finished when the agent reported it was done, or when nothing was left open: no wait the world
    had not settled and no commitment the agent's last report still held open. A run that stopped any other
    way with work open is UNFINISHED even when every check held: a wake limit, the scenario's deadline or an
    agent that stopped asking to be woken cut it off, and nobody saw the agent finish."""

    kind: VerdictKind
    stop: StopReason | None = Field(description="How the run ended; None for a run captured elsewhere, which says not")
    failed_checks: int = Field(ge=0)
    open_waits: int = Field(ge=0, description="Waits the world had not settled when the run ended")
    open_commitments: int | None = Field(
        ge=0, description="Commitments the agent's last report held open; None when it reported none at all"
    )
    words: str = Field(description="The verdict in one sentence, as every surface states it")
    on_what_happened: VerdictKind | None = Field(
        default=None,
        description="SIMULATION_INCOMPLETE: the verdict the agent's checks and its finishing come to over what did "
        "happen, as it would read with the world whole; None for any other kind",
    )
    unjudged: list[str] = Field(
        default=[],
        description="NOT_JUDGED: why, one reason each: a check that could not run and what it needed, or that "
        "nothing was there to judge",
    )

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.kind]


class OutboundUse(Model):
    """The agent's calls to one host no provider claims: captured as declared, discovered, refused, or relayed
    unopened on tunnels (a model API)."""

    host: str
    mode: CaptureMode | None = Field(
        description="None: nobody declared it, and the run refused it, or, when `tunnelled`, relayed it unopened"
    )
    declared_as: str | None = Field(default=None, description="The declaration's host pattern, when one matched")
    calls: int = Field(ge=0)
    replayed: int = Field(default=0, ge=0, description="Answered from a recording")
    refused: int = Field(default=0, ge=0, description="Answered 502: undeclared, or a replay that missed")
    methods: list[str] = Field(default=[], description="Each method it was called with, in order of first use")
    paths: list[str] = Field(default=[], description="Up to five paths it was called at, without their query")
    unknown_recipients: list[str] = Field(
        default=[], description="Addresses its sends named that match no person in the scenario"
    )
    tunnelled: int = Field(default=0, ge=0, description="Bursts on tunnels the proxy relayed and never opened")
    connections: int = Field(default=0, ge=0, description="The tunnels those bursts were on")
    bytes_sent: int = Field(default=0, ge=0, description="On those tunnels, from the agent")
    bytes_received: int = Field(default=0, ge=0, description="On those tunnels, to the agent")
    not_served: int = Field(
        default=0, ge=0, description="Calls the provider claiming the host does not serve, answered as declared"
    )
    not_served_by: list[str] = Field(default=[], description="The providers that did not serve them")


class OperationCount(Model):
    operation: str
    calls: int = Field(ge=1)


class EmulatorUse(Model):
    """What the agent's calls to one external emulator came to, by `CallOutcome`."""

    emulator: str
    calls: int = Field(ge=0)
    answered: int = Field(default=0, ge=0)
    refused: int = Field(default=0, ge=0, description="Errors it answered as the real service would")
    internal_errors: int = Field(default=0, ge=0, description="5xx answers nobody declared a faithful error")
    unavailable: int = Field(default=0, ge=0, description="Calls nothing answered: it was down or did not answer")
    not_implemented: list[OperationCount] = Field(
        default=[], description="Operations it had no answer for, each with how many calls asked"
    )
    first_unavailable: str | None = Field(default=None, description="The first call it was unavailable for")


DEFAULT_WAKES = 20
"""The wake limit of a scenario that sets none and gives no deadline to size one from; also the room left, beyond the
agent's own rhythm, for the wakes replies and happenings bring."""


class WakeLimit(Model):
    """The most wakes a run plays, and where that number came from."""

    wakes: int = Field(ge=1)
    why: str


def wake_limit(scenario: Scenario, agent: AgentUnderTest) -> WakeLimit:
    """The scenario's `max_wakes`; else, with a deadline and a rhythm the agent keeps (a polled wake's `every`, or
    the agent file's `tick`), every tick up to the deadline and `DEFAULT_WAKES` more; else `DEFAULT_WAKES`."""
    if scenario.max_wakes is not None:
        return WakeLimit(wakes=scenario.max_wakes, why="the scenario's max_wakes")
    ticks = [w.every for w in agent.wakes if isinstance(w, Polled)] + ([agent.tick] if agent.tick else [])
    if scenario.deadline_after is not None and ticks:
        tick = min(ticks)
        rhythm = math.ceil(scenario.deadline_after / tick)
        return WakeLimit(
            wakes=rhythm + DEFAULT_WAKES,
            why=f"{rhythm} wakes of the agent's {_said(tick)} rhythm before the scenario's deadline, and "
            f"{DEFAULT_WAKES} more for what else wakes it",
        )
    missing = "no deadline" if scenario.deadline_after is None else "a deadline, but the agent file declares no tick"
    return WakeLimit(
        wakes=DEFAULT_WAKES,
        why=f"the default: the scenario sets no max_wakes and has {missing} to size one from",
    )


def _said(tick: timedelta) -> str:
    minutes = round(tick.total_seconds() / 60)
    return f"{minutes // 60}-hour" if minutes % 60 == 0 else f"{minutes}-minute"


class RunRecord(Model):
    run_id: str
    scenario: str
    seed: int
    parent_run: str | None = None
    forked_at: int | None = Field(default=None, description="WorldEvent.seq shared with the parent")
    started_at: AwareDatetime = Field(description="Simulated")
    ended_at: AwareDatetime = Field(description="Simulated")
    wall_seconds: float = Field(ge=0)
    stop: StopReason
    failure: str | None = Field(default=None, description="Why, when the run stopped AGENT_FAILED")
    reported: AgentReport | None = Field(
        default=None,
        description="A standing world or a case (`minutehand serve`): the agent's own report of its work, as whoever "
        "drove it last relayed it; None when nobody relayed one",
    )
    providers: list[ProviderKey] = Field(
        default=[], description="Every provider the agent called, in the order of its first call"
    )
    outbound: list[OutboundUse] = Field(
        default=[], description="Every host no provider claims that the agent called, in the order of its first call"
    )
    emulators: list[EmulatorUse] = Field(default=[], description="Every external emulator the agent's calls reached")
    wakes: list[WakeRecord]
    wake_limit: WakeLimit | None = Field(
        default=None, description="The most wakes the run would play, and why; None for a standing world"
    )
    worlds: list[str] = Field(
        default=[],
        description="A case (`minutehand serve`, worlds opened under one case label): the worlds it is made of, in "
        "the order they opened; empty for any other run",
    )
