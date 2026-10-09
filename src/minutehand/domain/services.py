"""Declared services (`docs/services.md`, `docs/design-transitions.md`): a host no provider fakes that the agent files
things with, whose items follow a state machine Minutehand holds, and through which the scenario's people respond.

    services:
      - host: api.approvals.example
        responders: [nadia, marta]
        within: {min: PT2H, max: P2D}
        describe: Purchase approvals; an approver approves, rejects, or asks for the cost centre.

The agent never sees a person act: it sees what the service shows it afterwards, in answers to its calls and in
calls the service pushes to it. Minutehand holds each item's state and moves it only along the machine's
transitions; a model renders what the agent is answered from that state and the service's log, and writes what a
person responds.
"""

from __future__ import annotations

import re
from datetime import timedelta
from enum import StrEnum
from typing import Self

from pydantic import ConfigDict, Field, JsonValue, model_validator

from minutehand.domain.common import ProviderKey, Window
from minutehand.domain.model import Model
from minutehand.domain.outbound import Collection, IdFormat, KeyPath

ROUTE = re.compile(r"^(?P<method>[A-Z]+) (?P<path>/[^\s?#]*)$")
"""A route as written: a method and a path template, `GET /v1/requests/{id}`."""

STATE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")


class Trigger(StrEnum):
    """Who moves an item along a transition, as a machine declares it; recorded as the `Actor` of the same name."""

    AGENT = "agent"  # a call of the agent's that the route means as this transition
    PERSON = "person"  # a responder, at a moment drawn in their available time
    TIMER = "timer"  # the service itself, `after` (or within `within`) the item entered the state
    SYSTEM = "system"  # as a timer, done by a named actor of the service's: a warehouse, a payment processor


class MachineTransition(Model):
    """One way an item moves: from any of `from` to `to`, by `by`."""

    model_config = ConfigDict(frozen=True, extra="forbid", serialize_by_alias=True, validate_by_name=True)

    name: str = Field(pattern=STATE.pattern)
    from_: list[str] = Field(alias="from", min_length=1, description="The states it leaves")
    to: str = Field(pattern=STATE.pattern)
    by: Trigger
    actor: str | None = Field(default=None, description="Who does it, for `by: system`: `warehouse`")
    after: timedelta | None = Field(
        default=None, gt=timedelta(0), description="For a timer or system: this long after the item entered the state"
    )
    within: Window | None = Field(
        default=None, description="For a timer or system instead of `after`: a moment drawn in this calendar span"
    )
    requires: list[KeyPath] = Field(
        default=[], description="Fields the agent's call, or the person's content, must carry for it to be taken"
    )

    @model_validator(mode="after")
    def _when_it_fires(self) -> Self:
        timed = self.by in (Trigger.TIMER, Trigger.SYSTEM)
        if timed and (self.after is None) == (self.within is None):
            raise ValueError(f"transition {self.name}: a {self.by.value} transition takes `after` or `within`")
        if not timed and (self.after is not None or self.within is not None):
            raise ValueError(f"transition {self.name}: only a timer or system transition takes `after` or `within`")
        if (self.by is Trigger.SYSTEM) != (self.actor is not None):
            raise ValueError(f"transition {self.name}: `actor` names who does a system transition, and only that")
        return self


class Machine(Model):
    """The states an item of the service can be in and the transitions between them, fixed for a run."""

    initial: str = Field(pattern=STATE.pattern, description="The state an item is created in")
    states: list[str] = Field(min_length=1)
    transitions: list[MachineTransition] = []

    @model_validator(mode="after")
    def _names_resolve(self) -> Self:
        for state in self.states:
            if not STATE.match(state):
                raise ValueError(f"state {state!r} is not a name: letters, digits, '_' and '-'")
        if len(set(self.states)) != len(self.states):
            raise ValueError("a state is listed twice")
        names = [t.name for t in self.transitions]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ValueError(f"transition {', '.join(twice)} is declared twice")
        known = set(self.states)
        named = [self.initial, *(s for t in self.transitions for s in [*t.from_, t.to])]
        unknown = sorted(set(named) - known)
        if unknown:
            raise ValueError(f"no such state: {', '.join(unknown)}; the states are {', '.join(self.states)}")
        return self

    def transition(self, name: str) -> MachineTransition | None:
        return next((t for t in self.transitions if t.name == name), None)

    def taken_by(self, by: Trigger) -> list[MachineTransition]:
        return [t for t in self.transitions if t.by is by]

    def legal(self, state: str, by: Trigger) -> list[MachineTransition]:
        """The transitions `by` may take from `state`, in the order declared."""
        return [t for t in self.transitions if t.by is by and state in t.from_]


class Meaning(StrEnum):
    """What a call to a route does to the service."""

    CREATE = "create"  # files a new item, in the machine's initial state
    TRANSITION = "transition"  # asks for one transition of the item it names
    UPDATE = "update"  # changes what an item says, not its state
    READ = "read"  # changes nothing
    SUBSCRIBE = "subscribe"  # registers an address the service pushes to
    OTHER = "other"  # changes nothing the machine holds


class RouteMeaning(Model):
    """What one route of a service means, and where its answers show an item, kept once per run: declared, read
    from the service's OpenAPI document by the model, or the model's reading of the first call to it."""

    route: str = Field(pattern=ROUTE.pattern, description="`GET /v1/requests/{id}`")
    means: Meaning
    transition: str | None = Field(default=None, description="For `transition`: the machine's transition, by name")
    item_at: str | None = Field(
        default=None, description="Where an answer shows an item's id: `id`, `items[*].id`; None: it shows none"
    )
    state_at: str | None = Field(
        default=None, description="Where an answer shows that item's state, beside its id: `status`, `items[*].status`"
    )
    url_at: str | None = Field(default=None, description="For `subscribe`: where the call's body holds the address")
    shape: JsonValue = Field(
        default=None, description="The JSON Schema every answer to it conforms to; None until its first answer"
    )

    @model_validator(mode="after")
    def _fits(self) -> Self:
        if (self.means is Meaning.TRANSITION) != (self.transition is not None):
            raise ValueError(f"route {self.route}: `transition` names the transition a `transition` route asks for")
        if self.state_at is not None and self.item_at is None:
            raise ValueError(f"route {self.route}: `state_at` is read beside `item_at`; name both")
        return self

    @property
    def method(self) -> str:
        found = ROUTE.match(self.route)
        assert found is not None
        return found["method"]

    @property
    def path(self) -> str:
        found = ROUTE.match(self.route)
        assert found is not None
        return found["path"]


class ServiceRoute(Model):
    """A route pinned in the scenario, as a run showed it: what it means, and the shape of its answers."""

    route: str = Field(pattern=ROUTE.pattern)
    means: Meaning | None = Field(default=None, description="None: read from the OpenAPI document or by the model")
    transition: str | None = None
    item_at: str | None = None
    state_at: str | None = None
    url_at: str | None = None
    shape: JsonValue = Field(default=None, description="The JSON Schema of its answers; None: fixed by the first")


class ItemIds(Model):
    """How the service names an item it creates: a UUID, or a prefix and a number."""

    format: IdFormat = IdFormat.UUID
    prefix: str = ""

    @model_validator(mode="after")
    def _made_here(self) -> Self:
        if self.format not in (IdFormat.UUID, IdFormat.PREFIXED, IdFormat.INTEGER):
            raise ValueError("a service makes its items' ids: `uuid`, `prefixed` or `integer`")
        if self.prefix and self.format is not IdFormat.PREFIXED:
            raise ValueError("an id's `prefix` goes with `format: prefixed`")
        return self


class Service(Model):
    """A service the agent files things with and the scenario's people respond through."""

    host: str = Field(description="An exact lower-case host")
    name: ProviderKey | None = Field(default=None, description="What its events are recorded under; from the host")
    responders: list[str] = Field(
        description="People who respond, by key: those the item names (by email or key) when it names any, else any; "
        "one is drawn per response. Empty: nobody ever responds"
    )
    within: Window = Field(description="When a response comes: of the responder's available time after it is due")
    describe: str | None = Field(default=None, description="What the service is, in plain language, for the model")
    bias: str | None = Field(default=None, description="How its responders tend to respond, in plain language")
    odds: dict[str, float] = Field(
        default={}, description="Person transitions by name and their weights: drawn first, seeded, then worded"
    )
    openapi: str | None = Field(default=None, description="The service's OpenAPI document: a file or an http(s) URL")
    ids: ItemIds = ItemIds()
    collections: list[Collection] = Field(
        default=[], description="Routes answered exactly as the `store` kind answers them: what the agent wrote"
    )
    machine: Machine | None = Field(default=None, description="None: derived from `openapi`, or proposed, once")
    routes: list[ServiceRoute] = []

    @model_validator(mode="after")
    def _fits(self) -> Self:
        if not re.fullmatch(r"[a-z0-9.\-:\[\]]+", self.host):
            raise ValueError(f"service host {self.host!r} is not an exact lower-case host: a name or an address")
        if len(set(self.responders)) != len(self.responders):
            raise ValueError(f"service {self.key}: a responder is listed twice")
        for weight in self.odds.values():
            if weight < 0:
                raise ValueError(f"service {self.key}: odds are weights of zero or more")
        if self.odds and sum(self.odds.values()) <= 0:
            raise ValueError(f"service {self.key}: odds that are all zero leave nothing to draw")
        if self.machine is not None:
            unknown = sorted(set(self.odds) - {t.name for t in self.machine.taken_by(Trigger.PERSON)})
            if unknown:
                raise ValueError(f"service {self.key}: odds name {', '.join(unknown)}, which no person may take")
            for route in self.routes:
                if route.transition is not None and self.machine.transition(route.transition) is None:
                    raise ValueError(f"service {self.key}: route {route.route} names no transition of the machine")
        routes = [r.route for r in self.routes]
        twice = sorted({r for r in routes if routes.count(r) > 1})
        if twice:
            raise ValueError(f"service {self.key}: route {', '.join(twice)} is pinned twice")
        return self

    @property
    def key(self) -> ProviderKey:
        if self.name is not None:
            return self.name
        spelled = "".join(c if c.isalnum() else "_" for c in self.host)
        return spelled if spelled[:1].isalpha() else "h_" + spelled


def refuse_unknown_responders(services: list[Service], people: list[str]) -> None:
    for service in services:
        unknown = sorted(set(service.responders) - set(people))
        if unknown:
            raise ValueError(f"service {service.key}: no such person: {', '.join(unknown)}")
    hosts = [s.host for s in services]
    keys = [s.key for s in services]
    for label, values in (("host", hosts), ("name", keys)):
        twice = sorted({v for v in values if values.count(v) > 1})
        if twice:
            raise ValueError(f"service {label} declared twice: {', '.join(twice)}")
