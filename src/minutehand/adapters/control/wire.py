"""The control API's requests and answers (`minutehand serve`, under `/v1`). Every one is a `Model`: frozen,
and an unknown field is refused, so a client that misspells a field is told, never silently ignored.

`minutehand.testing.MinutehandClient` reads and writes these same models.
"""

from __future__ import annotations

import json
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from minutehand.checks.runner import RunResult
from minutehand.domain.outbound import OutboundHost, refuse_repeats
from minutehand.domain.people import InboundCredential, InboundCredentialAsk, InboundTarget, PermissionGrant, Press
from minutehand.domain.provider import PersonChange
from minutehand.domain.scenario import (
    Happening,
    Model,
    Person,
    ProviderKey,
    ProviderSeed,
    Seed,
    SeededChannel,
    SeededDocument,
    SeededTicket,
    SharedSpace,
    SignIn,
    TicketState,
)
from minutehand.domain.telemetry import StoredSpan
from minutehand.domain.world import EntityRef, RecordedCall, Stored, WorldEvent

API = "/v1"


class Claims(Model):
    """How an intercepted call is known to be this world's. Each token and host belongs to one open world.

    A call is this world's when it went to one of `hosts` (exact, e.g. a self-hosted tracker's
    `acme.youtrack.cloud`), or carries one of `tokens`: a bearer token, a Basic password, an OAuth
    `access_token` or `refresh_token`, or a signed JWT assertion's issuer or subject (a service account).
    An access token a token endpoint mints for a call of this world is claimed by it from then on.
    A call is also this world's when its URL names one of `keys` where its provider's manifest says a URL names
    a world (`Manifest.world_keys`): a Microsoft tenant id or domain in a sign-in path, the label of a SharePoint,
    Atlassian or YouTrack host, an Atlassian cloud id in `/ex/jira/{cloudId}/`. Those calls carry no credential
    (OpenID metadata, key sets, `authorize`, a pre-authenticated download), and without a key a second tenant's
    would reach the first's world.
    `default` makes this world the one every call no world claims goes to; at most one open world is the
    default, and while it is, it shares the stack with no isolation for those calls."""

    tokens: list[str] = []
    hosts: list[str] = []
    keys: list[str] = []
    default: bool = False

    @model_validator(mode="after")
    def _claims_something(self) -> Self:
        if not self.tokens and not self.hosts and not self.keys and not self.default:
            raise ValueError("a world claims no call: give tokens, hosts, keys, or default: true")
        return self


class Inbound(Model):
    """Where a provider pushes events for this world (Slack's Events API URL), and the secret the receiving
    service verifies them with. None signs with a secret made for the world and given to no one."""

    provider: ProviderKey
    url: str
    interactivity_url: str | None = Field(
        default=None, description="Where a person's use of a control or a form is pushed; None: the same as `url`"
    )
    secret: str | None = None

    def to_target(self) -> InboundTarget:
        """The target as a provider pushes to it; the secret travels beside it, never in it."""
        return InboundTarget(provider=self.provider, url=self.url, interactivity_url=self.interactivity_url)


class Fault(Model):
    """The next `times` calls of this world to `provider` that match `method` and start with `path` are
    answered with `status` and `body` instead of reaching the provider, and recorded like any call.
    The body is the caller's: no provider renders its own error shape for a fault yet."""

    provider: ProviderKey
    method: str | None = Field(default=None, description="None matches any method")
    path: str = Field(default="/", description="A prefix of the path the real API is called at")
    status: int = Field(ge=400, le=599)
    body: str = ""
    content_type: str = "application/json"
    retry_after: int | None = Field(default=None, ge=0, description="Seconds, sent as Retry-After")
    times: int = Field(default=1, ge=1)


class DeclareFaults(Model):
    """Faults typed by one provider, given as a fragment of that provider's own seed model that sets only the
    fields declaring them (Slack's `faults`, Asana's `rate_limits`, Microsoft's `faults` and `holds`, ...): the
    provider validates it and records each as seeding does, its offsets counted from the world's now."""

    provider: ProviderKey
    seed: str = Field(description="The fragment, as JSON text; written as structure, it is kept as its text")

    @field_validator("seed", mode="before")
    @classmethod
    def _as_text(cls, value: object) -> object:
        return json.dumps(value) if isinstance(value, dict) else value


class ModelHost(Model):
    """A model API this world's services call besides api.openai.com, api.anthropic.com and
    generativelanguage.googleapis.com (a self-hosted model, a gateway in front of one). A host is decided before
    its call is opened, so each belongs to one open world: its calls are tunnelled, never decrypted, or, with
    `record`, opened, sent on unchanged and kept as spans in this world."""

    host: str = Field(description="An exact lower-case host, or `*.` and a domain")
    record: bool = Field(default=False, description="Open its calls and keep each as a span in this world")


class CreateWorld(Model):
    seed: Seed
    claims: Claims
    inbound: list[Inbound] = []
    scripted_people: bool = Field(
        default=False,
        description="True: the seed's scripted people answer, fates land and directions are said as the clock "
        "passes each. False: nobody speaks but the caller.",
    )
    faults: list[Fault] = []
    outbound: list[OutboundHost] = Field(
        default=[],
        description="Hosts no provider claims that this world captures (acknowledge, pass_through, replay), as an "
        "agent file declares them; a call reaches them only once it is this world's, by its claims",
    )

    model_hosts: list[ModelHost] = Field(
        default=[],
        description="Model APIs this world declares, each tunnelled or recorded as it says; a host belongs to one "
        "open world, and one a provider claims or this world captures is refused",
    )

    @model_validator(mode="after")
    def _one_declaration_per_host(self) -> Self:
        refuse_repeats(self.outbound)
        hosts = [m.host for m in self.model_hosts]
        repeated = sorted({h for h in hosts if hosts.count(h) > 1})
        if repeated:
            raise ValueError(f"a model host is declared once: {', '.join(repeated)}")
        return self


class OwedView(Model):
    at: AwareDatetime
    what: str


class WorldView(Model):
    world_id: str = Field(description="Also the run id: `minutehand findings`, `view` and the MCP tools read it")
    name: str
    open: bool
    claims: Claims
    now: AwareDatetime = Field(description="The world's clock, simulated")
    head: int = Field(description="The latest WorldEvent.seq")
    owed: list[OwedView] = Field(default=[], description="What falls due as the clock moves")
    resets: int = Field(default=0, ge=0, description="How many times it was reset; its record from before each is kept")


class WorldList(Model):
    worlds: list[WorldView]


class EventsPage(Model):
    """The log, filtered. Each stretch between resets numbers its events from 1, so across resets a `seq` is told
    apart by the stretch `resets` puts it in; `since` and `head` are the current stretch's."""

    events: list[WorldEvent]
    head: int
    resets: list[int] = Field(
        default=[],
        description="Read across resets (`since_reset=false`): for each reset, oldest first, the index in this list "
        "of the first item after it. Empty when read since the last reset, the default",
    )


class EntitiesPage(Model):
    entities: list[Stored]


class CallsPage(Model):
    calls: list[RecordedCall]
    resets: list[int] = Field(
        default=[],
        description="Read across resets (`since_reset=false`): for each reset, oldest first, the index in this list "
        "of the first item after it. Empty when read since the last reset, the default",
    )


class SpansPage(Model):
    spans: list[StoredSpan]
    resets: list[int] = Field(
        default=[],
        description="Read across resets (`since_reset=false`): for each reset, oldest first, the index in this list "
        "of the first item after it. Empty when read since the last reset, the default",
    )


class LobbyKind(StrEnum):
    """What a call kept in the lobby is. Only `unclaimed` is a call nobody expected: the others are traffic the
    server was told about, kept so the record is complete."""

    UNCLAIMED = "unclaimed"
    """No open world claimed it and nothing declared its host: refused with 502 (a late call for a closed world among
    them, `Exchange.late_for`). The lobby a suite asserts empty."""
    MODEL_HOST = "model_host"
    """A burst on a tunnel to a host the server was told is a model host (a default one, or `serve --model-host`),
    relayed unopened (`Exchange.tunnelled`), when no world declared that host."""
    PASS_THROUGH = "pass_through"
    """A call to a host no provider claims, passed through and kept because the server was told to
    (`serve --capture-unknown`, `Exchange.captured`)."""


def lobby_kind(call: RecordedCall) -> LobbyKind:
    """What a call the lobby kept is, read from how it was answered."""
    if call.exchange.tunnelled is not None:
        return LobbyKind.MODEL_HOST
    if call.exchange.captured is not None:
        return LobbyKind.PASS_THROUGH
    return LobbyKind.UNCLAIMED


class Unmatched(Model):
    """Calls kept in the lobby, oldest first, of the kinds asked for (`?kind=`, repeated; by default only
    `unclaimed`: calls no open world claimed and nothing declared, refused with 502). `kinds` counts every call the
    lobby kept since `since`, by kind, whichever were asked for, so traffic left out of `calls` is never out of
    sight. `since` and `head` count every call the lobby kept across the life of the server (a call to a provider's
    shared host, answered there and listed under no kind, among them), so `since` reads only what is new."""

    calls: list[RecordedCall]
    head: int
    kinds: dict[LobbyKind, int] = Field(
        default={}, description="How many calls of each kind the lobby kept since `since`, listed or not"
    )


class Say(Model):
    """A person messages the agent directly: in Slack, a DM to its bot."""

    kind: Literal["say"] = "say"
    person: str
    text: str
    provider: ProviderKey = "slack"


class Reply(Model):
    """A person answers a message: in its thread in a channel, as a new message in a DM."""

    kind: Literal["reply"] = "reply"
    person: str
    text: str
    to: EntityRef


class MoveTicket(Model):
    """The ticket's assignee completes, cancels or reopens it, as actor PERSON."""

    kind: Literal["move_ticket"] = "move_ticket"
    ticket: EntityRef
    to: TicketState


class EditTicket(Model):
    """The ticket is rewritten from outside the agent (reassigned, its state set), as actor SCENARIO."""

    kind: Literal["edit_ticket"] = "edit_ticket"
    ticket: EntityRef
    state: TicketState | None = None
    assignee: str | None = Field(default=None, description="Person.key")


class Happen(Model):
    """A person does something by themselves now: any happening a scenario can schedule (a ticket's, a document's,
    or a messaging one), landed the way the world lands one when its clock passes it. Its `after` is not read."""

    kind: Literal["happen"] = "happen"
    happening: Happening


class PressControl(Model):
    """A person uses a control on a message now: presses a button, picks someone, fills and submits a form."""

    kind: Literal["press"] = "press"
    person: str
    on: EntityRef = Field(description="The message the control is on")
    press: Press


class DeleteTicket(Model):
    """A person deletes a ticket now: one the agent filed, or one seeded; its assignee, or the owner when unassigned.
    Afterwards the service answers for it as for a ticket that never was."""

    kind: Literal["delete_ticket"] = "delete_ticket"
    ticket: EntityRef


Act = Annotated[
    Say | Reply | MoveTicket | EditTicket | Happen | PressControl | DeleteTicket, Field(discriminator="kind")
]


class ActRequest(Model):
    act: Act


class Acted(Model):
    event: WorldEvent = Field(description="The first change the act wrote")


class Advance(Model):
    """Move the clock forward, by `by` or to `to`; exactly one."""

    by: timedelta | None = Field(default=None, gt=timedelta(0))
    to: AwareDatetime | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.by is None) == (self.to is None):
            raise ValueError("give exactly one of by and to")
        return self


class FiredView(Model):
    at: AwareDatetime
    what: str
    events: list[int]


class Advanced(Model):
    now: AwareDatetime
    fired: list[FiredView]


QUIET_FOR = timedelta(milliseconds=250)
"""How long a world must have had no call before closing it counts it quiet, unless the close says otherwise."""
QUIET_AT_MOST = timedelta(seconds=5)
"""How long closing a world waits for it to go quiet, unless the close says otherwise."""


class Quiet(Model):
    """Wait until no call routed to this world has been seen for `quiet_for`, and no delivery from Minutehand to
    the service (an event pushed, a webhook sent) still awaits the service's answer; give up after `at_most`."""

    quiet_for: timedelta = Field(default=QUIET_FOR, ge=timedelta(0))
    at_most: timedelta = Field(default=QUIET_AT_MOST, ge=timedelta(0))


class Quieted(Model):
    """Whether the world went quiet before the wait gave up, and what was still going on when it stopped."""

    quiet: bool
    waited: timedelta = Field(description="Real time spent waiting")
    busy: list[str] = Field(
        default=[], description="When not quiet: each call still in progress and each delivery still awaited"
    )
    last_call: str | None = Field(default=None, description="The latest call routed to the world, for a person")


class Checked(Model):
    result: RunResult
    quiet: Quieted | None = Field(
        default=None, description="On a close: how waiting for the world to go quiet ended; None when not waited for"
    )


class Environment(Model):
    """What a service needs to reach the fakes through this server: the variables, by name."""

    variables: dict[str, str]


class RefusalKind(StrEnum):
    UNSUPPORTED = "unsupported"
    """The provider cannot do what was asked at all, in any world: a capability it does not have."""


class RefusalCode(StrEnum):
    """What went wrong, for a machine: stable, one per answer the control API gives in place of what was asked."""

    INVALID = "invalid"  # 422: a body that is not the model, a query parameter that is not what its route takes
    NOT_FOUND = "not_found"  # 404: a world that is not open, or something it does not hold
    REFUSED = "refused"  # 409: what the world cannot do
    UNSUPPORTED = "unsupported"  # 409: what the provider cannot do in any world
    AGENT_REFUSED = "agent_refused"  # 502: the service an event was pushed to refused it
    ENVIRONMENT = "environment"  # 503: the machine failed (a port, a process, a host)
    INTERNAL_ERROR = "internal_error"  # 500: Minutehand's own error; never the request's fault


class Refusal(Model):
    """Every answer the control API gives in place of what was asked, whatever the route: `code` for a machine,
    `error` for a person."""

    error: str
    kind: RefusalKind | None = Field(default=None, description="Set when the refusal is of a known kind")
    code: RefusalCode = Field(default=RefusalCode.REFUSED, description="What went wrong, stable for a machine")


class FurtherSeed(Model):
    """More of what a world is seeded with, landed on a world already open: written with the same models and the
    same seeding the world opened with, as actor SCENARIO at the world's now. Refused, with nothing written, when
    it contradicts what is there (a key or title already taken, a seeded thing changed since, an id in use). A
    provider fragment is merged into the provider's own seed: lists grow, and a value it sets must agree."""

    people: list[Person] = []
    tickets: list[SeededTicket] = []
    documents: list[SeededDocument] = []
    spaces: list[SharedSpace] = []
    sign_ins: list[SignIn] = []
    channels: list[SeededChannel] = []
    provider_seeds: list[ProviderSeed] = []

    @model_validator(mode="after")
    def _adds_something(self) -> Self:
        if not any(
            (self.people, self.tickets, self.documents, self.spaces, self.sign_ins, self.channels, self.provider_seeds)
        ):
            raise ValueError("a further seed adds nothing")
        return self


class Seeded(Model):
    """What a further seed gave each provider the world already held, by count of things written."""

    view: WorldView
    written: dict[ProviderKey, int]


class ChangePerson(Model):
    """Something happens to a person's account in one provider now, as an administrator would do it."""

    provider: ProviderKey
    person: str = Field(description="Person.key")
    change: PersonChange


class Permit(Model):
    """A named permission granted or withheld for a person on a project now, in a provider that names them."""

    provider: ProviderKey
    grant: PermissionGrant


class MintInbound(Model):
    """Sign or mint, for a request a test builds itself, what the provider's service would send with it."""

    provider: ProviderKey
    ask: InboundCredentialAsk


class Minted(Model):
    credential: InboundCredential


class ProviderView(Model):
    """What one installed provider can be asked to do in a world already open."""

    key: ProviderKey
    people_changes: list[PersonChange]
    permissions: bool = Field(description="Grants and withholds named permissions (`POST /permissions`)")
    inbound_credentials: bool = Field(description="Signs a request a test builds (`POST /inbound-credential`)")
    faults: bool = Field(description="Declares its own typed faults (`POST /provider-faults`)")
    deletes_tickets: bool = Field(description="A person can delete its tickets (`act` `delete_ticket`, fates)")
    seed_model: bool = Field(description="Has a seed model of its own (`provider_seeds`)")


class ProvidersView(Model):
    providers: list[ProviderView]


class RawEntity(Model):
    """One entity, every version of it the log holds, oldest first; `deleted` when its latest change removed it."""

    entity: EntityRef
    deleted: bool
    versions: list[Stored]


class RawState(Model):
    """Everything a world holds of one provider, as the provider keeps it: for a person debugging, not for a test
    to assert on. Its shape is the provider's own and changes with it."""

    provider: ProviderKey
    entities: list[RawEntity]
