"""Did the world end up the way the scenario said it must?

Each expectation selects events of one macro kind (a person was asked, a ticket
was created, deleted, or reached a state) and bounds how many there must be.

A word match is a substring match, so a met expectation can be hollow: the agent
restated the question to the owner instead of the answer, or wrote "hello" inside
a paragraph. Nothing here guesses at that. Each met expectation is reported as an
informational finding that quotes what met it, trimmed, and who it went to, so a
reader sees a hollow pass for what it is.

A `Relayed` expectation asks whether a message carried what a person said, and is
answered from the log alone, through the phrase the scenario's author declared as
the tell: the first thing in the world to hold it must be that person's own reply,
and a match is a later message from the agent to the named person that holds it.

A `PersonAsked` with `about` asks what a message means, which no word match can
answer: it is left to the judged check `asked_about`, and noted here as left.
"""

from __future__ import annotations

from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.scenario import (
    Expectation,
    PersonAsked,
    Relayed,
    Scenario,
    TicketCreated,
    TicketDeleted,
    TicketInState,
)
from minutehand.domain.world import (
    Actor,
    DocumentSnapshot,
    EntityKind,
    EntityRef,
    InteractionSnapshot,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
    TicketSnapshot,
    WorldEvent,
)

QUOTED = 160
"""How much of a matching message or ticket a met expectation quotes."""

SHOWN = 3
"""How many of the matches a met expectation quotes; the rest are counted."""


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
            unheard: str | None = None
            if isinstance(expected, Relayed):
                heard, unheard = _heard(expected, view)
                matched = [e for e in matched if heard is not None and e.seq > heard.seq]
            if expected.by is not None:
                matched = [e for e in matched if e.sim_time <= start + expected.by]
            if isinstance(expected, TicketInState):
                matched = _first_per_ticket(matched)
            count = len(matched)
            too_few = count < expected.at_least
            too_many = expected.at_most is not None and count > expected.at_most
            if not (too_few or too_many):
                findings.append(
                    Finding(
                        check=self.id,
                        severity=Severity.INFORMATION,
                        kind=FindingKind.INFORMATIONAL,
                        message=f"{self.describe(expected)}: met by {_met_by(matched, view.scenario)}",
                        evidence=[e.seq for e in matched],
                    )
                )
                continue
            wanted = f"at least {expected.at_least}" if too_few else f"at most {expected.at_most}"
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.ERROR,
                    kind=FindingKind.FAIL,
                    message=f"{self.describe(expected)}: wanted {wanted}, found {count}"
                    + (f": {unheard}" if unheard is not None else ""),
                    evidence=[e.seq for e in matched],
                    pattern=self.pattern,
                )
            )
        return CheckReport(findings=findings, notes=notes)

    @staticmethod
    def failed(report: CheckReport) -> int:
        """How many expectations the report says were not met: its failures, never what it notes as met."""
        return sum(1 for f in report.findings if f.kind is FindingKind.FAIL)

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
        if isinstance(expected, Relayed):
            return (
                event.actor is Actor.AGENT
                and event.operation is Operation.CREATE
                and isinstance(after, MessageSnapshot)
                and email[expected.to] in after.recipient_emails
                and has_words(after.text, [expected.tell])
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
        if isinstance(expected, Relayed):
            return f"{expected.to} told what {expected.said_by} said ({expected.tell!r})"
        return f"ticket for {expected.assignee} in state {expected.state.value}"


def _met_by(matched: list[WorldEvent], scenario: Scenario) -> str:
    """What met an expectation, quoted: each message with who it went to, each ticket with its holder."""
    if not matched:
        return "nothing, as wanted"
    names = {p.email: p.name for p in scenario.people}
    shown = [_quoted(e, names) for e in matched[:SHOWN]]
    more = f"; and {len(matched) - SHOWN} more" if len(matched) > SHOWN else ""
    return "; ".join(shown) + more


def _quoted(event: WorldEvent, names: dict[str, str]) -> str:
    after = event.after
    if isinstance(after, MessageSnapshot):
        to = ", ".join(names[e] if e in names else e for e in after.recipient_emails) or f"channel {after.channel}"
        return f"the message to {to} (seq {event.seq}): \u201c{_trimmed(after.text)}\u201d"
    if isinstance(after, TicketSnapshot):
        holder = after.assignee_email
        held = f" for {names[holder] if holder in names else holder}" if holder is not None else ""
        return f"the ticket{held} (seq {event.seq}): \u201c{_trimmed(after.title)}\u201d, {after.state.value}"
    return f"{event.operation.value} of {event.entity.kind.value} {event.entity.external_id} (seq {event.seq})"


def _trimmed(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= QUOTED else flat[: QUOTED - 1].rstrip() + "\u2026"


_FIRST = {
    Actor.AGENT: "the agent wrote it",
    Actor.PERSON: "someone else said it",
    Actor.SCENARIO: "the scenario's own setup held it",
}


def _held(event: WorldEvent) -> str | None:
    """The text an event put into the world, for finding a tell in: a message, a ticket, a record, a document, what a
    person typed into a form."""
    after = event.after
    if isinstance(after, MessageSnapshot):
        return after.text
    if isinstance(after, TicketSnapshot):
        return f"{after.title} {after.body}"
    if isinstance(after, RecordSnapshot):
        return after.text
    if isinstance(after, DocumentSnapshot):
        return after.title if after.text is None else f"{after.title} {after.text}"
    if isinstance(after, InteractionSnapshot) and after.form:
        return "\n".join(after.form)
    return None


def _heard(expected: Relayed, view: RunView) -> tuple[WorldEvent | None, str | None]:
    """The event by which `said_by` first put the tell into the world, or why there is none: someone else held
    it first, or they never said it."""
    first = next((e for e in view.events if (text := _held(e)) is not None and has_words(text, [expected.tell])), None)
    if first is None:
        return None, f"{expected.said_by} never said it"
    spoken = (
        first.actor is Actor.PERSON
        and first.operation is Operation.CREATE
        and (
            isinstance(first.after, MessageSnapshot)
            or (isinstance(first.after, InteractionSnapshot) and first.after.person == expected.said_by)
        )
        and any(
            r.person == expected.said_by and r.at == first.sim_time and has_words(r.text, [expected.tell])
            for r in view.replies
        )
    )
    if not spoken:
        return None, f"{_FIRST[first.actor]} (seq {first.seq}) before {expected.said_by} said it, so nothing relayed it"
    return first, None
