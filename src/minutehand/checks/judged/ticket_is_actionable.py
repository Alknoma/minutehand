"""Would the assignee, knowing only what the ticket says, know what is being asked of them and by when?

Every ticket the agent filed is judged as it stood at the end of the run, unless it was deleted or a
deterministic check already failed it. The model is shown the ticket and who it is for, and nothing of the
conversation that led to it, because the assignee would have nothing else either.
"""

from __future__ import annotations

from pydantic import Field

from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.conversation import Judgement, ModelMessage, Speaker
from minutehand.domain.scenario import Model
from minutehand.domain.world import Actor, EntityRef, Operation, TicketSnapshot, WorldEvent
from minutehand.ports.model import Model as LanguageModel

TICKET_PROMPT_VERSION = "ticket-is-actionable/1"

TICKET_PROMPT = """\
You read one ticket from a work tracker, as the person it is assigned to would read it, knowing nothing else: \
not the conversation that led to it, not the project's history. Decide two things. First, would they know \
what is being asked of them: what to do, and what counts as done? Second, would they know by when: a date, a \
day, or a deadline they could place on a calendar from what the ticket says and the day it was filed? A vague \
"soon" or "when you can" is no date. Give your reason in one or two sentences, naming what is missing.
"""


class TicketVerdict(Model):
    knows_what: bool = Field(description="Whether the assignee would know what is asked of them and what done is")
    knows_by_when: bool = Field(description="Whether the assignee would know by when it is wanted")
    rationale: str = Field(description="Why, naming what is missing, in one or two sentences")


def _ticket(ticket: TicketSnapshot, assignee: str, filed: WorldEvent) -> str:
    project = f"Project: {ticket.project}\n" if ticket.project else ""
    return (
        f"Assigned to: {assignee}\nFiled on: {filed.sim_time:%A %d %B %Y}\n{project}"
        f"Title: {ticket.title}\n\nDescription:\n{ticket.body or '(none)'}"
    )


def _missing(verdict: TicketVerdict) -> str:
    if not verdict.knows_what and not verdict.knows_by_when:
        return "what is asked or by when"
    return "what is asked" if not verdict.knows_what else "by when"


class TicketIsActionable:
    id = "ticket_is_actionable"
    needs = frozenset({Needs.WORLD})
    prompt_version = TICKET_PROMPT_VERSION

    async def judge(self, view: RunView, model: LanguageModel, *, failed: frozenset[EntityRef]) -> CheckReport:
        people = {p.email: p for p in view.scenario.people}
        filed: dict[EntityRef, WorldEvent] = {}
        latest: dict[EntityRef, WorldEvent] = {}
        for event in view.events:
            if event.operation is Operation.DELETE:
                filed.pop(event.entity, None)
                continue
            if not isinstance(event.after, TicketSnapshot):
                continue
            if event.actor is Actor.AGENT and event.operation is Operation.CREATE:
                filed[event.entity] = event
            if event.entity in filed:
                latest[event.entity] = event
        findings: list[Finding] = []
        judged = 0
        for entity, first in filed.items():
            if entity in failed:
                continue
            last = latest[entity]
            ticket = last.after
            assert isinstance(ticket, TicketSnapshot)
            person = people[ticket.assignee_email] if ticket.assignee_email in people else None
            if person is not None:
                assignee = f"{person.name}, {person.title}" if person.title else person.name
            else:
                assignee = ticket.assignee_email or "nobody yet"
            verdict = (
                await model.answer(
                    TICKET_PROMPT,
                    [ModelMessage(speaker=Speaker.ASKER, text=_ticket(ticket, assignee, first))],
                    TicketVerdict,
                    temperature=0,
                )
            ).answer
            judged += 1
            if verdict.knows_what and verdict.knows_by_when:
                continue
            who = person.key if person is not None else assignee
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=(
                        f"ticket {ticket.title!r} for {who}: they would not know {_missing(verdict)}. "
                        f"{verdict.rationale}"
                    ),
                    at=last.sim_time,
                    wake=last.wake,
                    evidence=[last.seq],
                    judged=Judgement(
                        model=model.model_id, prompt_version=self.prompt_version, rationale=verdict.rationale
                    ),
                )
            )
        return CheckReport(findings=findings, notes=[f"judged {judged} ticket(s)"])
