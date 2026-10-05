"""The control API's requests and answers (`minutehand serve`, under `/v1`). Every one is a `Model`: frozen,
and an unknown field is refused, so a client that misspells a field is told, never silently ignored.

`minutehand.testing.MinutehandClient` reads and writes these same models.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from minutehand.checks.runner import RunResult
from minutehand.domain.people import InboundTarget
from minutehand.domain.scenario import Model, ProviderKey, Seed, TicketState
from minutehand.domain.telemetry import StoredSpan
from minutehand.domain.world import EntityRef, RecordedCall, Stored, WorldEvent

API = "/v1"


class Claims(Model):
    """How an intercepted call is known to be this world's. Each token and host belongs to one open world.

    A call is this world's when it went to one of `hosts` (exact, e.g. a self-hosted tracker's
    `acme.youtrack.cloud`), or carries one of `tokens`: a bearer token, a Basic password, an OAuth
    `access_token` or `refresh_token`, or a signed JWT assertion's issuer or subject (a service account).
    An access token a token endpoint mints for a call of this world is claimed by it from then on.
    `default` makes this world the one every call no world claims goes to; at most one open world is the
    default, and while it is, it shares the stack with no isolation for those calls."""

    tokens: list[str] = []
    hosts: list[str] = []
    default: bool = False

    @model_validator(mode="after")
    def _claims_something(self) -> Self:
        if not self.tokens and not self.hosts and not self.default:
            raise ValueError("a world claims no call: give tokens, hosts, or default: true")
        return self


class Inbound(Model):
    """Where a provider pushes events for this world (Slack's Events API URL), and the secret the receiving
    service verifies them with. None signs with a secret made for the world and given to no one."""

    provider: ProviderKey
    url: str
    secret: str | None = None

    def to_target(self) -> InboundTarget:
        """The target as a provider pushes to it; the secret travels beside it, never in it."""
        return InboundTarget(provider=self.provider, url=self.url)


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


class WorldList(Model):
    worlds: list[WorldView]


class EventsPage(Model):
    events: list[WorldEvent]
    head: int


class EntitiesPage(Model):
    entities: list[Stored]


class CallsPage(Model):
    calls: list[RecordedCall]


class SpansPage(Model):
    spans: list[StoredSpan]


class Unmatched(Model):
    """Calls no open world claimed, refused with 502, oldest first. `position` counts from 1 across the life
    of the server, so `since` reads only what is new."""

    calls: list[RecordedCall]
    head: int


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


Act = Annotated[Say | Reply | MoveTicket | EditTicket, Field(discriminator="kind")]


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


class Checked(Model):
    result: RunResult


class Environment(Model):
    """What a service needs to reach the fakes through this server: the variables, by name."""

    variables: dict[str, str]


class Refusal(Model):
    error: str
