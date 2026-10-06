"""The agent changed the world after the scenario's deadline had passed.

Writing a ticket or a document after the date is work done too late, and fails.
A message after it may be the honest report that the date was missed, so a wake
whose only writes are messages answers `review`.
"""

from __future__ import annotations

from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.world import Actor, EntityKind, Operation, WorldEvent

WRITES = frozenset({Operation.CREATE, Operation.UPDATE, Operation.DELETE})


class ActedAfterDeadline:
    id = "acted_after_deadline"
    needs = frozenset({Needs.WORLD})
    pattern = "budgeted_follow_up"

    def run(self, view: RunView) -> CheckReport:
        deadline = view.scenario.deadline
        if deadline is None:
            return CheckReport(notes=["scenario sets no deadline"])
        late: dict[int, list[WorldEvent]] = {}
        for event in view.events:
            if event.actor is Actor.AGENT and event.operation in WRITES and event.sim_time > deadline:
                late.setdefault(event.wake, []).append(event)
        findings: list[Finding] = []
        for wake, events in late.items():
            only_messages = all(e.entity.kind is EntityKind.MESSAGE for e in events)
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.WARNING if only_messages else Severity.ERROR,
                    kind=FindingKind.REVIEW if only_messages else FindingKind.FAIL,
                    message=f"{len(events)} change(s) to the world after the deadline"
                    + (", all of them messages" if only_messages else ""),
                    at=events[0].sim_time,
                    wake=wake,
                    evidence=[e.seq for e in events],
                    pattern=self.pattern,
                )
            )
        return CheckReport(findings=findings)
