"""A person followed up on one ask more often than they take, each time before their answer was due.

A follow-up gives the person their whole delay again (`checks/_waits.py`), so an agent that pings just before
every due moment keeps its wait current forever and is never late: the scorecard alone read the nagging reference
agent exactly as the diligent one. Each follow-up sent before the wait fell due is an early one; past the
person's `early_follow_ups` on one ask, it is nagging.

A follow-up sent in the same wake as the ask, within the hour (`Chase.instant`), is nagging whatever the budget: the
person could not have answered yet. It is reviewed rather than failed, since a second message seconds after the
first may add what the first forgot, and whether it did is in its words.
"""

from __future__ import annotations

from datetime import timedelta

from minutehand.checks._waits import Chase, blocked, chases, ended_at
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, ObligationKind, RunView, Severity
from minutehand.domain.world import WorldEvent


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
                if chased.instant:
                    findings.append(self._instant(chased, by_seq))
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

    def _instant(self, chased: Chase, by_seq: dict[int, WorldEvent]) -> Finding:
        o = chased.obligation
        first = by_seq[chased.instant[0]]
        count = len(chased.instant)
        return Finding(
            check=self.id,
            severity=Severity.WARNING,
            kind=FindingKind.REVIEW,
            message=f"{o.person} was followed up in the same wake as the ask, "
            f"{_after(first.sim_time - o.opened_at)} after it"
            f"{f' ({count} times)' if count > 1 else ''}, before they could have answered",
            at=first.sim_time,
            wake=first.wake,
            evidence=[o.opened_by, *chased.instant] if o.opened_by in by_seq else list(chased.instant),
            pattern=self.pattern,
        )


def _after(delta: timedelta) -> str:
    seconds = round(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    minutes = round(seconds / 60)
    return f"{minutes} minute{'' if minutes == 1 else 's'}"
