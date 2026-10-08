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
    INBOX_ITEM = "inbox_item"  # something waiting on a person in the agent's own product (`domain.inboxes`)
    DUE = "due"  # an entry of the run loop's own table of what is due next (`domain.clock.DueEntry`)
    FILE = "file"  # a file in a folder of the agent's own machine the agent file says to watch
    TOOL_CALL = "tool_call"  # a tool the agent called on an MCP server
    MEMORY = "memory"  # a key of the agent's own memory, written or read through `minutehand.agent.store`
    NEXT_WAKE = "next_wake"  # the moment the agent asked to be woken next through `minutehand.agent.wake`


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
    FORWARD = "forward"  # sent to an external emulator the agent file or world declares (`domain.emulator`)
    MODELED = "modeled"  # declared by nobody; answered by a model standing in for the service (`UnknownHosts.MODEL`)


class AnsweredBy(StrEnum):
    DECLARATION = "declaration"  # the declared answer; the call never left the machine
    REAL_HOST = "real_host"  # the real host, reached through the proxy
    RECORDING = "recording"  # an earlier run's recording of the same call
    REFUSAL = "refusal"  # nobody: a replay that missed, declared to refuse, or an emulator that was unavailable
    EMULATOR = "emulator"  # an external emulator, named in `Captured.emulator`
    MODEL = "model"  # a language model standing in for a service nobody declared; never sent


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


class CallOutcome(StrEnum):
    """What a call's answer was, as distinct from its status: who is at fault when it is not a plain answer.

    Set on every forwarded call (`CaptureMode.FORWARD`) and on every call a provider in this process answered
    (`adapters.answering`); None on a call nothing classified."""

    ANSWERED = "answered"  # the service answered as it would
    REFUSED = "refused"  # the service refused it, as the real one would: a 4xx, or an error declared faithful
    NOT_IMPLEMENTED = "not_implemented"  # the fake has no answer for it: a 501, or a declared not-implemented marker
    INTERNAL_ERROR = "internal_error"  # the fake broke answering it: a 5xx nobody declared a faithful error
    INJECTED_FAULT = "injected_fault"  # a fault the scenario or the test declared
    UNAVAILABLE = "unavailable"  # nothing answered: the external emulator was down, unhealthy or did not answer


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
    emulator: str | None = Field(default=None, description="The external emulator it was forwarded to, by name")
    operation: str | None = Field(
        default=None,
        description="What a forwarded call asked for: a GraphQL operation's name, else its method and path",
    )
    forwarded_traceparent: str | None = Field(
        default=None,
        description="The `traceparent` the forwarded copy carried: the agent's trace (or one begun for it) with "
        "Minutehand's span of this call as the parent, under which the emulator's own spans sit",
    )


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


class GrpcCode(StrEnum):
    """A gRPC status, by the name gRPC gives it (`grpc-status` carries its number on the wire)."""

    OK = "OK"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    FAILED_PRECONDITION = "FAILED_PRECONDITION"
    ABORTED = "ABORTED"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    UNIMPLEMENTED = "UNIMPLEMENTED"
    INTERNAL = "INTERNAL"
    UNAVAILABLE = "UNAVAILABLE"
    DATA_LOSS = "DATA_LOSS"
    UNAUTHENTICATED = "UNAUTHENTICATED"


GRPC_NUMBERS: tuple[GrpcCode, ...] = tuple(GrpcCode)
"""Each status at the number `grpc-status` carries for it: gRPC numbers them in the order above, from 0."""


class GrpcStatus(Model):
    """How a gRPC call ended, as its trailers (or, for an answer that is trailers only, its headers) said."""

    code: GrpcCode
    message: str | None = Field(default=None, description="`grpc-message`, decoded; None when it was empty")


class FrameSender(StrEnum):
    """Who sent a message on a WebSocket connection."""

    AGENT = "agent"
    SERVICE = "service"  # the provider's socket server, answering for the real service


class SocketFrame(Model):
    """One message on a WebSocket connection to a host a provider claims, as it crossed the proxy."""

    connection: str = Field(description="The connection's own id, the same on every message it carried")
    number: int = Field(ge=1, description="This message's number on its connection, from 1, both ways counted")
    sender: FrameSender
    text: bool = Field(description="A text message; False: binary")


class CallFailure(Model):
    """Why Minutehand answered a call in the provider's place: an operation the fake does not implement, or its own
    error, with the exception's type and, for an internal error, its traceback."""

    kind: CallOutcome = Field(description="`NOT_IMPLEMENTED` or `INTERNAL_ERROR`")
    message: str = Field(description="What the agent was answered, as its client reads it")
    exception_type: str = Field(description="The exception's qualified class name")
    traceback: str | None = Field(default=None, description="Set for an internal error")


class InboxAct(StrEnum):
    """What Minutehand did as a person in the agent's own product (`domain.inboxes`)."""

    LIST = "list"  # read what is waiting on the person
    DECIDE = "decide"  # made the person's decision on one item


class InboxCall(Model):
    """A call Minutehand made itself, as a person, to the agent's own product: never one of the agent's."""

    inbox: ProviderKey = Field(description="The inbox's `name`")
    person: str = Field(description="Person.key it acted as")
    act: InboxAct
    contract: str | None = Field(
        default=None,
        description="Set when the answer departed from the agent's own API description (`OperationRequest`): the "
        "agent's contract changed, and this says how, naming the field",
    )


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
    outcome: CallOutcome | None = Field(
        default=None, description="What its answer was; None when nothing classified it (see `CallOutcome`)"
    )
    late_for: str | None = Field(
        default=None,
        description="Set for a call refused into the lobby of `minutehand serve` that carried a claim of a world "
        "already closed: the id of the world it would have belonged to, had it come before that world closed",
    )
    failure: CallFailure | None = Field(
        default=None, description="Set when Minutehand answered in the fake's place: not implemented, or its own error"
    )
    inbox_call: InboxCall | None = Field(
        default=None,
        description="Set for a call Minutehand made as a person to the agent's own product (reading an inbox, "
        "deciding an item): the agent made no such call, and nothing counts it as the agent's",
    )
    grpc: GrpcStatus | None = Field(
        default=None,
        description="Set for a gRPC call: how it ended. `path` is the method (`/package.Service/Method`), `status` "
        "the HTTP status (200 for any call answered in gRPC), and the bodies the request and answer messages as "
        "proto3 JSON when the provider's server read them",
    )
    frame: SocketFrame | None = Field(
        default=None,
        description="Set for one message on a WebSocket connection: `method` and `path` are the upgrade's, `status` "
        "101, and the message is `request_body` (or `request_bytes`) when the agent sent it, `response_body` (or "
        "`response_bytes`) when the service did",
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


class ItemStatus(StrEnum):
    PENDING = "pending"  # waiting on the person
    DECIDED = "decided"  # the person decided it, and the product took the decision
    WITHDRAWN = "withdrawn"  # gone from the person's inbox without their deciding it: the agent took it back


class InboxItemSnapshot(Model):
    """Something waiting on a person in the agent's own product (`domain.inboxes`): an operation to approve, a
    question raised on its own page. Seen by Minutehand reading the person's inbox, it is the agent asking that
    person, as a message is; the person's decision is their answer.

    Written as actor AGENT when first seen (`PENDING`) and when it is gone undecided (`WITHDRAWN`); as actor
    PERSON when they decided (`DECIDED`), or tried to and the product refused (`PENDING`, with `refused`)."""

    kind: Literal["inbox_item"] = "inbox_item"
    inbox: ProviderKey = Field(description="The inbox's `name`")
    item_id: str = Field(description="The product's own id for it")
    person: str | None = Field(description="Person.key it waits on; None when it names nobody in the scenario")
    waits_on: str = Field(description="Who it waits on, as the product named them, or the person whose list held it")
    summary: str = Field(description="What it asks, as the product words it")
    category: str | None = Field(default=None, description="Its kind in the product's own words, when listed")
    decisions: list[str] = Field(description="The decisions the person can make on it, by name")
    gates: str | None = Field(
        default=None, description="The product's id for the operation it holds back, when the inbox says where"
    )
    status: ItemStatus
    decision: str | None = Field(default=None, description="The decision made or tried, by name")
    said: str | None = Field(default=None, description="The decision as the record says it: 'approved'")
    permits: bool | None = Field(
        default=None, description="Whether the decision lets what the item gates go ahead; None when it says nothing"
    )
    inputs: dict[str, str] = Field(default={}, description="What the person gave with the decision, by input name")
    refused: str | None = Field(default=None, description="The product's answer to a decision it did not take")


class FileSnapshot(Model):
    """A file in a watched folder of the agent's machine (`AgentUnderTest.watches`), as it stood after a change."""

    kind: Literal["file"] = "file"
    path: str = Field(description="Absolute")
    size: int = Field(ge=0, description="Bytes, as the file system reports them")


class ToolCallSnapshot(Model):
    """A tool the agent called on an MCP server (`tools/call`), with what the server answered."""

    kind: Literal["tool_call"] = "tool_call"
    server: str = Field(description="The server's host, or the name its relay was started with")
    tool: str
    arguments: str = Field(description="The call's arguments, as JSON text")
    result: str | None = Field(default=None, description="The text of the result's content; None when none came")
    is_error: bool = Field(default=False, description="The server answered an error, or a result marked isError")


class MemorySnapshot(Model):
    """A key of the agent's memory (`minutehand.agent.store`), as the agent wrote or read it. Written by the run's
    receiver as actor AGENT: a write carries the value it left (`value`, None for a delete); a read carries none, and
    a listing names the prefix it listed under in `key` with `listing` set."""

    kind: Literal["memory"] = "memory"
    collection: str
    key: str = Field(description="The key; for a listing, the prefix listed")
    value: str | None = Field(default=None, description="The value written, as canonical JSON text; None otherwise")
    listing: bool = Field(default=False, description="A listing of every key under `key`, not one key")


class NextWakeSnapshot(Model):
    """The moment the agent asked to be woken next (`minutehand.agent.wake`), or none."""

    kind: Literal["next_wake"] = "next_wake"
    at: AwareDatetime | None


Snapshot = Annotated[
    MemorySnapshot
    | NextWakeSnapshot
    | ToolCallSnapshot
    | FileSnapshot
    | TicketSnapshot
    | MessageSnapshot
    | DocumentSnapshot
    | GrantSnapshot
    | RecordSnapshot
    | InteractionSnapshot
    | InboxItemSnapshot,
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
    unopened on a tunnel (`exchange.tunnelled`), made by Minutehand as a person (`exchange.inbox_call`) or, when
    all three are None, refused. `first_seq > last_seq` means the
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
        return (
            self.provider is None
            and self.exchange.captured is None
            and self.exchange.tunnelled is None
            and self.exchange.inbox_call is None
        )
