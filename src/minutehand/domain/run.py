"""What a finished run is, for the record."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from minutehand.domain.agent import AgentReport
from minutehand.domain.checks import WakeRecord
from minutehand.domain.scenario import Model, ProviderKey
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


EXIT_CODES = {
    VerdictKind.PASSED: 0,
    VerdictKind.FAILED: 1,
    VerdictKind.ENVIRONMENT_FAILED: 2,
    VerdictKind.UNFINISHED: 3,
    VerdictKind.TOOL_FAILED: 4,
    VerdictKind.NOT_JUDGED: 5,
}
"""What `minutehand run`, `fork` and `findings` exit with for each verdict. 2 is also a run that could not be
performed, which has no verdict: either way, the environment and not the agent. 4 is Minutehand's own failure."""


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
    worlds: list[str] = Field(
        default=[],
        description="A case (`minutehand serve`, worlds opened under one case label): the worlds it is made of, in "
        "the order they opened; empty for any other run",
    )
