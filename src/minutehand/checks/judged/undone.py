"""Work left undone: what the agent's own instructions give it to do, which it did not do in the window though
nothing in the world stopped it.

Every other check reads something the agent did. An agent that does nothing does nothing wrong, and so passes every
one of them; this check reads the window whole. It is shown the agent's own instructions to its model (its statement
of its work), the world the scenario declares, everything the agent did and when, what people said to it, and how
each item stood at the end, and names each part of the work left undone with what in the world allowed it: an answer
that came, a decision that was made, an item that was open to the agent's move. Work the world kept from the agent
(nobody answered, nobody decided) is not undone by the agent, and is not named.

It needs the agent's instructions, read from its model calls (`RunView.agent_instructions`): without them nobody can
say what its work was, and it does not run. Its findings are for review, as every model's judgement is."""

from __future__ import annotations

from pydantic import Field

from minutehand.checks.facts import transitions
from minutehand.checks.judged.review import reviewed
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.conversation import Judgement, ModelMessage, Speaker, Wrote
from minutehand.domain.items import Assessed, AssessedKind
from minutehand.domain.scenario import Model
from minutehand.domain.world import EntityRef
from minutehand.ports.model import Model as LanguageModel

UNDONE_PROMPT_VERSION = "work-undone/1"

UNDONE_PROMPT = """\
You read how an agent spent a stretch of simulated time. It works on its own: its own instructions say what its \
work is. You are shown those instructions, the world its users declared (the people, the services), everything the \
agent did and when, what people said to it, and how each item stood at the end.

Name each part of the work its instructions give it that it left undone, where what you are shown establishes that \
the world allowed it: the answer it needed came, the decision it waited on was made, the item was open to its move, \
and time was left. Do not name work the world kept from it (nobody answered, nobody decided, the time ran out before \
it could), work its instructions do not give it, or how it might have done its work better. For each, say what was \
left undone, what in the world allowed it, quoted from what you are shown, and why, in one or two sentences. Answer \
with nothing undone when nothing shown establishes it."""

SHOWN = 80
"""The most of the agent's acts, and of what people said, shown: the latest."""


class Undone(Model):
    what: str = Field(description="The part of its work left undone, as its instructions put it")
    allowed_by: str = Field(description="What in the world allowed it, quoted from what was shown")
    rationale: str = Field(description="Why, in one or two sentences")


class WorkReview(Model):
    undone: list[Undone] = Field(description="Every part of its work left undone; none when nothing shown says so")


def _cut(text: str, most: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[: most - 1] + "…"


def shown_whole(view: RunView) -> str:
    """The window as the reviewer reads it: instructions, world, acts, what was said, how items ended."""
    scenario = view.scenario
    parts = ["The agent's own instructions, as it gave them to its model:\n" + "\n---\n".join(view.agent_instructions)]
    people = [
        f"- {p.name} <{p.email}>" + (f": {_cut(p.profile)}" if p.profile.strip() else "") for p in scenario.people
    ]
    parts.append("People:\n" + "\n".join(people))
    if scenario.services:
        parts.append(
            "Declared services:\n"
            + "\n".join(
                f"- {s.key} ({s.host})" + (f": {_cut(s.describe)}" if s.describe else "") for s in scenario.services
            )
        )
    window = scenario.window_end
    parts.append(f"The window: {scenario.starts_at.isoformat()} to {window.isoformat() if window else 'its end'}")
    acts = [
        f"- {t.at.isoformat()} {t.kind.value} {t.operation.value}"
        + (f" to {', '.join(t.people)}" if t.people else "")
        + (f": {_cut(t.text, 240)}" if t.text else "")
        for t in reviewed(view)
    ]
    parts.append("What the agent did, oldest first:\n" + ("\n".join(acts[-SHOWN:]) if acts else "(nothing)"))
    said = [f"- {r.at.isoformat()} {r.person}: {_cut(r.text, 240)}" for r in view.replies]
    parts.append("What people said to it, oldest first:\n" + ("\n".join(said[-SHOWN:]) if said else "(nothing)"))
    last: dict[str, str] = {}
    for moved in transitions(view):
        last[f"{moved.transition.provider} {moved.transition.item.external_id}"] = moved.transition.to_state
    parts.append(
        "How each item stood at the end:\n" + ("\n".join(f"- {k}: {v}" for k, v in last.items()) if last else "(none)")
    )
    return "\n\n".join(parts)


class WorkUndone:
    id = "work_left_undone"
    needs = frozenset({Needs.WORLD})
    prompt_version = UNDONE_PROMPT_VERSION
    wrote = Wrote.UNDONE

    def applies(self, view: RunView) -> bool:
        return bool(view.agent_instructions)

    async def judge(self, view: RunView, model: LanguageModel, *, failed: frozenset[EntityRef]) -> CheckReport:
        answered = await model.answer(
            UNDONE_PROMPT, [ModelMessage(speaker=Speaker.ASKER, text=shown_whole(view))], WorkReview, temperature=0
        )
        end = view.scenario.window_end or max((e.sim_time for e in view.events), default=view.scenario.starts_at)
        findings = [
            Finding(
                check=self.id,
                severity=Severity.WARNING,
                kind=FindingKind.REVIEW,
                message=f"left undone: {u.what} ({u.rationale})",
                at=end,
                assessed=Assessed(kind=AssessedKind.WRONG_ACTION, item=None, against=u.allowed_by),
                judged=Judgement(model=answered.model, prompt_version=self.prompt_version, rationale=u.rationale),
            )
            for u in answered.answer.undone
        ]
        return CheckReport(
            findings=findings,
            notes=[f"the window read whole for work left undone by {model.model_id} ({self.prompt_version})"],
        )
