"""Assessments: how a team judges its agent, written by the team in YAML over the facts of a run.

Minutehand holds no opinion of how an agent should behave. It records what happened (who was asked what and when,
what the agent wrote and to whom, when each answer landed, what the agent planned and reported) and a run is judged
only by the rules its own files declare: `assess:` in the agent file and in the scenario (`docs/assessments.md`).
A run whose files declare none is reported as facts, and its verdict says nothing was assessed.

A rule reads as one sentence: for each of something (the run, every ask, every hand-off, every person), when a
condition holds, the number of some facts between two moments is within bounds.

    - id: follows_up_within_an_hour_of_each_day
      each: ask
      where: {person_not: [owner]}
      at: [ask+P1D, ask+P2D]
      when: {open_at: moment}
      count: {follow_ups: {}, since: moment, until: moment+PT1H}
      at_least: 1
      message: "{person.key} was not followed up within an hour of {rule.moment}"

Moments are an anchor and an optional ISO 8601 offset (`ask+P1D`, `deadline-PT2H`, `answer`). Placeholders in
`message` and `holding` are `{namespace.name}` (`domain/templates.py`): `{person.key}`, `{person.name}`,
`{ask.at}`, `{ask.answer}`, `{rule.id}`, `{rule.count}`, `{rule.moment}`.
"""

from __future__ import annotations

import re
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, Field, TypeAdapter, ValidationError, model_validator

from minutehand.domain.model import Model
from minutehand.domain.templates import refuse_unknown


class Each(StrEnum):
    """What a rule is read once for."""

    RUN = "run"  # once for the whole run
    ASK = "ask"  # every wait for a person's answer: a message they can answer, or an item in the agent's product
    HANDOFF = "handoff"  # every ticket the agent handed to a person
    PERSON = "person"  # every person in the scenario


class Anchor(StrEnum):
    """Where a moment is measured from."""

    START = "start"  # the scenario's start
    DEADLINE = "deadline"  # the scenario's deadline; a rule naming it is not read in a scenario without one
    END = "end"  # the last moment the run reached
    ASK = "ask"  # when the ask or hand-off was made
    ANSWER = "answer"  # when it was answered or the work finished; not read for one never settled
    CLOSED = "closed"  # when it was answered, or the run's end when it never was
    DUE = "due"  # when the scenario says the person would have answered by: their longest delay, or a ticket's fate
    MOMENT = "moment"  # each of the rule's own `at`
    ALL_ANSWERED = "all_answered"  # when the last ask of the run was answered; not read while any is unanswered


SCOPED = {
    Anchor.ASK: (Each.ASK, Each.HANDOFF),
    Anchor.ANSWER: (Each.ASK, Each.HANDOFF),
    Anchor.CLOSED: (Each.ASK, Each.HANDOFF),
    Anchor.DUE: (Each.ASK, Each.HANDOFF),
}
"""Anchors that only an ask or a hand-off has."""

_MOMENT = re.compile(r"^\s*(?P<anchor>[a-z_]+)\s*(?:(?P<sign>[+-])\s*(?P<offset>P\S+))?\s*$")
_DURATION: TypeAdapter[timedelta] = TypeAdapter(timedelta)


class Moment(Model):
    """A moment of the run: an anchor and an offset from it."""

    anchor: Anchor
    offset: timedelta = timedelta(0)

    @classmethod
    def read(cls, said: str) -> Moment:
        found = _MOMENT.match(said)
        anchors = ", ".join(a.value for a in Anchor)
        if found is None:
            raise ValueError(f"{said!r} is not a moment: write an anchor ({anchors}) and an offset, e.g. ask+P1D")
        if found.group("anchor") not in {a.value for a in Anchor}:
            raise ValueError(f"{said!r}: {found.group('anchor')!r} is not an anchor; one of {anchors}")
        offset = timedelta(0)
        if found.group("offset") is not None:
            try:
                offset = _DURATION.validate_python(found.group("offset"))
            except ValidationError as e:
                raise ValueError(f"{said!r}: {found.group('offset')!r} is not an ISO 8601 duration (P1D, PT1H)") from e
            if found.group("sign") == "-":
                offset = -offset
        return cls(anchor=Anchor(found.group("anchor")), offset=offset)

    def __str__(self) -> str:
        if not self.offset:
            return self.anchor.value
        sign = "-" if self.offset < timedelta(0) else "+"
        return f"{self.anchor.value}{sign}{_iso(abs(self.offset))}"


def _iso(delta: timedelta) -> str:
    days, seconds = delta.days, delta.seconds
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    time = "".join(f"{n}{u}" for n, u in ((hours, "H"), (minutes, "M"), (seconds, "S")) if n)
    return "P" + (f"{days}D" if days else "") + (f"T{time}" if time else "") if days or time else "PT0S"


def _moment(said: str) -> str:
    Moment.read(said)
    return said.strip()


MomentText = Annotated[str, AfterValidator(_moment)]
"""A moment as written: `ask+P1D`. Read by `Moment.read`."""

Who = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
"""A Person.key; `owner` is the scenario's owner, whoever that is, and `person` the person the rule is read for."""

OWNER = "owner"
THIS_PERSON = "person"


class Write(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class Thing(StrEnum):
    """What the agent wrote, as the world records it."""

    MESSAGE = "message"
    TICKET = "ticket"
    COMMENT = "comment"
    DOCUMENT = "document"
    RECORD = "record"
    INBOX_ITEM = "inbox_item"
    FILE = "file"
    TOOL_CALL = "tool_call"


class FollowUps(Model):
    """The agent's writes the person could see on the ask while it was open: a message to them or their delegate,
    a change to the ask's thread or ticket. Read for `each: ask` or `handoff` only."""


class Touches(Model):
    """Every write of the agent's on the ask's person or its thread or ticket, open or not: after an answer, the
    agent coming back to it. Read for `each: ask` or `handoff` only."""


class Messages(Model):
    """Messages the agent sent."""

    to: list[Who] = Field(default=[], description="To any of these; empty: to anyone")
    to_not: list[Who] = Field(default=[], description="To none of these")
    in_thread: bool | None = Field(
        default=None, description="True: threaded under the ask the rule is read for; False: not under it"
    )
    holding: list[str] = Field(
        default=[],
        description="Each phrase must be in the text, in any case; `{ask.answer}` is the answer itself, and "
        "`{person.key}`, `{person.name}` the person the rule is read for",
    )
    to_away: bool | None = Field(
        default=None, description="True: to someone away at that moment while a delegate covered for them"
    )


class Writes(Model):
    """Every change the agent made to the world: what it created, updated or deleted."""

    things: list[Thing] = Field(default=[], description="Of these kinds; empty: of any")
    things_not: list[Thing] = Field(default=[], description="Of none of these kinds")
    operations: list[Write] = Field(default=[], description="These operations; empty: any of the three")
    repeats_open_ticket: bool | None = Field(
        default=None,
        description="True: a ticket created with the title (case, punctuation and spacing aside) of one still open in "
        "the same project",
    )
    in_repeated_wake: bool | None = Field(
        default=None, description="True: written in a wake that was the second delivery of the same wake (`dispatch`)"
    )
    gated: bool | None = Field(
        default=None,
        description="True: a call carrying an operation an item in the agent's own product held back (`gates`), made "
        "while that item was pending, turned down, or taken back undecided",
    )


class Wakes(Model):
    """The agent's wakes."""

    changed_world: bool | None = Field(default=None, description="False: the wake wrote nothing a person could see")
    changed_commitments: bool | None = None


class PlannedWakes(Model):
    """Wakes the agent asked for itself (reported, booked, its rhythm, its own timer), counted at the moment each
    was due, from the run loop's table. Only a run Minutehand played keeps that table."""


class CommitmentState(StrEnum):
    OPEN = "open"
    MET = "met"
    DROPPED = "dropped"


class Commitments(Model):
    """What the agent reported it was waiting on, as each wake ended, counted at that wake's end."""

    status: list[CommitmentState] = Field(default=[], description="In any of these states; empty: any")
    waiting_on: list[Who] = Field(default=[], description="Waiting on any of these people; empty: on anyone or nothing")


class Written(StrEnum):
    """Where a person's words came from (`domain.people.Writing`), as a rule names it."""

    SCRIPT = "script"  # a model, from a step of their script
    VERBATIM = "verbatim"  # the step's exact words, or a control pressed
    CONVERSING = "conversing"  # a model, from their own facts: no plan, or after the script was used
    AUTOMATIC = "automatic"  # their automatic reply while away
    BY_HAND = "by_hand"  # whoever drives a standing world, speaking for them


class Replies(Model):
    """What people said back to the agent: each reply or decision that landed, counted at the moment it landed."""

    by: list[Who] = Field(default=[], description="By any of these people; empty: by anyone")
    written: list[Written] = Field(default=[], description="Whose words, any of these; empty: any")


class Asks(Model):
    """The run's asks: each wait for a person's answer, counted at the moment it was made."""

    of: list[Who] = Field(default=[], description="Of any of these people; empty: of anyone")
    open_at: MomentText | None = Field(default=None, description="Still unanswered at this moment")


Scalar = str | int | float | bool | None


class Memory(Model):
    """Keys of the agent's memory (`minutehand.agent.store`) as they stood at the count's `until` (the run's end
    without one): each key holding a value there is one fact, counted at the moment that value was written, so
    `since` keeps only those written from then on. `key` and `prefix` may hold `{person.key}`."""

    collection: str = Field(default="default", pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    key: str | None = Field(default=None, description="Exactly this key")
    prefix: str | None = Field(default=None, description="Every key starting with this")
    values: dict[str, Scalar] = Field(
        default={},
        description="Each field of the value (a dotted path into it, `venue.city`) equal to this; a value that is "
        "not an object, or lacks the field, does not match",
    )

    @model_validator(mode="after")
    def _one_way(self) -> Self:
        if self.key is not None and self.prefix is not None:
            raise ValueError("memory: name a `key` or a `prefix`, not both")
        return self


class Count(Model):
    """Which facts a rule counts, and between which moments."""

    follow_ups: FollowUps | None = None
    touches: Touches | None = None
    messages: Messages | None = None
    writes: Writes | None = None
    wakes: Wakes | None = None
    planned_wakes: PlannedWakes | None = None
    commitments: Commitments | None = None
    asks: Asks | None = None
    memory: Memory | None = None
    replies: Replies | None = None
    since: MomentText | None = Field(default=None, description="From this moment, inclusive; absent: the start")
    until: MomentText | None = Field(default=None, description="To this moment, inclusive; absent: the end")

    @model_validator(mode="after")
    def _one(self) -> Self:
        named = [k for k, v in self._kinds() if v is not None]
        if len(named) != 1:
            raise ValueError(f"count one kind of fact, one of {', '.join(_COUNTED)}; this names {len(named) or 'none'}")
        return self

    def _kinds(self) -> list[tuple[str, Model | None]]:
        return [
            ("follow_ups", self.follow_ups),
            ("touches", self.touches),
            ("messages", self.messages),
            ("writes", self.writes),
            ("wakes", self.wakes),
            ("planned_wakes", self.planned_wakes),
            ("commitments", self.commitments),
            ("asks", self.asks),
            ("memory", self.memory),
            ("replies", self.replies),
        ]

    @property
    def counted(self) -> str:
        """The kind of fact counted, by its name in the file."""
        return next(k for k, v in self._kinds() if v is not None)


_COUNTED = (
    "follow_ups",
    "touches",
    "messages",
    "writes",
    "wakes",
    "planned_wakes",
    "commitments",
    "asks",
    "memory",
    "replies",
)
_ON_AN_ASK = ("follow_ups", "touches")


class Where(Model):
    """Which of the things the rule is read for: by the person asked or handed the work."""

    person: list[Who] = Field(default=[], description="Only these; empty: everyone")
    person_not: list[Who] = Field(default=[], description="Not these")


class StoppedBy(StrEnum):
    """How the run stopped, as a rule may name it (`domain.run.StopReason`)."""

    AGENT_DONE = "agent_done"
    WAKE_LIMIT = "wake_limit"
    DEADLINE_PASSED = "deadline_passed"
    NOTHING_PENDING = "nothing_pending"
    AGENT_FAILED = "agent_failed"
    CLOSED = "closed"
    ENVIRONMENT_FAILED = "environment_failed"


class When(Model):
    """A condition for reading the rule at all."""

    open_at: MomentText | None = Field(default=None, description="The ask or hand-off was unanswered at this moment")
    answered: bool | None = Field(default=None, description="It was answered, or the work finished, by the end")
    stopped: list[StoppedBy] = Field(default=[], description="The run stopped in one of these ways")


class Judged(StrEnum):
    """What a rule's finding asks of whoever reads it."""

    FAIL = "fail"  # the run failed
    REVIEW = "review"  # someone should look; the run does not fail on it


class Rule(Model):
    """One rule of a team's: for each `each`, where `when` holds, the `count` is within the bounds."""

    id: str = Field(
        pattern=r"^[a-z][a-z0-9_]*$", description="How findings name the rule; unique among the run's rules"
    )
    each: Each = Each.RUN
    where: Where = Where()
    at: list[MomentText] = Field(
        default=[], description="Read the rule once at each of these moments, named `moment` in the rest of it"
    )
    when: When = When()
    count: Count
    at_least: int | None = Field(default=None, ge=0)
    at_most: int | None = Field(default=None, ge=0)
    exactly: int | None = Field(default=None, ge=0)
    gap_at_least: timedelta | None = Field(
        default=None, gt=timedelta(0), description="No two of the counted facts closer together than this"
    )
    severity: Judged = Judged.FAIL
    message: str | None = Field(
        default=None,
        description="What the finding says; absent, one is written from the rule. May name {person.key}, "
        "{person.name}, {ask.at}, {ask.answer}, {rule.id}, {rule.count}, {rule.moment}",
    )
    pattern: str | None = Field(default=None, description="Pattern.key of the design that avoids what this finds")

    @model_validator(mode="after")
    def _reads(self) -> Self:
        if self.at_least is None and self.at_most is None and self.exactly is None and self.gap_at_least is None:
            raise ValueError(f"rule {self.id}: give a bound: at_least, at_most, exactly or gap_at_least")
        if self.exactly is not None and (self.at_least is not None or self.at_most is not None):
            raise ValueError(f"rule {self.id}: exactly takes neither at_least nor at_most")
        if self.at_least is not None and self.at_most is not None and self.at_least > self.at_most:
            raise ValueError(f"rule {self.id}: at_least {self.at_least} is more than at_most {self.at_most}")
        on_an_ask = self.each in (Each.ASK, Each.HANDOFF)
        if self.count.counted in _ON_AN_ASK and not on_an_ask:
            raise ValueError(
                f"rule {self.id}: {self.count.counted} are counted on an ask: write `each: ask` or `each: handoff`"
            )
        if (self.when.open_at is not None or self.when.answered is not None) and not on_an_ask:
            raise ValueError(f"rule {self.id}: `when.open_at` and `when.answered` are read for an ask or a hand-off")
        if self.where != Where() and self.each is Each.RUN:
            raise ValueError(f"rule {self.id}: `where` picks asks, hand-offs or people; this rule is read once")
        if self.count.messages is not None and self.count.messages.in_thread is not None and self.each is not Each.ASK:
            raise ValueError(f"rule {self.id}: `in_thread` is the thread of an ask: write `each: ask`")
        for said in self.moments():
            moment = Moment.read(said)
            if moment.anchor in SCOPED and self.each not in SCOPED[moment.anchor]:
                raise ValueError(
                    f"rule {self.id}: {said!r} names {moment.anchor.value}, which only an ask or a "
                    "hand-off has: write `each: ask` or `each: handoff`"
                )
            if moment.anchor is Anchor.MOMENT and not self.at:
                raise ValueError(f"rule {self.id}: {said!r} names moment, and the rule has no `at`")
        for who in self.people():
            if who == THIS_PERSON and self.each is Each.RUN:
                raise ValueError(f"rule {self.id}: `person` is the person the rule is read for; this one is read once")
        allowed = ("person.key", "person.name", "ask.at", "ask.answer", "rule.id", "rule.count", "rule.moment")
        if self.message is not None:
            refuse_unknown(f"rule {self.id}: message", self.message, allowed)
        if self.count.messages is not None:
            refuse_unknown(
                f"rule {self.id}: holding",
                list(self.count.messages.holding),
                ("ask.answer", "person.key", "person.name"),
            )
            if "ask.answer" in str(self.count.messages.holding) and self.each is not Each.ASK:
                raise ValueError(f"rule {self.id}: {{ask.answer}} is the answer to an ask: write `each: ask`")
        if self.count.memory is not None:
            named = [n for n in (self.count.memory.key, self.count.memory.prefix) if n is not None]
            refuse_unknown(f"rule {self.id}: memory", list(named), ("person.key",))
            if "{person.key}" in " ".join(named) and self.each is Each.RUN:
                raise ValueError(f"rule {self.id}: {{person.key}} is the person the rule is read for: write `each`")
        return self

    def moments(self) -> list[str]:
        """Every moment the rule names, as written."""
        said = [*self.at, self.when.open_at, self.count.since, self.count.until]
        if self.count.asks is not None:
            said.append(self.count.asks.open_at)
        return [s for s in said if s is not None]

    def people(self) -> list[str]:
        """Every person the rule names, as written."""
        named = [*self.where.person, *self.where.person_not]
        if self.count.messages is not None:
            named += [*self.count.messages.to, *self.count.messages.to_not]
        if self.count.commitments is not None:
            named += self.count.commitments.waiting_on
        if self.count.asks is not None:
            named += self.count.asks.of
        return named


def refuse_repeated_rules(rules: list[Rule]) -> None:
    ids = [r.id for r in rules]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise ValueError(f"two rules share an id: {', '.join(twice)}")


def refuse_unknown_people(rules: list[Rule], people: list[str]) -> None:
    """Every person a rule names is one of the scenario's, `owner` or `person`."""
    known = set(people) | {OWNER, THIS_PERSON}
    for rule in rules:
        unknown = sorted(set(rule.people()) - known)
        if unknown:
            raise ValueError(
                f"rule {rule.id} names {', '.join(unknown)}, who is not in the scenario; name a person's key, "
                "`owner` or `person`"
            )


def merged(agent: list[Rule], scenario: list[Rule], off: list[str]) -> list[Rule]:
    """The rules a run is judged by: the agent file's, each replaced by the scenario's rule of the same id, then the
    scenario's own, without those the scenario switches off. Refused when `off` names no rule."""
    by_id = {r.id: r for r in agent}
    by_id.update({r.id: r for r in scenario})
    unknown = sorted(set(off) - set(by_id))
    if unknown:
        raise ValueError(
            f"the scenario switches off {', '.join(unknown)}, which no rule of the agent file or the scenario is"
        )
    return [r for r in by_id.values() if r.id not in off]
