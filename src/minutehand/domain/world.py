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


class ControlKind(StrEnum):
    """What a control on a message does when a reader uses it."""

    BUTTON = "button"
    USER_SELECT = "user_select"
    LINK = "link"


class MessageAction(Model):
    """One control a reader can use on a message: a button, a person picker, a link that opens a page.

    `action_id` and `value` are the provider's own, exactly as the agent wrote them; `label` is what the reader sees.
    """

    action_id: str
    label: str
    control: ControlKind = ControlKind.BUTTON
    value: str | None = None


class MessageSnapshot(Model):
    kind: Literal["message"] = "message"
    text: str
    channel: str
    recipient_emails: list[str] = []
    thread_of: str | None = None
    actions: list[MessageAction] = Field(default=[], description="What a reader can press or pick on it, in order")


class InteractionKind(StrEnum):
    PRESS = "press"
    SUBMIT = "submit"


class InteractionSnapshot(Model):
    """A person used a control on something the agent showed them: pressed a button on a message, or submitted a
    form the agent opened for them. The event's actor is PERSON; `person` says which one."""

    kind: Literal["interaction"] = "interaction"
    interaction: InteractionKind
    person: str = Field(description="Person.key")
    on: EntityRef = Field(description="The message pressed on, or the message whose press opened the form")
    action_id: str = Field(description="The control pressed, or the form's own id when submitted")
    label: str = Field(description="What the person saw: the button's label, or the form's title")
    value: str | None = Field(default=None, description="The control's value, or the member picked, as sent")
    form: list[str] = Field(default=[], description="What the person typed into the form, field by field")


class DocumentSnapshot(Model):
    kind: Literal["document"] = "document"
    title: str
    mime_type: str | None = None


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
    TicketSnapshot | MessageSnapshot | DocumentSnapshot | RecordSnapshot | InteractionSnapshot,
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
