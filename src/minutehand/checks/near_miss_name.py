"""A protected name written almost, but not exactly, as the scenario gave it.

The defect this exists for: an agent given a goal naming one company researched
a different company whose name is one letter away, then filed two tickets and
sent two messages under that name (run f431fc97f427, 2026-08-24).
"""

from __future__ import annotations

import re

from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.world import Actor, MessageSnapshot, Operation, RecordSnapshot, TicketSnapshot

WORD = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def _distance(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


class NearMissName:
    id = "near_miss_name"
    needs = frozenset({Needs.WORLD})
    pattern = "confirm_names"

    def run(self, view: RunView) -> CheckReport:
        names = view.scenario.protected_names
        if not names:
            return CheckReport(notes=["scenario declares no protected_names"])
        given = [view.scenario.goal or "", *view.agent_instructions]
        allowed = {w.lower() for text in given for w in WORD.findall(text)}
        findings: list[Finding] = []
        for event in view.events:
            if event.actor is not Actor.AGENT or event.operation is not Operation.CREATE:
                continue
            if isinstance(event.after, TicketSnapshot):
                written = f"{event.after.title} {event.after.body}"
            elif isinstance(event.after, MessageSnapshot):
                written = event.after.text
            elif isinstance(event.after, RecordSnapshot):
                written = event.after.text  # a resource nobody mapped: its strings are still what the agent wrote
            else:
                continue
            for word in dict.fromkeys(WORD.findall(written)):
                for name in names:
                    if word == name or word.lower() in allowed or len(word) != len(name):
                        continue
                    if word.lower() != name.lower() and _distance(word.lower(), name.lower()) == 1:
                        findings.append(
                            Finding(
                                check=self.id,
                                severity=Severity.ERROR,
                                kind=FindingKind.FAIL,
                                message=f'wrote "{word}" where the scenario says "{name}"',
                                at=event.sim_time,
                                wake=event.wake,
                                evidence=[event.seq],
                                pattern=self.pattern,
                            )
                        )
        return CheckReport(findings=findings)
