"""The agent under test: the sandbox it runs in, and how the world reaches it.

Minutehand asks the agent nothing. Everything it learns, it observes: the agent's calls through the proxy, and the
sandbox whose clock Minutehand owns falling idle.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from minutehand.domain.assessments import IntegrityCheck, Rule, refuse_repeated_rules
from minutehand.domain.emulator import ExternalEmulator, refuse_unknown_emulators
from minutehand.domain.inboxes import HttpInbox, refuse_repeated_inboxes
from minutehand.domain.memory import DueRecords
from minutehand.domain.outbound import Forward, OutboundHost, refuse_repeats
from minutehand.domain.people import InboundTarget
from minutehand.domain.scenario import Model, ProviderKey
from minutehand.domain.world import EntityRef


class WakeReason(StrEnum):
    START = "start"
    DUE = "due"
    PERSON_REPLIED = "person_replied"
    DIRECTION = "direction"


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


class AgentReport(Model):
    """What a harness that drives the agent itself says of the agent's work, in a standing world (`minutehand serve`,
    `POST /v1/worlds/{id}/report`): whether it is done, and what it still holds open. A run asks the agent nothing
    and holds no report."""

    status: AgentStatus
    commitments: list[Commitment] | None = None


class Sandbox(Model):
    """The sandbox every agent runs in, whose clock Minutehand owns (a patched gVisor; docs/design.md, "A sandbox
    whose clock Minutehand owns"): every way the agent reads the time reads the run's, with no code of Minutehand's
    in it, and nothing the agent calls leaves the sandbox except through the proxy.

    Two commands reach the sandbox: `deadlines` prints, as its last line, a JSON object holding `"idle": bool`
    (whether every task is blocked) and `"now_ns": int` (its realtime clock); anything else it prints there is
    the sandbox's and ignored. `advance` moves the sandbox's clock forward by `{nanoseconds}`, with every jump of
    the run's clock, so the two agree. The sandbox is born at the run's start (its command reads
    `MINUTEHAND_STARTS_AT_NS`) and its clock is never moved back: a timer the agent armed before a backward step
    would fire early. A wake is over when the sandbox is idle on two reads `quiet` apart, no call of the agent's is
    in flight at either, and none was sent or answered between them. No deadline a task in it waits for is a wake:
    a timer the agent armed fires when the clock passes it for something else, and work the agent means to come
    back to is a booking with a scheduler, kept in the run's log (docs/design.md, "How the clock knows what is
    next").

    A third command is the agent's own and optional: `export_state` saves what the agent holds (a database emulator in
    its sandbox, its files) into a folder at each checkpoint after a wake, so a fork from there starts the agent from
    it (`MINUTEHAND_AGENT_STATE`) rather than by playing its recorded inputs back, and an agent whose code changed
    since can carry the run on (docs/design.md, "The agent's own saved state").

    Two more are optional and named together: `hold` stops the sandbox's clock and `release` lets it run on from the
    moment held. With them the agent's clock moves only inside a wake: held as soon as a wake has settled, advanced to
    the next wake's moment while held, and released as that wake begins (docs/design.md, "A held clock between
    wakes"). Without them it runs at real speed between wakes, and a timer of the agent's that comes due then starts
    work in no wake."""

    deadlines: list[str] = Field(min_length=1)
    advance: list[str] = Field(min_length=1, description="Holds {nanoseconds} where the step forward goes")
    quiet: timedelta = Field(
        default=timedelta(milliseconds=50),
        gt=timedelta(0),
        description="Idle on two reads this far apart, with no call of the agent's in flight at either and none "
        "sent or answered between them, is idle",
    )
    settle_limit: timedelta = Field(
        default=timedelta(seconds=30), gt=timedelta(0), description="Real time a wake may take to fall idle"
    )
    export_state: list[str] | None = Field(
        default=None,
        min_length=1,
        description="Saves the agent's own state (its databases, its files) into the folder `{dir}` names, run by "
        "Minutehand at every checkpoint after a wake, only once the sandbox has settled as idle (`settle`: the same "
        "reading that ends a wake), before the checkpoint is written, so what it saves is exactly as consistent as "
        "that reading: "
        "a fork from that checkpoint starts the agent with `MINUTEHAND_AGENT_STATE` naming the folder, for it to "
        "import as it starts, in place of playing its recorded inputs back. Its failure is kept with the checkpoint, "
        "and a fork from there plays back as without it",
    )

    hold: list[str] | None = Field(
        default=None,
        min_length=1,
        description="Holds the sandbox's clock still: every read of it returns the moment of the hold, and no timer on "
        "it fires. Run by Minutehand as soon as a wake has settled, so nothing the agent's own timers start (a "
        "scheduler loop's next cycle) runs while Minutehand reads inboxes, plays the people, writes a checkpoint or "
        "saves the agent's state; named with `release`, or neither. Without them the sandbox's clock runs at real "
        "speed between wakes",
    )
    release: list[str] | None = Field(
        default=None,
        min_length=1,
        description="Lets a held clock run on from the moment it was held, so none of the time held passed for the "
        "agent. Run by Minutehand as a wake begins, once the clock has been advanced to the wake's moment",
    )

    @model_validator(mode="before")
    @classmethod
    def _no_housekeeping(cls, written: object) -> object:
        if isinstance(written, dict) and "housekeeping" in written:
            raise ValueError(
                "`sandbox.housekeeping` is gone: no deadline in the sandbox is a wake any more, so there is no floor "
                "to set. Work the agent comes back to by itself is a booking with a scheduler it calls (Cloud Tasks, "
                "EventBridge Scheduler, Slack's chat.scheduleMessage), kept in the run's log (docs/agent-contract.md)"
            )
        return written

    @model_validator(mode="after")
    def _steps(self) -> Self:
        if not any("{nanoseconds}" in part for part in self.advance):
            raise ValueError("`advance` must hold {nanoseconds}, where the step it moves the clock by goes")
        if self.export_state is not None and not any("{dir}" in part for part in self.export_state):
            raise ValueError("`export_state` must hold {dir}, the folder it saves the agent's state into")
        if (self.hold is None) != (self.release is None):
            raise ValueError(
                "`hold` and `release` are named together or not at all: a clock held and never released stops the "
                "agent for good, and one released and never held was never stopped"
            )
        return self


class OwnDatabaseForm(StrEnum):
    FILE = "file"  # the file's path: /…/runs/<run>/own/<VARIABLE>.sqlite
    SQLITE_URL = "sqlite_url"  # the same file as a URL: sqlite:////…/runs/<run>/own/<VARIABLE>.sqlite


class OwnDatabase(Model):
    """A database the agent keeps state in outside `minutehand.agent.store`, by the variable it reads its location
    from. Minutehand hands every run, and every fork, a fresh empty SQLite file in that variable, so no run reads
    another's or production's, and notes in the run that it is outside forks: a fork starts with an empty one, not
    the parent's at the checkpoint. Any other database (PostgreSQL, a cloud one) is the team's to provide per run."""

    env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", description="The variable the agent reads its location from")
    form: OwnDatabaseForm = OwnDatabaseForm.FILE


class GoalByMessage(Model):
    """The scenario's owner sends the goal as a message through this provider, as a person would, and every
    scripted direction the same way."""

    provider: ProviderKey


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


class Instructions(Model):
    """A file whose words state the agent's work: its prompt, its task list, its rules. Read when the agent file is,
    and kept with the run, so the reviewers judge the agent against what it was told, not against a prompt it gave a
    model for one part of its work."""

    file: str = Field(
        description="A path, relative to the agent file's folder; `${NAME}` or `${NAME:-default}` reads the "
        "environment Minutehand runs in, as the agent reads it (an agent told its work file by a variable)"
    )
    text: str | None = Field(
        default=None, description="The file's words, filled when Minutehand reads the agent file; left out by hand"
    )


class AgentUnderTest(Model):
    """The agent under test: the sandbox it runs in, and how the world reaches it. Nothing is asked of it: it wakes
    when a person's answer or a pushed event reaches it, when a scheduler it booked with delivers, and when one of the
    due records it keeps in its memory (`due`) falls due, as the record stands when the run decides its next jump."""

    version: int | None = Field(
        default=None, ge=1, description="The agent file version it is written for; absent: the current one"
    )
    name: str
    sandbox: Sandbox
    goal: GoalByMessage | None = Field(
        default=None,
        description="Where an older scenario's owner sends its goal and directions, as a person would; none for a "
        "scenario that is only a world",
    )
    start_limit: timedelta = Field(
        default=timedelta(seconds=30),
        gt=timedelta(0),
        description="How long, in real time, the agent's command may take to start: from its launch until the address "
        "the world pushes to accepts connections. Nothing is pushed before then",
    )
    take_limit: timedelta = Field(
        default=timedelta(seconds=30),
        gt=timedelta(0),
        description="How long, in real time, the run waits for the agent to take a booking's delivery from its own "
        "queue before it moves on, when the scheduler can tell (`ConfirmsDelivery`)",
    )
    tick: timedelta | None = Field(
        default=None,
        gt=timedelta(0),
        description="How often the agent comes back to work by itself at most, by its bookings or its due records. "
        "Sizes the wake limit from the scenario's deadline",
    )
    inbound: list[InboundTarget] = []
    due: list[DueRecords] = Field(
        default=[],
        description="Where the agent keeps its due records in its memory (`minutehand.agent.store`): the run reads "
        "them as they stand whenever it decides its next jump, and the earliest one's instant is a wake",
    )
    inboxes: list[HttpInbox] = Field(
        default=[],
        description="Where work waits on a person inside the agent's own product (an approval, a question on its "
        "own page), read and decided as each person (`domain.inboxes`)",
    )
    instructions: list[Instructions] = Field(
        default=[],
        description="The files that state the agent's work. When named, the reviewers judge every effect against "
        "them; when not, against the system prompts the agent gave its model (`docs/assessments.md`)",
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
        description="Optional team policy: rules over the facts of each run (`docs/assessments.md`), for what the "
        "world and the agent's own instructions cannot imply. Every run is assessed without them. A scenario may "
        "replace one by its id, add its own, or switch one off (`Scenario.assess_off`)",
    )
    fail_on_integrity: list[IntegrityCheck] = Field(
        default=[],
        description="Integrity facts that fail the run when they are found (`around_proxy`, "
        "`agent_contract_changed`, `unmatched_call`); unnamed, each is stated as `review` and never changes the verdict",
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
                "`minutehand.agent.store`, which every run and fork holds for it (docs/agent-contract.md); a database "
                "it keeps beside that is named under `own_databases`"
            )
        if isinstance(written, dict) and "wakes" in written:
            raise ValueError(
                "`wakes:` is gone: every agent runs in a sandbox whose clock Minutehand owns (`sandbox`), and it "
                "comes back to work by itself through a scheduler it books with, observed at the proxy; nothing is "
                "asked of it (docs/agent-contract.md)"
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
        if self.goal is not None and not any(t.provider == self.goal.provider for t in self.inbound):
            raise ValueError(
                f"the goal is sent as a message on {self.goal.provider}, and the agent declares no inbound target there"
            )
        return self
