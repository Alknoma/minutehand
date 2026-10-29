"""A run told for someone who will never open Minutehand: who was in it, what was said where, what was already
written down, what the agent drafted for approval and what it did itself, and what Minutehand's checks said of it
(`minutehand export`, docs/exporting.md).

Two kinds of model live here. What a provider tells the exporter about its own things, which only it can read
from its own JSON: what a conversation is called and who is in it, who wrote a message, the reference a ticket
shows. And the record the exporter writes, `minutehand-replay-record/2`, in the shape a reader of it validates:
its field names are that reader's (`replyTo`, `with`), so the models serialize by alias.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from minutehand.domain.checks import FindingKind, Severity
from minutehand.domain.model import Model
from minutehand.domain.run import StopReason, VerdictKind

RECORD_VERSION = "minutehand-replay-record/2"


class ConversationKind(StrEnum):
    """What a conversation is, whatever its provider calls it."""

    CHANNEL = "named"  # a named place many people read
    DIRECT = "one_to_one"  # the agent and one person
    GROUP = "several"  # the agent and several people, unnamed


# ---------------------------------------------------------------------------- what a provider says of its own


class PresentedConversation(Model):
    """A conversation as its provider knows it: its name, or the people in it besides the agent."""

    id: str = Field(description="The provider's own id, as the run's messages name it")
    kind: ConversationKind
    name: str | None = Field(default=None, description="A channel's name; None for a direct or group conversation")
    people: list[str] = Field(default=[], description="Person.key of everyone in it but the agent")


class PresentedAuthor(Model):
    """Who wrote one message, as its provider records it."""

    message: str = Field(description="The message's id, as the run's events name it")
    person: str | None = Field(description="Person.key of its author; None when the agent or nobody in the scenario")
    agent: bool = Field(description="Whether the agent's own account wrote it")
    written_at: AwareDatetime = Field(
        description="When the provider dates it: for a message already there when the run began, before the run"
    )
    listed: bool = Field(description="Whether anyone can list it; False for one shown to a single member once")


class PresentedReference(Model):
    """The reference a person sees on an item and writes in a message ('ENG-12'), for one the provider holds."""

    item: str = Field(description="The item's id, as the run's events name it")
    reference: str


class PresentedAgent(Model):
    """The agent as a provider shows it to people: its account's id there, and the name it goes by."""

    account: str
    name: str | None = Field(default=None, description="The name its account shows; None when it shows none")


# ---------------------------------------------------------------------------- the exported record


class _Out(Model):
    model_config = ConfigDict(frozen=True, extra="forbid", serialize_by_alias=True, validate_by_name=True)


class RecordRun(_Out):
    id: str
    scenario: str
    timezone: str = Field(description="The zone every time is shown in: the scenario owner's, else its first person's")
    start: AwareDatetime
    end: AwareDatetime
    ran_on: date = Field(alias="ranOn", description="The day the run was performed, by the machine's clock")
    chat: str = Field(description="The chat service the conversations are in, as its users call it")
    markup: Literal["slack", "markdown", "plain"]
    owner: str | None = Field(default=None, description="Person.key of the scenario's owner, when it names one")
    goal: str | None = Field(default=None, description="The scenario's goal, verbatim, when it has one")
    deadline: AwareDatetime | None = Field(
        default=None, description="The run's start plus the scenario's deadline_after, when it gives one"
    )


class RecordAgent(_Out):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str
    chat_id: str | None = Field(default=None, alias="chatId")


class RecordPerson(_Out):
    key: str
    name: str
    title: str | None = None
    chat_id: str | None = Field(default=None, alias="chatId")


class RecordChannel(_Out):
    kind: Literal["channel"] = "channel"
    id: str
    name: str


class RecordDirect(_Out):
    kind: Literal["direct"] = "direct"
    id: str
    person: str = Field(alias="with")


class RecordGroup(_Out):
    kind: Literal["group"] = "group"
    id: str
    people: list[str] = Field(alias="with", min_length=2)


RecordConversation = Annotated[RecordChannel | RecordDirect | RecordGroup, Field(discriminator="kind")]


class RecordMessage(_Out):
    id: str
    at: AwareDatetime
    by: str
    conversation: str
    reply_to: str | None = Field(default=None, alias="replyTo")
    text: str
    wake: int = Field(ge=0, description="0: there before the run began")
    edited: bool
    edited_until: AwareDatetime | None = Field(default=None, alias="editedUntil")


class RecordWake(_Out):
    n: int = Field(ge=1)
    starts: AwareDatetime = Field(alias="from")
    ends: AwareDatetime = Field(alias="to")


class RecordAction(_Out):
    id: str
    at: AwareDatetime
    wake: int = Field(ge=1)
    did: str = Field(description="What the agent did, as a reader would say it: 'Created a ticket'")
    title: str
    tool: str
    place: str | None = None
    ref: str | None = None


class RecordItem(_Out):
    id: str
    kind: str = Field(description="What it is, as its tool calls it: 'Ticket', 'Document', 'Spreadsheet'")
    ref: str | None = None
    tool: str
    place: str
    title: str
    text: str | None = None
    rows: list[list[str]] | None = None
    state: Literal["open", "done"] | None = None
    assignee: str | None = None


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DECIDED = "decided"  # decided some other way: the decision is named
    WITHDRAWN = "withdrawn"  # gone from the person's inbox without their deciding it


class RecordApproval(_Out):
    id: str
    at: AwareDatetime = Field(description="When it was first seen waiting")
    person: str = Field(alias="for")
    status: ApprovalStatus
    summary: str
    decision: str | None = None


class RecordCite(_Out):
    """What a finding points at, as the record names it: exactly one of a message, an action or an approval."""

    message: str | None = Field(default=None, description="RecordMessage.id")
    action: str | None = Field(default=None, description="RecordAction.id")
    approval: str | None = Field(default=None, description="RecordApproval.id")

    @model_validator(mode="after")
    def _one(self) -> RecordCite:
        named = [n for n in (self.message, self.action, self.approval) if n is not None]
        if len(named) != 1:
            raise ValueError("a cite names exactly one of message, action or approval")
        return self


class RecordFinding(_Out):
    check: str
    severity: Severity
    kind: FindingKind
    text: str = Field(description="Finding.message")
    at: AwareDatetime | None = None
    wake: int | None = None
    cites: list[RecordCite] = Field(description="Its evidence that is in the record, in order, each once")


class RecordAssessment(_Out):
    """Minutehand's verdict on a finished run and its checks' findings; never the verdict's words, which name hosts."""

    verdict: VerdictKind
    stop: StopReason | None = None
    failed_checks: int = Field(ge=0, alias="failedChecks")
    open_waits: int = Field(ge=0, alias="openWaits")
    findings: list[RecordFinding]


class ReplayRecord(_Out):
    version: Literal["minutehand-replay-record/2"] = Field(default=RECORD_VERSION, alias="schema")
    run: RecordRun
    agent: RecordAgent
    people: list[RecordPerson]
    conversations: list[RecordConversation]
    messages: list[RecordMessage]
    wakes: list[RecordWake]
    actions: list[RecordAction]
    items: list[RecordItem]
    approvals: list[RecordApproval]
    assessment: RecordAssessment | None = Field(
        default=None, description="None for a run with no verdict: still being written, or died before its record"
    )
