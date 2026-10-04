"""The agent kept messaging someone about a wait the world had already settled.

What a message is about is read from the world, never guessed: a message threaded
under the ask that was answered, or one naming the title of the ticket that was
finished. Whether it chased or merely thanked is a judgement, so this answers
`review`.
"""

from __future__ import annotations

from minutehand.checks._waits import blocked
from minutehand.checks.ledger import recipients
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, ObligationKind, RunView, Severity
from minutehand.domain.world import Actor, MessageSnapshot, Operation, TicketSnapshot


class KeptChasingAfterDone:
    id = "kept_chasing_after_done"
    needs = frozenset({Needs.WORLD, Needs.OBLIGATIONS})
    pattern = "one_open_ask_per_person"

    def run(self, view: RunView) -> CheckReport:
        missing = blocked(view, self.needs, self.id)
        if missing:
            return CheckReport(blocked=missing)
        by_seq = {e.seq: e for e in view.events}
        findings: list[Finding] = []
        for o in view.obligations:
            if o.kind is ObligationKind.DATE or o.settled_at is None or o.person is None or o.entity is None:
                continue
            opening = by_seq.get(o.opened_by)
            if opening is None:
                continue
            title = opening.after.title.lower() if isinstance(opening.after, TicketSnapshot) else None
            for event in view.events:
                after = event.after
                if (
                    event.actor is not Actor.AGENT
                    or event.operation is not Operation.CREATE
                    or not isinstance(after, MessageSnapshot)
                    or event.sim_time < o.settled_at
                    or o.person not in {p.key for p in recipients(event, view.scenario)}
                ):
                    continue
                if o.kind is ObligationKind.ANSWER_FROM_PERSON:
                    about = after.thread_of == o.entity.external_id
                else:
                    about = title is not None and title in after.text.lower()
                if not about:
                    continue
                findings.append(
                    Finding(
                        check=self.id,
                        severity=Severity.WARNING,
                        kind=FindingKind.REVIEW,
                        message=f"messaged {o.person} about something they had already "
                        + ("answered" if o.kind is ObligationKind.ANSWER_FROM_PERSON else "finished"),
                        at=event.sim_time,
                        wake=event.wake,
                        evidence=[o.opened_by, event.seq],
                        pattern=self.pattern,
                    )
                )
        return CheckReport(findings=findings)
