"""A scenario: the goal, the people, and what already exists in the world.

A scenario holds no absolute dates except `starts_at`. Every other moment is an
offset from it, so the same file replays on any day and under any seed.

A scenario file may leave `starts_at` out (`WrittenScenario`): it then starts at the
moment the run starts, for an agent that reads the real clock. A run resolves it once,
before anything is played, into the `Scenario` it plays and records, so every sample,
fork and rerun of that run starts at the same instant.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    """Every model in this package: frozen, and an unknown field is an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


ProviderKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
"""A provider's registry key ("slack", "asana"). Open, because the set of providers
is whatever is installed; it is checked against the registry when a scenario loads."""


class AbsenceTrigger(StrEnum):
    AT_START = "at_start"
    ON_FIRST_ASK = "on_first_ask"


class Absence(Model):
    """A stretch during which a person does not answer."""

    trigger: AbsenceTrigger = AbsenceTrigger.AT_START
    starts_after: timedelta = timedelta(0)
    lasts: timedelta
    delegate: str | None = Field(default=None, description="Person.key who covers")
    reason: str | None = None


class DelayRange(Model):
    """How long a person takes to answer, in simulated time."""

    shortest: timedelta = timedelta(hours=6)
    longest: timedelta = timedelta(hours=66)

    @model_validator(mode="after")
    def _ordered(self) -> DelayRange:
        if self.longest < self.shortest:
            raise ValueError("longest is shorter than shortest")
        return self


class ScriptedReply(Model):
    """One fixed answer, given to the nth question this person receives."""

    to_ask: int = Field(ge=1)
    text: str


class Helpfulness(StrEnum):
    """What this person does with a question."""

    FULL = "full"  # answers it
    PARTIAL = "partial"  # answers part and leaves the rest
    ASKS_BACK = "asks_back"  # replies with a question of their own
    DECLINES = "declines"  # says it is not theirs, names nobody
    MISTAKEN = "mistaken"  # answers confidently from an out-of-date fact


class Answers(Model):
    """Replies are written by a model from this person's facts.

    The first reply to each ask is stored with the run; a rerun replays it, so
    only a new ask costs a model call.
    """

    kind: Literal["answers"] = "answers"
    delay: DelayRange = DelayRange()
    helpfulness: Helpfulness = Helpfulness.FULL
    voice: str | None = Field(default=None, description="How they write: terse, formal, chatty")
    model: str | None = Field(default=None, description="None uses the run's default model")
    temperature: float = Field(default=0.6, ge=0, le=2)


class Scripted(Model):
    """Replies are fixed text. No model call, fully repeatable."""

    kind: Literal["scripted"] = "scripted"
    delay: DelayRange = DelayRange()
    replies: list[ScriptedReply]


class Silent(Model):
    """This person never answers."""

    kind: Literal["silent"] = "silent"


ReplyBehaviour = Annotated[Answers | Scripted | Silent, Field(discriminator="kind")]


class WorkingHours(Model):
    """Replies land only inside these hours, in this person's own timezone."""

    timezone: str = "UTC"
    opens: time = time(9)
    closes: time = time(17)
    weekdays_only: bool = True


class Person(Model):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    email: str
    title: str | None = None
    facts: list[str] = Field(default=[], description="What this person knows; all a model reply may draw on")
    stale_facts: list[str] = Field(default=[], description="What they believe that is no longer true")
    reply: ReplyBehaviour = Answers()
    working_hours: WorkingHours | None = None
    absences: list[Absence] = []


class TicketState(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class SeededTicket(Model):
    provider: ProviderKey
    project: str
    title: str
    body: str = ""
    assignee: str | None = Field(default=None, description="Person.key")
    state: TicketState = TicketState.OPEN


class SeededDocument(Model):
    provider: ProviderKey
    title: str
    text: str
    folder: str | None = None


class TicketFate(Model):
    """What happens to a ticket the agent hands to a person."""

    assignee: str = Field(description="Person.key")
    becomes: TicketState
    after: timedelta


class Direction(Model):
    """Something the owner says to the agent partway through."""

    text: str
    after: timedelta


class Bound(Model):
    """How many matches are right, and by when."""

    at_least: int = Field(default=1, ge=0)
    at_most: int | None = Field(default=None, ge=0)
    by: timedelta | None = Field(default=None, description="Offset from the scenario's start")

    @model_validator(mode="after")
    def _ordered(self) -> Bound:
        if self.at_most is not None and self.at_most < self.at_least:
            raise ValueError("at_most is below at_least")
        return self


class PersonAsked(Bound):
    """The agent sent this person a message; with `about`, one that asks them about that, as a model judges it."""

    kind: Literal["person_asked"] = "person_asked"
    person: str = Field(description="Person.key")
    mentions: list[str] = Field(default=[], description="Words the message must contain, any case")
    about: str | None = Field(
        default=None,
        description="What the message asks this person about, in meaning rather than words; a model judges it",
    )


class TicketCreated(Bound):
    """The agent filed a ticket."""

    kind: Literal["ticket_created"] = "ticket_created"
    assignee: str | None = Field(default=None, description="Person.key; None matches any")
    mentions: list[str] = Field(default=[], description="Words the title or body must contain")


class TicketDeleted(Bound):
    """The agent deleted a ticket. The default says it must not."""

    kind: Literal["ticket_deleted"] = "ticket_deleted"
    at_least: int = Field(default=0, ge=0)
    at_most: int | None = Field(default=0, ge=0)


class TicketInState(Bound):
    """A ticket assigned to this person reached this state."""

    kind: Literal["ticket_in_state"] = "ticket_in_state"
    assignee: str = Field(description="Person.key")
    state: TicketState


class Relayed(Bound):
    """The agent passed on what a person said: a message from the agent to `to` carries the `tell`, a phrase the
    scenario's author picks from what `said_by` will say, matched in any case.

    The tell is what makes this mechanical rather than a guess at meaning. A scenario is refused when its goal, a
    direction, a seeded ticket or document, or anyone else's scripted reply or facts holds the tell, and when
    `said_by` could never say it: silent, or none of their scripted replies (or, written by a model, none of their
    facts) holds it. In a run, the first thing in the world to hold the tell must be `said_by`'s own reply; an
    agent message that held it before means the agent did not hear it from them, and no message counts."""

    kind: Literal["relayed"] = "relayed"
    said_by: str = Field(description="Person.key of whoever says the tell")
    to: str = Field(description="Person.key the agent must pass it on to")
    tell: str = Field(min_length=1, description="A phrase only `said_by`'s answer holds")


Expectation = Annotated[
    PersonAsked | TicketCreated | TicketDeleted | TicketInState | Relayed, Field(discriminator="kind")
]


class _ScenarioBody(Model):
    """Everything a scenario says but when it starts."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    goal: str = Field(description="The text handed to the agent, verbatim")
    owner: str = Field(description="Person.key of whoever gave the goal")
    deadline_after: timedelta | None = None
    max_wakes: int = Field(default=20, ge=1)
    seed: int = 17
    protected_names: list[str] = Field(default=[], description="Names the agent must spell exactly as given")
    people: list[Person]
    tickets: list[SeededTicket] = []
    documents: list[SeededDocument] = []
    ticket_fates: list[TicketFate] = []
    directions: list[Direction] = []
    expect: list[Expectation] = Field(default=[], description="What must be true of the world for this run to be right")

    @model_validator(mode="after")
    def _keys_resolve(self) -> Self:
        keys = [p.key for p in self.people]
        if len(keys) != len(set(keys)):
            raise ValueError("two people share a key")
        known = set(keys)
        named = [self.owner]
        named += [t.assignee for t in self.tickets if t.assignee]
        named += [f.assignee for f in self.ticket_fates]
        named += [a.delegate for p in self.people for a in p.absences if a.delegate]
        named += [e.person for e in self.expect if isinstance(e, PersonAsked)]
        named += [e.assignee for e in self.expect if isinstance(e, (TicketCreated, TicketInState)) and e.assignee]
        named += [k for e in self.expect if isinstance(e, Relayed) for k in (e.said_by, e.to)]
        missing = sorted(set(named) - known)
        if missing:
            raise ValueError(f"no such person: {', '.join(missing)}")
        for relayed in (e for e in self.expect if isinstance(e, Relayed)):
            self._refuse_tell(relayed)
        return self

    def _refuse_tell(self, relayed: Relayed) -> None:
        """A tell the agent could write without hearing it from `said_by`, or that `said_by` can never say."""
        tell = relayed.tell.casefold()
        if relayed.said_by == relayed.to:
            raise ValueError(f"a relayed tell goes from one person to another; {relayed.said_by} is both")
        elsewhere = [("the goal", self.goal)]
        elsewhere += [(f"direction {i + 1}", d.text) for i, d in enumerate(self.directions)]
        elsewhere += [(f"seeded ticket {t.title!r}", f"{t.title} {t.body}") for t in self.tickets]
        elsewhere += [(f"seeded document {d.title!r}", f"{d.title} {d.text}") for d in self.documents]
        for person in self.people:
            if person.key == relayed.said_by:
                continue
            said = [r.text for r in person.reply.replies] if isinstance(person.reply, Scripted) else []
            elsewhere += [(f"what {person.key} says or knows", text) for text in [*said, *person.facts]]
        for where, text in elsewhere:
            if tell in text.casefold():
                raise ValueError(
                    f"the tell {relayed.tell!r} appears in {where}, so the agent could write it without hearing "
                    f"it from {relayed.said_by}"
                )
        speaker = next(p for p in self.people if p.key == relayed.said_by)
        reply = speaker.reply
        if isinstance(reply, Silent):
            raise ValueError(f"{relayed.said_by} is silent and can never say the tell {relayed.tell!r}")
        own = [r.text for r in reply.replies] if isinstance(reply, Scripted) else speaker.facts
        if not any(tell in text.casefold() for text in own):
            source = "scripted reply" if isinstance(reply, Scripted) else "fact"
            raise ValueError(f"no {source} of {relayed.said_by} holds the tell {relayed.tell!r}")


class WrittenScenario(_ScenarioBody):
    """A scenario as its file states it. With no `starts_at` it starts at the moment the run does."""

    starts_at: AwareDatetime | None = Field(default=None, description="None: the moment the run starts")

    def starting(self, now: datetime) -> Scenario:
        """The scenario a run plays: its own `starts_at`, or `now`, the moment the run starts, when it has none."""
        return Scenario.model_validate({**self.model_dump(), "starts_at": self.starts_at or now})


class Scenario(_ScenarioBody):
    """A scenario as a run plays it and records it: its start is an instant."""

    starts_at: AwareDatetime = Field(description="Simulated; every other moment is an offset from it")

    def starting(self, now: datetime) -> Scenario:
        """Itself: its start is already an instant."""
        return self

    @property
    def deadline(self) -> datetime | None:
        return self.starts_at + self.deadline_after if self.deadline_after else None
