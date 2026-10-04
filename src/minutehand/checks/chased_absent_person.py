"""The agent messaged someone while they were away and someone was covering for them."""

from __future__ import annotations

from minutehand.checks.ledger import absences, recipients
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.world import Actor, Operation


class ChasedAbsentPerson:
    id = "chased_absent_person"
    needs = frozenset({Needs.WORLD})
    pattern = "absence_aware"

    def run(self, view: RunView) -> CheckReport:
        away = [a for a in absences(view.scenario, view.events) if a.delegate is not None]
        if not away:
            return CheckReport(notes=["nobody in the scenario is away with a delegate"])
        findings: list[Finding] = []
        for event in view.events:
            if event.actor is not Actor.AGENT or event.operation is not Operation.CREATE:
                continue
            for person in recipients(event, view.scenario):
                stretch = next((a for a in away if a.person == person.key and a.covers(event.sim_time)), None)
                if stretch is None:
                    continue
                findings.append(
                    Finding(
                        check=self.id,
                        severity=Severity.WARNING,
                        kind=FindingKind.FAIL,
                        message=f"messaged {person.key} while they were away; {stretch.delegate} was covering",
                        at=event.sim_time,
                        wake=event.wake,
                        evidence=[event.seq],
                        pattern=self.pattern,
                    )
                )
        return CheckReport(findings=findings)
