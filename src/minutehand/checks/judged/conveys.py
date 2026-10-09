"""The team's rules that say what a message must convey (`Messages.conveys`), in whatever words: read here, with a
judge model, as the deterministic pass reads every other rule.

Everything but the meaning stays deterministic: the rule's subjects, its moments, the messages' recipients and the
literal `holding` phrases. Each message a rule counts is shown to the model with one phrase, alone, and the model
answers whether the message conveys it; the rule is then read with those answers, once each. Its findings are for
review (`FindingKind.REVIEW`), each naming the model and its reasons.
"""

from __future__ import annotations

from pydantic import Field

from minutehand.checks.assessments import Assessments, judged, read_with
from minutehand.domain.checks import CheckReport, FindingKind, Needs, RunView
from minutehand.domain.conversation import Judgement, ModelMessage, Speaker
from minutehand.domain.scenario import Model
from minutehand.domain.world import EntityRef
from minutehand.ports.model import Model as LanguageModel

CONVEYS_PROMPT_VERSION = "conveys/1"

CONVEYS_PROMPT = """\
You read one message an assistant sent at work, and a statement. Decide whether the message conveys the statement: \
whether someone who reads only the message learns what the statement says. Other words, a paraphrase or a summary \
that keeps its meaning count; a message that leaves out what the statement says, contradicts it, or only hints at \
it does not. Judge the message alone. Give your reason in one or two sentences.
"""


class ConveysVerdict(Model):
    conveys: bool = Field(description="Whether the message conveys the statement")
    rationale: str = Field(description="Why, in one or two sentences")


class Conveys:
    id = "conveys"
    needs = frozenset({Needs.WORLD})
    prompt_version = CONVEYS_PROMPT_VERSION

    async def judge(self, view: RunView, model: LanguageModel, *, failed: frozenset[EntityRef]) -> CheckReport:
        del failed  # a rule's messages are judged whatever another check said of them
        rules = [r for r in view.rules if judged(r)]
        if not rules:
            return CheckReport()
        asked: set[tuple[str, str]] = set()

        def ask(text: str, phrase: str) -> bool:
            asked.add((text, phrase))
            return True  # so every phrase of every message is asked

        for rule in rules:
            read_with(view, rule, ask)
        verdicts: dict[tuple[str, str], ConveysVerdict] = {}
        for text, phrase in sorted(asked):
            shown = f"Statement: {phrase}\n\nMessage:\n{text}"
            answered = await model.answer(
                CONVEYS_PROMPT, [ModelMessage(speaker=Speaker.ASKER, text=shown)], ConveysVerdict, temperature=0
            )
            verdicts[(text, phrase)] = answered.answer
        report = Assessments().run(
            view.model_copy(update={"rules": rules}), conveyed=lambda t, p: verdicts[(t, p)].conveys
        )
        why = " ".join(f"[{p!r}] {v.rationale}" for (_, p), v in verdicts.items())
        return CheckReport(
            findings=[
                f.model_copy(
                    update={
                        "kind": FindingKind.REVIEW,
                        "judged": Judgement(model=model.model_id, prompt_version=self.prompt_version, rationale=why),
                    }
                )
                for f in report.findings
            ],
            notes=report.notes,
            rules_read=report.rules_read,
        )
