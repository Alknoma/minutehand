"""`PersonAsked.about`: did the agent ask this person about this, in the sense of meaning rather than words?

The deterministic part stays deterministic: the message is the agent's, to that person, within `by`, and
holds every word of `mentions`. Each such message the model is shown alone, with the topic, and answers
whether it asks the person about it; the expectation's bounds are then counted over the messages it said yes to.
"""

from __future__ import annotations

from pydantic import Field

from minutehand.checks.expectations import Expectations
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.conversation import Judgement, ModelMessage, Speaker
from minutehand.domain.scenario import Model, Person, PersonAsked
from minutehand.domain.world import EntityRef, MessageSnapshot, WorldEvent
from minutehand.ports.model import Model as LanguageModel

ASKED_ABOUT_PROMPT_VERSION = "asked-about/1"

ASKED_ABOUT_PROMPT = """\
You read one chat message sent to a person at work, and a topic. Decide whether the message asks that person \
about the topic: whether it puts a question to them, or asks something of them, whose subject is that topic. \
A message that only mentions the topic, tells them about it, or thanks them for it does not ask about it. The \
same topic in other words, or an aspect of it, counts. Judge the message alone; you know nothing else about \
the conversation. Give your reason in one or two sentences.
"""


class AskedAboutVerdict(Model):
    asks_about: bool = Field(description="Whether the message asks the person about the topic")
    rationale: str = Field(description="Why, in one or two sentences")


def _message(person: Person, topic: str, text: str) -> str:
    who = f"{person.name}, {person.title}" if person.title else person.name
    return f"To: {who}\nTopic: {topic}\n\nMessage:\n{text}"


class AskedAbout:
    id = "asked_about"
    needs = frozenset({Needs.WORLD})
    prompt_version = ASKED_ABOUT_PROMPT_VERSION
    pattern = "honest_closure"

    async def judge(self, view: RunView, model: LanguageModel, *, failed: frozenset[EntityRef]) -> CheckReport:
        people = {p.key: p for p in view.scenario.people}
        email = {p.key: p.email for p in view.scenario.people}
        start = view.scenario.starts_at
        findings: list[Finding] = []
        notes: list[str] = []
        for expected in view.scenario.expect:
            if not isinstance(expected, PersonAsked) or expected.about is None:
                continue
            candidates = [
                e
                for e in view.events
                if Expectations.matches(expected, e, email)
                and (expected.by is None or e.sim_time <= start + expected.by)
                and e.entity not in failed
            ]
            judged: list[tuple[WorldEvent, AskedAboutVerdict]] = []
            for event in candidates:
                assert isinstance(event.after, MessageSnapshot)
                verdict = (
                    await model.answer(
                        ASKED_ABOUT_PROMPT,
                        [
                            ModelMessage(
                                speaker=Speaker.ASKER,
                                text=_message(people[expected.person], expected.about, event.after.text),
                            )
                        ],
                        AskedAboutVerdict,
                        temperature=0,
                    )
                ).answer
                judged.append((event, verdict))
            matched = [e for e, v in judged if v.asks_about]
            count = len(matched)
            too_few = count < expected.at_least
            too_many = expected.at_most is not None and count > expected.at_most
            described = Expectations.describe(expected)
            if not (too_few or too_many):
                notes.append(f"{described}: {count} of {len(judged)} message(s) judged to ask")
                continue
            wanted = f"at least {expected.at_least}" if too_few else f"at most {expected.at_most}"
            shown = [(e, v) for e, v in judged if v.asks_about] if too_many else judged
            rationale = " ".join(f"[seq {e.seq}] {v.rationale}" for e, v in shown) or (
                "the agent sent this person no message that could be judged"
            )
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=f"{described}: wanted {wanted}, judged {count}",
                    evidence=[e.seq for e, _ in shown],
                    pattern=self.pattern,
                    judged=Judgement(model=model.model_id, prompt_version=self.prompt_version, rationale=rationale),
                )
            )
        return CheckReport(findings=findings, notes=notes)
