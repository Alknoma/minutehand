"""A person followed up on one ask more often than they take, each time before their answer was due.

A follow-up gives the person their whole delay again (`checks/_waits.py`), so an agent that pings just before
every due moment keeps its wait current forever and is never late: the scorecard alone read the nagging reference
agent exactly as the diligent one. Each follow-up sent before the wait fell due is an early one; past the
person's `early_follow_ups` on one ask, it is nagging.
"""

from __future__ import annotations

from minutehand.checks._waits import blocked, chases, ended_at
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, ObligationKind, RunView, Severity


class Nagged:
    id = "nagged"
    needs = frozenset({Needs.WORLD, Needs.OBLIGATIONS})
    pattern = "budgeted_follow_up"

    def run(self, view: RunView) -> CheckReport:
        missing = blocked(view, self.needs, self.id)
        if missing:
            return CheckReport(blocked=missing)
        people = {p.key: p for p in view.scenario.people}
        by_seq = {e.seq: e for e in view.events}
        findings: list[Finding] = []
        for chased in chases(view, ended_at(view)):
            o = chased.obligation
            if o.kind is not ObligationKind.ANSWER_FROM_PERSON or o.person not in people:
                continue
            takes = people[o.person].early_follow_ups
            if len(chased.early) <= takes:
                continue
            last = by_seq[chased.early[-1]]
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.FAIL,
                    message=f"{o.person} was followed up {len(chased.early)} times on one ask, each before their "
                    f"answer was due; they take {takes}",
                    at=last.sim_time,
                    wake=last.wake,
                    evidence=[o.opened_by, *chased.early] if o.opened_by in by_seq else list(chased.early),
                    pattern=self.pattern,
                )
            )
        return CheckReport(findings=findings)
