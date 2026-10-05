"""What the fakes record: every call the agent made and what it changed."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from minutehand.domain.scenario import Model, ProviderKey, TicketState


class EntityKind(StrEnum):
    MESSAGE = "message"
    TICKET = "ticket"
    COMMENT = "comment"
    DOCUMENT = "document"
    CHANNEL = "channel"
    RECORD = "record"


class Operation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    READ = "read"
    SEARCH = "search"


class Actor(StrEnum):
    AGENT = "agent"
    PERSON = "person"
    SCENARIO = "scenario"


class EntityRef(Model):
    provider: ProviderKey
    kind: EntityKind
    external_id: str


class Exchange(Model):
    """One HTTP call as it crossed the wire. Bodies are the provider's own format."""

    method: str
    host: str
    path: str
    status: int
    request_body: str | None = None
    response_body: str | None = None
    traceparent: str | None = Field(default=None, description="W3C trace context the caller sent, if any")


class TicketSnapshot(Model):
    kind: Literal["ticket"] = "ticket"
    title: str
    body: str = ""
    project: str | None = None
    assignee_email: str | None = None
    state: TicketState = TicketState.OPEN


class MessageSnapshot(Model):
    kind: Literal["message"] = "message"
    text: str
    channel: str
    recipient_emails: list[str] = []
    thread_of: str | None = None


class DocumentSnapshot(Model):
    """A document as it read after a change. A provider that cannot flatten its content leaves `text` None."""

    kind: Literal["document"] = "document"
    title: str
    mime_type: str | None = None
    text: str | None = Field(default=None, description="The whole content flattened to text, one block a line")
    parent: str | None = Field(default=None, description="The title of what it sits under; None at the top")
    last_edited_by: str | None = Field(
        default=None, description="Who last changed it: a person's email, or the name of the integration's bot"
    )
    last_edited_at: AwareDatetime | None = Field(default=None, description="Simulated time of that change")


class RecordSnapshot(Model):
    """Any resource of a provider nobody has mapped to a ticket, message or document.

    A generated provider emits these. Two checks read them: `near_miss_name`, which
    needs only the written text, and `acted_after_deadline`, which needs only that a
    write happened. `duplicate_ticket` and `repeated_message` do not: a duplicate is
    a title filed twice in one project, and a repeat is two messages to one channel,
    and a record carries neither a title, a project nor a channel.
    """

    kind: Literal["record"] = "record"
    resource: str = Field(description="The provider's own resource name, e.g. 'invoices'")
    text: str = Field(description="Every string field of the body, joined")


Snapshot = Annotated[
    TicketSnapshot | MessageSnapshot | DocumentSnapshot | RecordSnapshot,
    Field(discriminator="kind"),
]


class WorldEvent(Model):
    """One change, or one read, in a shape that is the same for every provider."""

    seq: int = Field(ge=1)
    run_id: str
    wake: int = Field(ge=0, description="The agent wake this happened in; 0 is setup")
    sim_time: AwareDatetime
    wall_time: AwareDatetime
    actor: Actor
    operation: Operation
    entity: EntityRef
    after: Snapshot | None = None
    exchange: Exchange | None = None


class Change(Model):
    """What a provider hands the store: one thing that happened to one entity.

    `body` is the entity in the provider's own JSON, as text. It is None for a
    delete, a read or a search, which change nothing and are recorded all the same.
    """

    entity: EntityRef
    operation: Operation
    actor: Actor
    body: str | None = None
    parent: str | None = Field(default=None, description="What this entity is listed under: a channel, a project")
    after: Snapshot | None = None


class Stored(Model):
    """An entity as the store holds it at the run's head."""

    entity: EntityRef
    body: str
    parent: str | None
    seq: int = Field(description="The event that wrote this version")
    sim_time: AwareDatetime


class RecordedCall(Model):
    """One HTTP call the proxy saw, with the events it produced, if any.

    `provider` is None when no provider claimed the host: the call was refused.
    `first_seq > last_seq` means the call produced no event.
    """

    exchange: Exchange
    provider: ProviderKey | None
    first_seq: int
    last_seq: int
    wake: int
    sim_time: AwareDatetime
