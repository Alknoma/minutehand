"""What a finished run is, for the record."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

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


class VerdictKind(StrEnum):
    PASSED = "passed"  # no check failed, and the agent finished: it reported done, or nothing was left open
    UNFINISHED = "unfinished"  # no check failed, but the agent never reported done and work was still open
    FAILED = "failed"  # a check failed


EXIT_CODES = {VerdictKind.PASSED: 0, VerdictKind.FAILED: 1, VerdictKind.UNFINISHED: 3}
"""What `minutehand run`, `fork` and `findings` exit with for each verdict. 2 is a run that could not be
performed, which has no verdict."""


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
    providers: list[ProviderKey] = Field(
        default=[], description="Every provider the agent called, in the order of its first call"
    )
    outbound: list[OutboundUse] = Field(
        default=[], description="Every host no provider claims that the agent called, in the order of its first call"
    )
    wakes: list[WakeRecord]
