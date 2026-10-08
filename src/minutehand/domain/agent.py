"""The contract between the monitor and the agent it is testing.

The agent answers three questions. Everything else the monitor learns from the
traffic it intercepts.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from minutehand.domain.assessments import Rule, refuse_repeated_rules
from minutehand.domain.emulator import ExternalEmulator, refuse_unknown_emulators
from minutehand.domain.inboxes import HttpInbox, refuse_repeated_inboxes
from minutehand.domain.outbound import Forward, OutboundHost, refuse_repeats
from minutehand.domain.people import InboundTarget
from minutehand.domain.scenario import Model, ProviderKey
from minutehand.domain.world import EntityRef


class WakeReason(StrEnum):
    START = "start"
    DUE = "due"
    PERSON_REPLIED = "person_replied"
    DIRECTION = "direction"
    TICK = "tick"


class AgentStatus(StrEnum):
    WORKING = "working"
    IDLE = "idle"
    DONE = "done"


class WaitingOn(StrEnum):
    PERSON = "person"
    SYSTEM = "system"
    DATE = "date"
    RESULT = "result"


class CommitmentStatus(StrEnum):
    OPEN = "open"
    MET = "met"
    DROPPED = "dropped"


class Commitment(Model):
    """Something the agent says it is waiting for. Optional; unlocks the time checks."""

    key: str
    description: str
    waiting_on: WaitingOn
    person_email: str | None = None
    entity: EntityRef | None = None
    opened_at: AwareDatetime
    expected_by: AwareDatetime | None = None
    status: CommitmentStatus = CommitmentStatus.OPEN


class WakeRequest(Model):
    """ "It is now `now`; go." Sent each time the clock reaches a due moment."""

    run_id: str
    now: AwareDatetime
    reason: WakeReason
    goal: str | None = Field(default=None, description="Set on the START wake only")
    direction: str | None = Field(default=None, description="What the owner said; set on a DIRECTION wake only")


class AgentReport(Model):
    """The answer to "are you still working?" and "when do you next need to wake?"."""

    status: AgentStatus
    next_wake: AwareDatetime | None = None
    commitments: list[Commitment] | None = None


class Reported(Model):
    """The agent says when it next needs to wake. Exact, and needs an endpoint or adapter.

    A wake is over when `report_url` answers anything but WORKING. The defaults suit an agent whose turn takes
    minutes: the report is asked for quickly at first and then less often, the wait doubling from
    `report_first_after` up to `report_at_most_every`, and a wake still WORKING after `working_limit` stops the
    run as AGENT_FAILED.
    """

    kind: Literal["reported"] = "reported"
    wake_url: str
    report_url: str
    wake_timeout: timedelta = Field(
        default=timedelta(minutes=2),
        gt=timedelta(0),
        description="How long one call to wake_url or report_url may take",
    )
    report_first_after: timedelta = Field(
        default=timedelta(milliseconds=100), gt=timedelta(0), description="The wait before the first ask for the report"
    )
    report_at_most_every: timedelta = Field(
        default=timedelta(seconds=10), gt=timedelta(0), description="The longest wait between two asks for the report"
    )
    working_limit: timedelta = Field(
        default=timedelta(minutes=30), gt=timedelta(0), description="How long one wake may stay WORKING"
    )

    @model_validator(mode="after")
    def _backs_off(self) -> Reported:
        if self.report_at_most_every < self.report_first_after:
            raise ValueError("report_at_most_every is shorter than report_first_after")
        return self


class Marked(Model):
    """The agent is woken at `wake_url`, and a wake is over when the call returns. Its next wake is the moment it
    marked with `minutehand_agent.wake` in that wake, or none: nothing else is asked of it."""

    kind: Literal["marked"] = "marked"
    wake_url: str
    wake_timeout: timedelta = Field(
        default=timedelta(minutes=5), gt=timedelta(0), description="How long one call to wake_url may take"
    )


class Booked(Model):
    """The agent books its own wake-ups with a scheduler the proxy intercepts.

    Nothing is asked of the agent: the booking is an outbound call like any other,
    and the scheduler provider calls the agent back when the clock reaches it.
    """

    kind: Literal["booked"] = "booked"
    take_limit: timedelta = Field(
        default=timedelta(seconds=30),
        gt=timedelta(0),
        description="How long, in real time, the run waits for the agent to take a booking's delivery from its own "
        "queue before it moves on, when the scheduler can tell (`ConfirmsDelivery`)",
    )


class Polled(Model):
    """The agent is invoked on a fixed rhythm and decides for itself whether anything is due."""

    kind: Literal["polled"] = "polled"
    wake_url: str
    every: timedelta = timedelta(minutes=5)


class Command(Model):
    """A command run per wake; it reads a WakeRequest on stdin and prints an AgentReport."""

    kind: Literal["command"] = "command"
    argv: list[str]


class Contained(Model):
    """The agent runs in a sandbox whose clock Minutehand owns (a patched gVisor; docs/design.md, "A sandbox whose
    clock Minutehand owns"), so its own in-process timers are its next wakes, with no code of Minutehand's in it.

    Two commands reach the sandbox: `deadlines` prints, as its last line, `{"idle": bool, "earliest_ns": int}`
    (whether every task is blocked, and the time until the earliest deadline any waits for, -1 for none), and
    `advance` moves the sandbox's clock forward by `{nanoseconds}`. Minutehand moves it with every jump of the
    run's clock, so the two agree, and when the sandbox is idle and no call of the agent's is in flight, its
    earliest deadline is a wake the agent asked for."""

    kind: Literal["contained"] = "contained"
    deadlines: list[str] = Field(min_length=1)
    advance: list[str] = Field(min_length=1, description="Holds {nanoseconds} where the step goes")
    quiet: timedelta = Field(
        default=timedelta(milliseconds=50), gt=timedelta(0), description="Idle on two reads this far apart is idle"
    )
    settle_limit: timedelta = Field(
        default=timedelta(seconds=30), gt=timedelta(0), description="Real time a wake may take to fall idle"
    )

    @model_validator(mode="after")
    def _steps(self) -> Self:
        if not any("{nanoseconds}" in part for part in self.advance):
            raise ValueError("`advance` must hold {nanoseconds}, where the step it moves the clock by goes")
        return self


WakeSource = Annotated[Reported | Marked | Booked | Polled | Command | Contained, Field(discriminator="kind")]

ANSWER_LIMIT = timedelta(seconds=120)
"""How long the agent's report endpoint has to answer at the start of a fork."""


class OwnDatabaseForm(StrEnum):
    FILE = "file"  # the file's path: /…/runs/<run>/own/<VARIABLE>.sqlite
    SQLITE_URL = "sqlite_url"  # the same file as a URL: sqlite:////…/runs/<run>/own/<VARIABLE>.sqlite


class OwnDatabase(Model):
    """A database the agent keeps state in outside `minutehand_agent.store`, by the variable it reads its location
    from. Minutehand hands every run, and every fork, a fresh empty SQLite file in that variable, so no run reads
    another's or production's, and notes in the run that it is outside forks: a fork starts with an empty one, not
    the parent's at the checkpoint. Any other database (PostgreSQL, a cloud one) is the team's to provide per run."""

    env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", description="The variable the agent reads its location from")
    form: OwnDatabaseForm = OwnDatabaseForm.FILE


class GoalByWake(Model):
    """The goal arrives in the START wake's `WakeRequest.goal`, and directions in DIRECTION wakes."""

    kind: Literal["by_wake"] = "by_wake"


class GoalByMessage(Model):
    """The scenario's owner sends the goal as a message through this provider, as a person would, and every
    scripted direction the same way. An agent reached like this may expose no wake endpoint at all."""

    kind: Literal["by_message"] = "by_message"
    provider: ProviderKey


GoalSource = Annotated[GoalByWake | GoalByMessage, Field(discriminator="kind")]


class BaseUrl(Model):
    """A host the agent reaches by a base URL it is handed rather than through the proxy, for a client that cannot be
    given a proxy: `env` is set to `http://<minutehand>/_host/<host><path>`, which the proxy answers exactly as a call
    to `https://<host><path>` made through it."""

    host: str = Field(
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?$",
        description="The real host, with a port when it is not 443",
    )
    env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", description="The variable the agent reads its base URL from")
    path: str = Field(
        default="", pattern=r"^(/[^?#]*)?$", description="Appended to the base URL, e.g. '/api/1.0'; empty for none"
    )


AGENT_FILE_VERSION = 1
"""The version of the agent file this Minutehand reads. A file may say which it was written for (`version`); one
naming a later version is refused, since it may hold what this one would misread. Absent: this one."""


class AgentUnderTest(Model):
    """How the monitor reaches the agent. Replies and pushed events always wake it;
    `wakes` lists every other way it comes back to work, and may be empty when the goal is sent as a message."""

    version: int | None = Field(
        default=None, ge=1, description="The agent file version it is written for; absent: the current one"
    )
    name: str
    goal: GoalSource = GoalByWake()
    wakes: list[WakeSource] = []
    tick: timedelta | None = Field(
        default=None,
        gt=timedelta(0),
        description="How often the agent comes back to work by itself at most, when that rhythm is its own (it "
        "reports or books its next wake); a polled wake's `every` counts without it. Sizes the wake limit from the "
        "scenario's deadline",
    )
    inbound: list[InboundTarget] = []
    inboxes: list[HttpInbox] = Field(
        default=[],
        description="Where work waits on a person inside the agent's own product (an approval, a question on its "
        "own page), read and decided as each person (`domain.inboxes`)",
    )
    watches: list[str] = Field(
        default=[],
        description="Folders of the agent's own machine whose files Minutehand records: what the agent creates, "
        "changes or removes in a wake, and what a scenario's machine commands do. A relative path is read from the "
        "agent file's folder",
    )
    checks: list[str] = Field(
        default=[],
        description="Python files holding checks of the agent's own, written as Minutehand's are: a class with `id`, "
        "`needs` and `run(view) -> CheckReport`. Run with Minutehand's after every run and fork. A relative path is "
        "read from the agent file's folder",
    )
    assess: list[Rule] = Field(
        default=[],
        description="The team's own rules for judging the agent, over the facts of each run (`docs/assessments.md`). "
        "A scenario may replace one by its id, add its own, or switch one off (`Scenario.assess_off`). Nothing else "
        "judges how the agent behaves",
    )
    outbound: list[OutboundHost] = Field(
        default=[], description="Hosts that are not places the agent keeps state, captured rather than faked"
    )
    base_urls: list[BaseUrl] = Field(
        default=[], description="Hosts the agent is handed a base URL for, each in its own variable, beside the proxy"
    )
    emulators: list[ExternalEmulator] = Field(
        default=[], description="Fakes outside Minutehand that `forward` hosts are sent to, started or attached to"
    )
    own_databases: list[OwnDatabase] = Field(
        default=[],
        description="Databases the agent keeps state in beside its memory, by the variable it reads each from: each "
        "run is handed a fresh empty SQLite file in each, outside forks",
    )

    @model_validator(mode="before")
    @classmethod
    def _no_hooks(cls, written: object) -> object:
        if isinstance(written, dict) and ("state" in written or "databases" in written):
            raise ValueError(
                "`state:` hooks and fronted `databases:` are gone: the agent keeps what it remembers in "
                "`minutehand_agent.store`, which every run and fork holds for it (docs/agent-contract.md); a database "
                "it keeps beside that is named under `own_databases`"
            )
        return written

    @model_validator(mode="after")
    def _goal_reaches_it(self) -> AgentUnderTest:
        if self.version is not None and self.version > AGENT_FILE_VERSION:
            raise ValueError(
                f"this agent file is written for version {self.version} of the agent file, and this Minutehand reads "
                f"up to version {AGENT_FILE_VERSION}: upgrade Minutehand to run it"
            )
        refuse_repeats(self.outbound)
        refuse_repeated_inboxes(self.inboxes)
        refuse_repeated_rules(self.assess)
        clash = sorted({i.name for i in self.inboxes} & {d.key for d in self.outbound})
        if clash:
            raise ValueError(f"an inbox and an outbound host are both recorded as {', '.join(clash)}")
        refuse_unknown_emulators([d.emulator for d in self.outbound if isinstance(d, Forward)], self.emulators)
        owned = [d.env for d in self.own_databases]
        if len(owned) != len(set(owned)):
            raise ValueError(f"two own databases are handed out in one variable: {sorted(set(owned))}")
        named = [b.env for b in self.base_urls]
        if len(named) != len(set(named)):
            raise ValueError(
                f"two base URLs are handed out in one variable: {sorted(n for n in set(named) if named.count(n) > 1)}"
            )
        if isinstance(self.goal, GoalByMessage):
            if not any(t.provider == self.goal.provider for t in self.inbound):
                raise ValueError(
                    f"the goal is sent as a message on {self.goal.provider}, "
                    "and the agent declares no inbound target there"
                )
        elif not self.wakes:
            raise ValueError("the goal is handed over in a wake, and the agent declares no way to be woken")
        return self
