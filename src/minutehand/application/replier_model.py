"""People whose replies a model writes (`Answers`), and the one replier a run uses for everybody.

The model decides two things about one message the agent sent one person: whether it needs an answer at all,
and what the person says. It is told who they are, what they know (`facts`), how they write (`voice`), what
they do with a question (`helpfulness`) and the exchange with the agent so far in this run. A message that
needs no answer, an FYI or a thank-you, gets None, and so opens no obligation; that is the model's decision,
in the structured answer, and nothing here second-guesses it.

When the reply lands is not the model's decision: `replier_scripted.lands_at` places it exactly as it places
a scripted person's, from the same seed.

A reply is remembered with the model and the prompt version that wrote it (`PersonReply.written_by`); a fork
replays what was remembered, so a rerun asks the model nothing about a message answered before the fork.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from minutehand.application.refusals import RunRefused
from minutehand.application.replier_scripted import ScriptedReplier, lands_at, refuse_unworkable_hours
from minutehand.domain.conversation import ModelMessage, Provenance, Speaker
from minutehand.domain.people import PersonReply, Press
from minutehand.domain.scenario import Answers, FormInput, Helpfulness, Model, Person, Scenario
from minutehand.domain.world import (
    Actor,
    ControlKind,
    EntityKind,
    MessageAction,
    MessageSnapshot,
    Operation,
    WorldEvent,
    reached,
)
from minutehand.ports.clock import Clock
from minutehand.ports.model import Model as LanguageModel

PERSON_PROMPT_VERSION = "person-reply/2"
"""Changes whenever PERSON_PROMPT or HELPFULNESS changes a word, so a stored reply names the text that wrote it."""

PERSON_PROMPT = """\
You are {who}. You are at work, and someone has written to you in your team's chat. Every message they sent \
you so far is given to you as theirs, and everything you wrote back as yours. Write your reply to their last \
message, as yourself.

What you know:
{known}
{believed}
What you do with this message: {helpfulness}

Rules:
- First decide whether their last message needs an answer from you at all. A message that only tells you \
something, thanks you or closes the conversation needs none: then set "replies" to false and "text" to null.
- You state nothing that is not in what you know{or_believe}. Asked something it does not cover, you say you \
do not know.
- You do not offer to find out, promise to come back, or name anyone else, unless what you know says so.
- {voice}
- Write only the message itself, as it would appear in the chat. Today is {today}.
- Their last message may carry controls you can use instead of writing back, listed under it. To use one, set \
"press" to its label exactly as listed and "text" to null; if it opens a form, "form" is what you type into it, \
else null. To write back instead, set "press" and "form" to null.
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

_NOTHING = "- nothing about this beyond what the messages themselves say"


class WrittenReply(Model):
    """What the model answers for one message to one person: words written back, or a control used."""

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


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines) if lines else _NOTHING


def person_prompt(person: Person, behaviour: Answers, today: datetime) -> str:
    """The system text for one person. `stale_facts` reach the model only for a MISTAKEN person."""
    mistaken = behaviour.helpfulness is Helpfulness.MISTAKEN
    believed = (
        f"\nWhat you believe, and hold to be true:\n{_bullets(person.stale_facts)}\n"
        if mistaken and person.stale_facts
        else ""
    )
    return PERSON_PROMPT.format(
        who=f"{person.name}, {person.title}" if person.title else person.name,
        known=_bullets(person.facts),
        believed=believed,
        helpfulness=HELPFULNESS[behaviour.helpfulness],
        or_believe=" or in what you believe" if believed else "",
        voice=f"You write like this: {behaviour.voice}." if behaviour.voice else "You write plainly and briefly.",
        today=today.strftime("%A %d %B %Y"),
    )


def exchange(person: Person, asked: WorldEvent, history: list[WorldEvent]) -> list[ModelMessage]:
    """The agent's messages to this person, and theirs back, up to and including `asked`.

    Each message is where it was posted, reading as its last edit up to `asked` says: `asked` may itself be an
    edit. A person's own message is one recorded as actor PERSON in a conversation the agent wrote to them in
    and not addressed to them: whoever wrote a message is the one participant it is not sent to.
    """
    upto = [e for e in history if e.seq < asked.seq] + [asked]
    reads = {
        e.entity: e.after.text
        for e in upto
        if e.operation in (Operation.CREATE, Operation.UPDATE) and isinstance(e.after, MessageSnapshot)
    }
    controls = {
        e.entity: e.after.actions
        for e in upto
        if e.operation in (Operation.CREATE, Operation.UPDATE) and isinstance(e.after, MessageSnapshot)
    }
    channels: set[tuple[str, str]] = set()
    said: list[ModelMessage] = []
    for event in upto:
        after = event.after
        if not (event.operation is Operation.CREATE and isinstance(after, MessageSnapshot)):
            continue
        where = (event.entity.provider, after.channel)
        to_them = person in reached(after, [person])
        if event.actor is Actor.AGENT and to_them:
            channels.add(where)
            said.append(
                ModelMessage(speaker=Speaker.ASKER, text=reads[event.entity] + _controls(controls[event.entity]))
            )
        elif event.actor is Actor.PERSON and where in channels and not to_them:
            said.append(ModelMessage(speaker=Speaker.MODEL, text=reads[event.entity]))
    return said


def _controls(actions: list[MessageAction]) -> str:
    """The controls under a message, as the person sees them; a link is not one they can answer with."""
    usable = [a for a in actions if a.control is not ControlKind.LINK]
    if not usable:
        return ""
    return "\n\n[Controls: " + ", ".join(f'"{a.label}"' for a in usable) + "]"


class ModelReplier:
    """`ports.people.Replier` for `Answers` people."""

    def __init__(self, scenario: Scenario, model: LanguageModel) -> None:
        refuse_unworkable_hours(scenario)
        self._scenario = scenario
        self._model = model

    async def decide(
        self, person: Person, asked: WorldEvent, history: list[WorldEvent], clock: Clock
    ) -> PersonReply | None:
        behaviour = person.reply
        if not isinstance(behaviour, Answers):
            raise RunRefused(f"{person.key}'s replies are not written by a model; the model replier cannot write them")
        if asked.entity.kind is not EntityKind.MESSAGE or not isinstance(asked.after, MessageSnapshot):
            raise RunRefused(f"{person.key} was asked to answer {asked.entity.kind.value}, which is not a message")
        written = await self._model.answer(
            person_prompt(person, behaviour, asked.sim_time),
            exchange(person, asked, history),
            WrittenReply,
            model=behaviour.model,
            temperature=behaviour.temperature,
        )
        if not written.replies:
            return None
        press: Press | None = None
        if written.press is not None:
            usable = [a for a in asked.after.actions if a.control is not ControlKind.LINK]
            control = next((a for a in usable if a.label == written.press), None)
            if control is None:
                raise RunRefused(
                    f"the model had {person.key} press {written.press!r}, which is not a control on the message: "
                    f"{[a.label for a in usable]}"
                )
            press = Press(
                action_id=control.action_id,
                label=control.label,
                value=control.value,
                form=[FormInput(value=written.form)] if written.form else [],
            )
        text = written.form or written.text or (press.label if press is not None else "")
        return PersonReply(
            person=person.key,
            in_reply_to=asked.entity,
            text=text,
            press=press,
            at=lands_at(self._scenario, person, asked, history, behaviour.delay),
            patience=behaviour.delay.longest,
            written_by=Provenance(model=behaviour.model or self._model.model_id, prompt_version=PERSON_PROMPT_VERSION),
        )


class PeopleReplier:
    """The run's replier: each person to the scripted or the model replier by their `reply` kind.

    A scenario with an `Answers` person and no model is refused here, naming them, before anything runs.
    """

    def __init__(self, scenario: Scenario, model: LanguageModel | None) -> None:
        written = [p.key for p in scenario.people if isinstance(p.reply, Answers)]
        if written and model is None:
            raise RunRefused(
                f"a model writes the replies of {', '.join(written)} (reply kind 'answers'), and no model is configured"
            )
        self._scripted = ScriptedReplier(scenario)
        self._written = ModelReplier(scenario, model) if model is not None else None

    async def decide(
        self, person: Person, asked: WorldEvent, history: list[WorldEvent], clock: Clock
    ) -> PersonReply | None:
        if isinstance(person.reply, Answers):
            if self._written is None:
                raise RunRefused(f"a model writes {person.key}'s replies, and no model is configured")
            return await self._written.decide(person, asked, history, clock)
        return await self._scripted.decide(person, asked, history, clock)
