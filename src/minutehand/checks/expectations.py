"""Did the world end up the way the scenario said it must?

Each expectation selects events of one macro kind (a person was asked, a ticket
was created, deleted, or reached a state) and bounds how many there must be.

A `PersonAsked` with `about` asks what a message means, which no word match can
answer: it is left to the judged check `asked_about`, and noted here as left.
"""

from __future__ import annotations

from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.scenario import (
    Expectation,
    PersonAsked,
    TicketCreated,
    TicketDeleted,
    TicketInState,
)
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    MessageSnapshot,
    Operation,
    TicketSnapshot,
    WorldEvent,
)


def has_words(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return all(w.lower() in lowered for w in words)


def _first_per_ticket(events: list[WorldEvent]) -> list[WorldEvent]:
    """A ticket written twice in a state is one ticket in it: count tickets, not their versions."""
    seen: set[EntityRef] = set()
    first: list[WorldEvent] = []
    for event in events:
        if event.entity not in seen:
            seen.add(event.entity)
            first.append(event)
    return first


class Expectations:
    id = "expectations"
    needs = frozenset({Needs.WORLD})
    pattern = "honest_closure"

    def run(self, view: RunView) -> CheckReport:
        email = {p.key: p.email for p in view.scenario.people}
        start = view.scenario.starts_at
        findings: list[Finding] = []
        notes: list[str] = []
        for expected in view.scenario.expect:
            if isinstance(expected, PersonAsked) and expected.about is not None:
                notes.append(f"{self.describe(expected)}: left to the judged check asked_about")
                continue
            matched = [e for e in view.events if self.matches(expected, e, email)]
            if expected.by is not None:
                matched = [e for e in matched if e.sim_time <= start + expected.by]
            if isinstance(expected, TicketInState):
                matched = _first_per_ticket(matched)
            count = len(matched)
            too_few = count < expected.at_least
            too_many = expected.at_most is not None and count > expected.at_most
            if not (too_few or too_many):
                continue
            wanted = f"at least {expected.at_least}" if too_few else f"at most {expected.at_most}"
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.ERROR,
                    kind=FindingKind.FAIL,
                    message=f"{self.describe(expected)}: wanted {wanted}, found {count}",
                    evidence=[e.seq for e in matched],
                    pattern=self.pattern,
                )
            )
        return CheckReport(findings=findings, notes=notes)

    @staticmethod
    def matches(expected: Expectation, event: WorldEvent, email: dict[str, str]) -> bool:
        after = event.after
        if isinstance(expected, PersonAsked):
            return (
                event.actor is Actor.AGENT
                and event.operation is Operation.CREATE
                and isinstance(after, MessageSnapshot)
                and email[expected.person] in after.recipient_emails
                and has_words(after.text, expected.mentions)
            )
        if isinstance(expected, TicketCreated):
            return (
                event.actor is Actor.AGENT
                and event.operation is Operation.CREATE
                and isinstance(after, TicketSnapshot)
                and (expected.assignee is None or after.assignee_email == email[expected.assignee])
                and has_words(f"{after.title} {after.body}", expected.mentions)
            )
        if isinstance(expected, TicketDeleted):
            return (
                event.actor is Actor.AGENT
                and event.operation is Operation.DELETE
                and event.entity.kind is EntityKind.TICKET
            )
        if isinstance(expected, TicketInState):
            return (
                isinstance(after, TicketSnapshot)
                and after.assignee_email == email[expected.assignee]
                and after.state is expected.state
            )
        raise TypeError(f"no matcher for {type(expected).__name__}")

    @staticmethod
    def describe(expected: Expectation) -> str:
        if isinstance(expected, PersonAsked):
            words = f" mentioning {expected.mentions}" if expected.mentions else ""
            about = f" about {expected.about!r}" if expected.about is not None else ""
            return f"{expected.person} asked{about}{words}"
        if isinstance(expected, TicketCreated):
            words = f" mentioning {expected.mentions}" if expected.mentions else ""
            return f"ticket created for {expected.assignee or 'anyone'}{words}"
        if isinstance(expected, TicketDeleted):
            return "ticket deleted"
        return f"ticket for {expected.assignee} in state {expected.state.value}"
