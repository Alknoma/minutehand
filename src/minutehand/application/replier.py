"""What every person says and decides, in a run and in a standing world alike.

Two steps, kept apart so a standing world can show what is owed before anything is written:

- `plan`: whether the person answers an ask at all, how (a step of their script, its exact words, a control, or
  conversing from their own facts), and when (`application.moments`). Computed from the scenario and the world;
  never a model call.
- `write`: the words, written when the plan is carried out. A step's words are a model's, from the step's facts and
  intent; conversing is a model's, from the person's facts, which may judge that a message needs no answer; a
  `verbatim` step or a control pressed needs no model. Every model call is kept with the world (`PersonCall`):
  its answer is replayed by any later ask with exactly the same context, so a rerun or a fork costs no model call
  until something it was shown differs; a failed call is kept too, and raises `ModelFailed` for the caller to try
  again later.

What the model is shown is what the person can see in the world, and nothing else: the conversation they are
answering, in full and in order, with their own earlier words, and the other conversations they take part in (their
own direct messages, channels they are a member of, threads they are on), most recent first. A message reaches a
person when it was addressed to them (`MessageSnapshot.recipient_emails`), or is their own; another person's
private messages never do. Beyond `history_turns` messages, the oldest are given as one summary, itself written by a
model once and replayed. The person is told to stay consistent with what they said; what they know changes only
where the scenario says so (`Person.fact_changes`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from pydantic import BaseModel, Field, model_validator

from minutehand.application.moments import (
    Owed,
    after_available,
    asks_of,
    draw,
    first_ask,
    pinned,
    refuse_unworkable_hours,
)
from minutehand.application.refusals import RunRefused
from minutehand.domain.clock import DrawnFrom
from minutehand.domain.conversation import ModelMessage, PersonCall, Provenance, Speaker, Wrote
from minutehand.domain.experiment import ReplyAt
from minutehand.domain.inboxes import HttpInbox
from minutehand.domain.people import PersonReply, Plan, Press, Writing
from minutehand.domain.scenario import (
    AfterScript,
    Answers,
    FormInput,
    Helpfulness,
    Intent,
    Model,
    Person,
    Scenario,
    Scripted,
    ScriptedReply,
    Silent,
    Speaks,
)
from minutehand.domain.world import (
    Actor,
    ControlKind,
    EntityRef,
    MessageAction,
    MessageSnapshot,
    Operation,
    WorldEvent,
)
from minutehand.ports.clock import Clock
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.model import ModelFailed
from minutehand.ports.store import Store

AnswerT = TypeVar("AnswerT", bound=BaseModel)

PERSON_PROMPT_VERSION = "person-reply/3"
"""Changes whenever PERSON_PROMPT, HELPFULNESS or what the person is shown changes a word."""

STEP_PROMPT_VERSION = "person-step/1"
"""Changes whenever STEP_PROMPT, INTENT or what the person is shown changes a word."""


SUMMARY_PROMPT_VERSION = "person-summary/1"
"""Changes whenever SUMMARY_PROMPT changes a word."""

PERSON_PROMPT = """\
You are {who}. You are at work, and someone has written to you. You are shown what you can see of your \
conversations: the one you are answering, in full and in order, and others you take part in. Write your reply to \
their last message, as yourself.

What you know:
{known}
{believed}
What you do with this message: {helpfulness}

Rules:
- First decide whether their last message needs an answer from you at all. A message that only tells you \
something, thanks you or closes the conversation needs none: then set "replies" to false and "text" to null.
- Stay consistent with what you said before. Where what you know now differs from what you said, what you know \
now holds; nothing else you said changes.
- You state nothing that is not in what you know{or_believe} or in what you said before. Asked something it does \
not cover, you say you do not know.
- You do not offer to find out, promise to come back, or name anyone else, unless what you know says so.
- {voice}
- Write only the message itself, as it would appear where they wrote to you. Today is {today}.
- Their last message may carry controls you can use instead of writing back, listed under it. To use one, set \
"press" to its label exactly as listed and "text" to null; if it opens a form, "form" is what you type into it, \
else null. To write back instead, set "press" and "form" to null.
"""

STEP_PROMPT = """\
You are {who}. You are at work, and someone has written to you. You are shown what you can see of your \
conversations: the one you are answering, in full and in order, and others you take part in. Write your reply to \
their last message, as yourself.

What you know:
{known}
{believed}
What this reply says:
{carries}

What you do with this message: {intent}
How you usually deal with a question: {helpfulness} What you do with this message wins over it.

Rules:
- Your reply says what this reply says, all of it, in your own words, and nothing that contradicts it.
- Stay consistent with what you said before. Where what you know now differs from what you said, what you know \
now holds; nothing else you said changes.
- You state nothing that is not in what this reply says, in what you know{or_believe} or in what you said before.
- {voice}
- Write only the message itself, as it would appear where they wrote to you. Today is {today}.
"""

HELPFULNESS: dict[Helpfulness, str] = {
    Helpfulness.FULL: "you answer what they ask, fully, from what you know.",
    Helpfulness.PARTIAL: (
        "you answer only part of what they ask, from what you know, and leave the rest unsaid. You do not "
        "mention that you left anything out."
    ),
    Helpfulness.ASKS_BACK: (
        "you do not answer yet. You reply with one question of your own that you want answered before you will."
    ),
    Helpfulness.DECLINES: (
        "you say that this is not yours to answer. You do not answer it, and you do not name anyone who might."
    ),
    Helpfulness.MISTAKEN: (
        "you answer confidently from what you believe. Where it disagrees with what you know, what you believe "
        "wins, and you do not hedge or mention any doubt."
    ),
}

INTENT: dict[Intent, str] = {
    Intent.ANSWER: "you answer it with what this reply says.",
    Intent.DECLINE: (
        "you say that it is not yours to answer, and you do not answer it. You name nobody unless this reply says so."
    ),
    Intent.ASK_BACK: "you do not answer yet: you ask one question of your own first, from what this reply says.",
    Intent.DEFER: "you say you will come back to it, with what this reply says of when or why, and do not answer yet.",
}


SUMMARY_PROMPT = """\
You are {who}. Below are messages from your conversations at work, oldest first. Write a short account of them \
for yourself: who asked you what, what you answered, and anything you said you would do. Keep every fact, name, \
number and date as it was said, and add nothing.
"""

_NOTHING = "- nothing about this beyond what the messages themselves say"


class WrittenReply(Model):
    """What the model answers for one message to one person, conversing: words written back, a control used, or
    nothing when the message needs no answer."""

    replies: bool = Field(description="Whether their last message needs an answer from you")
    text: str | None = Field(
        description="Your reply, exactly as you would send it; null when replies is false or you press a control"
    )
    press: str | None = Field(default=None, description="The label of the control you use, exactly as listed; or null")
    form: str | None = Field(default=None, description="What you type into the form the control opens; or null")

    @model_validator(mode="after")
    def _text_when_replying(self) -> WrittenReply:
        if not self.replies:
            if self.text is not None or self.press is not None:
                raise ValueError("replies is false, so text and press must be null")
            return self
        if self.press is not None:
            if self.text is not None:
                raise ValueError("press is set, so text must be null")
            return self
        if not (self.text and self.text.strip()):
            raise ValueError("replies is true, so text must be the reply")
        if self.form is not None:
            raise ValueError("form is what is typed after pressing a control; press is null")
        return self


class WrittenStep(Model):
    """What the model answers for one step of a person's script: the words of the reply, which there always are."""

    text: str = Field(min_length=1, description="Your reply, exactly as you would send it")


class WrittenSummary(Model):
    summary: str = Field(min_length=1, description="Your account of these messages")


# -- what a person needs a model for --------------------------------------------------------------------------------


def unspoken(scenario: Scenario, inboxes: Sequence[HttpInbox] = ()) -> list[str]:
    """Each person whose words a model writes, and why: refused before anything runs when no model is configured."""
    found: list[str] = []
    for person in scenario.people:
        why = _needs_model(person, inboxes)
        if why:
            found.append(f"{person.key} ({why})")
    return found


def _needs_model(person: Person, inboxes: Sequence[HttpInbox]) -> str | None:
    behaviour = person.reply
    if isinstance(behaviour, Silent):
        return None
    if isinstance(behaviour, Answers):
        return "reply kind 'answers'"
    if behaviour.then is AfterScript.ANSWERS:
        return "they go on conversing once their script is used: `then: answers`, the default"
    written = [r.to_ask for r in behaviour.replies if r.written]
    if written:
        return f"a model writes their scripted step{'s' if len(written) > 1 else ''} to ask {written[0]}"
    return None


# -- the plan ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Turn:
    at: datetime
    provider: str
    channel: str
    who: str
    text: str


class PeopleReplier:
    """`ports.people.Replier` for everybody in a scenario. A person whose words a model writes, with no model
    configured, is refused here, naming them, before anything runs."""

    def __init__(
        self,
        scenario: Scenario,
        model: LanguageModel | None,
        inboxes: Sequence[HttpInbox] = (),
        *,
        pins: Sequence[ReplyAt] = (),
    ) -> None:
        refuse_unworkable_hours(scenario)
        needing = unspoken(scenario, inboxes)
        if needing and model is None:
            raise RunRefused(
                f"a model writes what {'; '.join(needing)} say{'s' if len(needing) == 1 else ''}, and no model is configured"
            )
        self._scenario = scenario
        self._model = model
        self._inboxes = {i.name: i for i in inboxes}
        self._pins = {(p.person, p.to_ask): p.after for p in pins if p.provider is None}

    @property
    def scenario(self) -> Scenario:
        return self._scenario

    # -- plan ---------------------------------------------------------------------------------------------------

    def plan(
        self, person: Person, asked: WorldEvent, history: Sequence[WorldEvent], owed: Sequence[Owed]
    ) -> Plan | None:
        """What `person` does about `asked`, and when; None when they do nothing: they are silent, their script
        skips this ask or says no more, it is a follow-up on an answer they already owe, or the control their
        asked is no message (an item of the agent's own product, which the people engine decides)."""
        behaviour = person.reply
        if isinstance(behaviour, Silent):
            return None
        upto = [e for e in history if e.seq <= asked.seq]
        if not isinstance(asked.after, MessageSnapshot):
            raise RunRefused(f"{person.key} was asked to answer {asked.entity.kind.value}, which is not a message")
        asks = asks_of(person, upto, owed)
        nth = next((n for n, e in enumerate(asks, start=1) if e.entity == asked.entity), None)
        if nth is None:
            return None
        if isinstance(behaviour, Answers):
            return self._planned(person, asked, upto, nth, Writing.CONVERSING, behaviour)
        step = next((r for r in behaviour.replies if r.to_ask == nth), None)
        if step is None:
            if nth <= behaviour.last_ask or behaviour.then is AfterScript.SILENT:
                return None
            return self._planned(person, asked, upto, nth, Writing.CONVERSING, behaviour)
        writing = Writing.VERBATIM if step.verbatim is not None else Writing.SCRIPT
        return self._planned(person, asked, upto, nth, writing, behaviour, step=step)

    def _planned(
        self,
        person: Person,
        asked: WorldEvent,
        upto: list[WorldEvent],
        nth: int,
        writing: Writing,
        behaviour: Speaks,
        *,
        step: ScriptedReply | None = None,
    ) -> Plan:
        within = step.within if step is not None else None
        if (person.key, nth) in self._pins:
            drawn = pinned(self._scenario, asked, self._pins[(person.key, nth)])
        else:
            drawn = draw(self._scenario, person, asked, upto, within=within, delay=behaviour.delay)
        return Plan(
            person=person.key,
            asked=asked.entity,
            nth=nth,
            writing=writing,
            step=step,
            drawn=drawn,
        )

    # -- write --------------------------------------------------------------------------------------------------

    async def decide(
        self, person: Person, asked: WorldEvent, history: list[WorldEvent], clock: Clock, world: Store | None = None
    ) -> PersonReply | None:
        """`plan`, then `write` at once: the reply `person` gives `asked`, or None. Without a world, only words no
        model writes can be given, and nothing is owed."""
        planned = self.plan(person, asked, history, owed_in(world) if world is not None else [])
        if planned is None:
            return None
        return await self.write(person, asked, planned, history, world, clock)

    async def write(
        self,
        person: Person,
        asked: WorldEvent,
        plan: Plan,
        history: Sequence[WorldEvent],
        world: Store | None,
        clock: Clock,
    ) -> PersonReply | None:
        """The words of `plan`: the reply that lands at its moment, or None when conversing found that the message
        needs no answer. Raises `ModelFailed` when the model could not write them; the call is kept either way."""
        behaviour = person.reply
        assert isinstance(behaviour, Answers | Scripted)
        patience = behaviour.delay.longest
        if plan.drawn.source is DrawnFrom.WINDOW and plan.drawn.window is not None:
            latest = after_available(
                asked.sim_time,
                plan.drawn.window.max,
                person,
                self._scenario.starts_at,
                first_ask=first_ask(person, history, asked),
            )
            patience = latest - asked.sim_time
        common = {
            "person": person.key,
            "in_reply_to": asked.entity,
            "at": plan.at,
            "patience": patience,
            "drawn": plan.drawn,
            "writing": plan.writing,
        }
        if plan.step is not None and plan.step.verbatim is not None:
            return PersonReply(text=plan.step.verbatim, **common)
        world = _kept_in(world, person)
        context = await self.context(person, behaviour, asked, history, world, clock)
        if plan.step is not None:
            step = plan.step
            system = step_prompt(person, behaviour, step, clock.now(), self._scenario.starts_at)
            written = await self.ask_model(
                person, Wrote.REPLY, asked.entity, system, context, WrittenStep, STEP_PROMPT_VERSION, world, clock
            )
            return PersonReply(
                text=written.answer.text,
                facts=list(step.facts),
                written_by=Provenance(model=written.model, prompt_version=STEP_PROMPT_VERSION),
                **common,
            )
        system = person_prompt(person, behaviour, clock.now(), self._scenario.starts_at)
        written = await self.ask_model(
            person, Wrote.REPLY, asked.entity, system, context, WrittenReply, PERSON_PROMPT_VERSION, world, clock
        )
        said = written.answer
        if not said.replies:
            return None
        press: Press | None = None
        if said.press is not None:
            assert isinstance(asked.after, MessageSnapshot)
            usable = [a for a in asked.after.actions if a.control is not ControlKind.LINK]
            control = next((a for a in usable if a.label == said.press), None)
            if control is None:
                raise RunRefused(
                    f"the model had {person.key} press {said.press!r}, which is not a control on the message: "
                    f"{[a.label for a in usable]}"
                )
            press = Press(
                action_id=control.action_id,
                label=control.label,
                value=control.value,
                form=[FormInput(value=said.form)] if said.form else [],
            )
        text = said.form or said.text or (press.label if press is not None else "")
        return PersonReply(
            text=text,
            press=press,
            written_by=Provenance(model=written.model, prompt_version=PERSON_PROMPT_VERSION),
            **common,
        )

    # -- what the person sees --------------------------------------------------------------------------------------

    async def context(
        self,
        person: Person,
        behaviour: Answers | Scripted,
        asked: WorldEvent,
        history: Sequence[WorldEvent],
        world: Store,
        clock: Clock,
        *,
        answers: bool = True,
    ) -> list[ModelMessage]:
        """Everything the person can see, up to `asked`, as one message to the model: the conversation they answer
        in order, the others they are in most recent first, and the oldest beyond their budget as a summary. With
        `answers` False they answer no message: every conversation is one they are in."""
        turns, answering = seen_by(self._scenario, person, asked, history, world.replies())
        if not answers:
            answering = None
        kept = turns[-behaviour.history_turns :]
        older = turns[: -behaviour.history_turns] if len(turns) > behaviour.history_turns else []
        parts: list[str] = []
        if older:
            summary = await self._summary(person, behaviour, older, world, clock)
            parts.append(f"Earlier, in short, as you remember it:\n{summary}")
        others: dict[tuple[str, str], list[_Turn]] = {}
        here: list[_Turn] = []
        for turn in kept:
            if (turn.provider, turn.channel) == answering:
                here.append(turn)
            else:
                others.setdefault((turn.provider, turn.channel), []).append(turn)
        if others:
            ordered = sorted(others.items(), key=lambda kv: kv[1][-1].at, reverse=True)
            lines = ["Other conversations you are in, most recent first:"]
            for (provider, channel), said in ordered:
                lines.append(f"## {provider} {channel}")
                lines += [_line(t) for t in said]
            parts.append("\n".join(lines))
        if isinstance(asked.after, MessageSnapshot) and answers:
            last = here[-1] if here else None
            earlier = here[:-1] if here else []
            lines = ["The conversation you are answering, oldest first:"]
            lines += [_line(t) for t in earlier] or ["(nothing before their last message)"]
            parts.append("\n".join(lines))
            if last is not None:
                parts.append(
                    "Their last message, which you answer now:\n" + _line(last) + _controls(asked.after.actions)
                )
        elif here:
            parts.append("\n".join(["Where the item was raised:", *(_line(t) for t in here)]))
        return [ModelMessage(speaker=Speaker.ASKER, text="\n\n".join(parts) or "(you can see no conversation)")]

    async def _summary(
        self, person: Person, behaviour: Answers | Scripted, older: list[_Turn], world: Store, clock: Clock
    ) -> str:
        said = "\n".join(_line(t) for t in older)
        system = SUMMARY_PROMPT.format(who=who_is(person))
        written = await self.ask_model(
            person,
            Wrote.SUMMARY,
            None,
            system,
            [ModelMessage(speaker=Speaker.ASKER, text=said)],
            WrittenSummary,
            SUMMARY_PROMPT_VERSION,
            world,
            clock,
            temperature=0,
        )
        return written.answer.summary

    # -- the model, through the world's record -------------------------------------------------------------------

    async def ask_model(
        self,
        person: Person,
        wrote: Wrote,
        asked: EntityRef | None,
        system: str,
        messages: Sequence[ModelMessage],
        answer: type[AnswerT],
        prompt_version: str,
        world: Store,
        clock: Clock,
        *,
        temperature: float | None = None,
    ) -> _Written[AnswerT]:
        behaviour = person.reply
        assert isinstance(behaviour, Answers | Scripted)
        if self._model is None:
            raise RunRefused(f"a model writes what {person.key} says, and no model is configured")
        named = behaviour.model or self._model.model_id
        heat = behaviour.temperature if temperature is None else temperature
        key = context_key(named, prompt_version, system, messages, answer.__name__, heat)
        call = {
            "key": key,
            "person": person.key,
            "wrote": wrote,
            "asked": asked,
            "model": named,
            "prompt_version": prompt_version,
            "sim_time": clock.now(),
            "wake": clock.wake(),
        }
        kept = world.written(key)
        if kept is not None:
            world.record_person_call(PersonCall.model_validate({**call, "answer": kept, "replayed": True}))
            return _Written(answer.model_validate_json(kept), named)
        try:
            answered = await self._model.answer(system, messages, answer, model=behaviour.model, temperature=heat)
        except ModelFailed as e:
            world.record_person_call(PersonCall.model_validate({**call, "failure": str(e)}))
            raise
        world.record_person_call(
            PersonCall.model_validate(
                {
                    **call,
                    "answer": answered.answer.model_dump_json(),
                    "input_tokens": answered.input_tokens,
                    "output_tokens": answered.output_tokens,
                }
            )
        )
        return _Written(answered.answer, named)


@dataclass(frozen=True)
class _Written[T]:
    answer: T
    model: str


def _kept_in(world: Store | None, person: Person) -> Store:
    """The world a model's words are kept with; refused without one, since they are replayed from it."""
    if world is None:
        raise RunRefused(f"a model writes what {person.key} says here, and its words are kept with a world: none given")
    return world


def owed_in(world: Store) -> list[Owed]:
    """Every answer the world's reply table holds as on its way or given, by the message it answers: what tells a
    follow-up from a new ask. An automatic reply is no answer."""
    return [(r.in_reply_to, r.at) for r in world.replies() if r.answers]


def context_key(
    model: str, prompt_version: str, system: str, messages: Sequence[ModelMessage], answer: str, temperature: float
) -> str:
    """Everything a model is asked, as one SHA-256: two asks with the same key get the same answer from the record."""
    said = json.dumps(
        [model, prompt_version, system, [[m.speaker.value, m.text] for m in messages], answer, temperature]
    )
    return hashlib.sha256(said.encode()).hexdigest()


# -- prompts ------------------------------------------------------------------------------------------------------


def bulleted(lines: Sequence[str]) -> str:
    return "\n".join(f"- {line}" for line in lines) if lines else _NOTHING


def who_is(person: Person) -> str:
    return f"{person.name}, {person.title}" if person.title else person.name


def believed_part(behaviour: Speaks, stale: list[str]) -> str:
    if behaviour.helpfulness is Helpfulness.MISTAKEN and stale:
        return f"\nWhat you believe, and hold to be true:\n{bulleted(stale)}\n"
    return ""


def voice_rule(behaviour: Speaks) -> str:
    return f"You write like this: {behaviour.voice}." if behaviour.voice else "You write plainly and briefly."


def person_prompt(person: Person, behaviour: Speaks, today: datetime, starts_at: datetime) -> str:
    """The system text for a person conversing. `stale_facts` reach the model only for a MISTAKEN person."""
    facts, stale = person.knows_at(today, starts_at)
    believed = believed_part(behaviour, stale)
    return PERSON_PROMPT.format(
        who=who_is(person),
        known=bulleted(facts),
        believed=believed,
        helpfulness=HELPFULNESS[behaviour.helpfulness],
        or_believe=" or in what you believe" if believed else "",
        voice=voice_rule(behaviour),
        today=today.strftime("%A %d %B %Y"),
    )


def step_prompt(person: Person, behaviour: Speaks, step: ScriptedReply, today: datetime, starts_at: datetime) -> str:
    """The system text for one step of a person's script."""
    facts, stale = person.knows_at(today, starts_at)
    believed = believed_part(behaviour, stale)
    carries = bulleted(step.facts) if step.facts else "- what you know, as it bears on their last message"
    return STEP_PROMPT.format(
        who=who_is(person),
        known=bulleted(facts),
        believed=believed,
        carries=carries,
        intent=INTENT[step.intent],
        helpfulness=HELPFULNESS[behaviour.helpfulness],
        or_believe=" or in what you believe" if believed else "",
        voice=voice_rule(behaviour),
        today=today.strftime("%A %d %B %Y"),
    )


def _line(turn: _Turn) -> str:
    return f"[{turn.at:%Y-%m-%d %H:%M} UTC] {turn.who}: {turn.text}"


def _controls(actions: list[MessageAction]) -> str:
    """The controls under a message, as the person sees them; a link is not one they can answer with."""
    usable = [a for a in actions if a.control is not ControlKind.LINK]
    if not usable:
        return ""
    return "\n[Controls: " + ", ".join(f'"{a.label}"' for a in usable) + "]"


def seen_by(
    scenario: Scenario,
    person: Person,
    asked: WorldEvent,
    history: Sequence[WorldEvent],
    replies: Sequence[PersonReply],
) -> tuple[list[_Turn], tuple[str, str] | None]:
    """Every message `person` can see up to `asked`, oldest first, each as it last read by then, and the
    conversation (provider, channel) `asked` is in. A message is theirs to see when it was addressed to them, or is
    their own: a person's message not addressed to them in a conversation they were written to in. Nobody sees
    another person's private messages."""
    upto = [e for e in history if e.seq <= asked.seq]
    reads: dict[EntityRef, str] = {}
    gone: set[EntityRef] = set()
    for e in upto:
        if e.operation is Operation.DELETE:
            gone.add(e.entity)
        elif e.operation in (Operation.CREATE, Operation.UPDATE) and isinstance(e.after, MessageSnapshot):
            reads[e.entity] = e.after.text
    by_key = {p.key: p for p in scenario.people}
    places: set[tuple[str, str]] = set()
    turns: list[_Turn] = []
    for e in upto:
        after = e.after
        if e.operation is not Operation.CREATE or not isinstance(after, MessageSnapshot) or e.entity in gone:
            continue
        where = (e.entity.provider, after.channel)
        mine = person.email in after.recipient_emails
        if mine:
            places.add(where)
        if e.actor is Actor.AGENT:
            if not mine:
                continue
            who = "They"
        elif e.actor is Actor.PERSON:
            if mine:
                who = _author(e, replies, by_key, after)
            elif where in places:
                who = "You"
            else:
                continue
        else:
            if not mine:
                continue
            who = "Someone"
        turns.append(
            _Turn(at=e.sim_time, provider=e.entity.provider, channel=after.channel, who=who, text=reads[e.entity])
        )
    answering = (asked.entity.provider, asked.after.channel) if isinstance(asked.after, MessageSnapshot) else None
    return turns, answering


def _author(
    event: WorldEvent,
    replies: Sequence[PersonReply],
    by_key: dict[str, Person],
    after: MessageSnapshot,
) -> str:
    """Who wrote a person's message someone else sees: the person whose reply landed then, in that provider, and
    who is not among its recipients."""
    for reply in replies:
        if reply.at == event.sim_time and reply.in_reply_to.provider == event.entity.provider:
            author = by_key[reply.person] if reply.person in by_key else None
            if author is not None and author.email not in after.recipient_emails:
                return author.name
    return "Someone"
