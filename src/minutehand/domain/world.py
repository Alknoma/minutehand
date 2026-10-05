"""What the fakes record: every call the agent made and what it changed."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field

from minutehand.domain.scenario import AccessRole, Model, ProviderKey, TicketState


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


class CaptureMode(StrEnum):
    """How a host no provider claims was declared (`domain.outbound`), or found under `--capture-unknown`."""

    ACKNOWLEDGE = "acknowledge"
    PASS_THROUGH = "pass_through"
    REPLAY = "replay"
    DISCOVERED = "discovered"  # declared by nobody; passed through because the run captures unknown hosts


class AnsweredBy(StrEnum):
    DECLARATION = "declaration"  # the declared answer; the call never left the machine
    REAL_HOST = "real_host"  # the real host, reached through the proxy
    RECORDING = "recording"  # an earlier run's recording of the same call
    REFUSAL = "refusal"  # nobody: a replay that missed, declared to refuse


class BodyKept(StrEnum):
    """How much of a body the record holds."""

    WHOLE = "whole"
    TRUNCATED = "truncated"  # text, kept up to the declared limit
    BYTES = "raw"  # kept whole as the bytes that crossed: binary, or not valid text in its declared charset
    BINARY = "binary"  # bytes longer than the declared limit: its length, content type and hash only
    EMPTY = "empty"


class Body(Model):
    content_type: str | None = None
    size: int = Field(ge=0, description="Bytes, the whole body as it crossed the wire, decoded")
    kept: BodyKept
    sha256: str = Field(description="Of the whole body with credentials and declared fields redacted")


class Recipient(Model):
    """One recipient read out of a captured send."""

    address: str = Field(description="As the request named it")
    person: str | None = Field(description="Person.key it reached; None when the scenario has nobody by it")


class Captured(Model):
    """A call to a host no provider claims, captured rather than refused: how, by what, and when on the real
    clock. The run's clock and wake are on the `RecordedCall`."""

    mode: CaptureMode
    declared_as: str | None = Field(description="The declaration's host pattern; None under --capture-unknown")
    answered_by: AnsweredBy
    replayed_from: str | None = Field(default=None, description="The recording that answered it, when one did")
    note: str | None = Field(default=None, description="What went other than declared: a replay's miss, a send unread")
    started: AwareDatetime = Field(description="Real time the request began")
    ended: AwareDatetime = Field(description="Real time the answer ended")
    request: Body
    response: Body
    streamed: bool = Field(default=False, description="The answer reached the agent chunk by chunk")
    recipients: list[Recipient] = Field(default=[], description="Read out of a send declared as a message")


class TunnelRoute(StrEnum):
    """Why a tunnelled call is kept in the world it is kept in."""

    RUN = "run"  # `minutehand run` or `fork`: every call is the one run's
    HOST = "host"  # `minutehand serve`: the world that declared the host a model host
    NONE = "none"  # `minutehand serve`: no world claims the host; kept in the lobby, and listed as unmatched


class Tunnelled(Model):
    """A call on a tunnel the proxy relays as bytes and never opens (a model API the run neither edits nor
    records): that it happened, never what it said. Its bodies are not opened, so the `Exchange` holds none.

    One record is one burst: the bytes that moved from the first after the connection opened, or after the
    previous burst was written, until the server had answered and the connection fell quiet, or it closed. A
    keep-alive connection reused across wakes is a record in each wake it carried a request in, each stamped
    with the wake and simulated time in progress when its first byte moved."""

    port: int = Field(ge=0, le=65535)
    connection: str = Field(description="The tunnel's own id, the same on every burst it carried")
    burst: int = Field(ge=1, description="This burst's number on its connection, from 1")
    opened: AwareDatetime = Field(description="Real time the agent's connection to the proxy opened")
    started: AwareDatetime = Field(description="Real time of the burst's first byte")
    ended: AwareDatetime = Field(description="Real time of the burst's last byte")
    closed: AwareDatetime | None = Field(
        default=None, description="Real time the connection closed, when it closed at the end of this burst"
    )
    bytes_sent: int = Field(ge=0, description="Bytes from the agent, encrypted, as they crossed")
    bytes_received: int = Field(ge=0, description="Bytes to the agent, encrypted, as they crossed")
    route: TunnelRoute


class Exchange(Model):
    """One HTTP call as it crossed the wire. Bodies are the provider's own format.

    A body that is valid UTF-8 is `*_body`, text, with credentials redacted. Any other (a .docx, an image, JSON in
    another encoding or with bytes that are no text at all) is `*_bytes`, exactly the bytes that crossed, and its
    `*_body` is None: every call is recorded whatever its bodies hold. In JSON, bytes are base64."""

    model_config = ConfigDict(frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64")

    method: str
    host: str
    path: str = Field(description="With its query string, credentials redacted")
    status: int
    request_body: str | None = None
    response_body: str | None = None
    request_bytes: bytes | None = Field(default=None, description="The request body when it is not UTF-8 text")
    response_bytes: bytes | None = Field(default=None, description="The answer's body when it is not UTF-8 text")
    traceparent: str | None = Field(default=None, description="W3C trace context the caller sent, if any")
    captured: Captured | None = Field(default=None, description="Set for a call to a host no provider claims")
    tunnelled: Tunnelled | None = Field(
        default=None,
        description="Set for a burst on a tunnel the proxy never opened: `method` is CONNECT, `path` its "
        "host:port, `status` the 200 the proxy answered the CONNECT with, and no body is kept",
    )


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
    answerable: bool = Field(
        default=True,
        description="Whether its recipients can answer where it was sent; False for a captured send whose "
        "declaration says nothing of replies (`Acknowledge.replies`), which nobody can answer",
    )


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
    owner: str | None = Field(
        default=None, description="Who owns it: a person's email, or the integration's name; None when unknown"
    )
    space: str | None = Field(
        default=None,
        description="The name of the shared place it lives in (a shared drive, a site, a workspace); None when it is "
        "its owner's own",
    )


class GrantSnapshot(Model):
    """Someone was given access to a document: a person by email, a domain, or anyone with the link."""

    kind: Literal["grant"] = "grant"
    document: str = Field(description="The title of the document, as it read when access was given")
    to: str = Field(description="A person's email; a domain; or `anyone`")
    role: AccessRole


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
    TicketSnapshot | MessageSnapshot | DocumentSnapshot | GrantSnapshot | RecordSnapshot | InteractionSnapshot,
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


class CallBegan(Model):
    """Where on the run's clock a call began, for one recorded only after it ended."""

    wake: int = Field(ge=0)
    sim_time: AwareDatetime


class RecordedCall(Model):
    """One HTTP call the proxy saw, with the events it produced, if any.

    `provider` is None when no provider claimed the host: the call was captured (`exchange.captured`), relayed
    unopened on a tunnel (`exchange.tunnelled`) or, when both are None, refused. `first_seq > last_seq` means the
    call produced no event. `wake` and `sim_time` are those in progress when the call began.
    """

    exchange: Exchange
    provider: ProviderKey | None
    first_seq: int
    last_seq: int
    wake: int
    sim_time: AwareDatetime

    @property
    def refused(self) -> bool:
        """No provider claimed it, nothing captured it and no tunnel carried it: it was answered 502 and reached
        nothing."""
        return self.provider is None and self.exchange.captured is None and self.exchange.tunnelled is None
