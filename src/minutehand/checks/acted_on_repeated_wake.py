"""The scenario delivered one of the agent's own wakes twice, and the agent changed the world again on the second.

At-least-once delivery is how most schedulers and queues behave, so a scenario can deliver a wake twice
(`DispatchRule`, `fault: twice`). The second delivery is an entry of the run loop's table marked as repeating the
first (`DueEntry.asked_for`), dispatched as the wake that follows it. An agent with a guard sees the work already
done and changes nothing; one without does it again. Writing something in the second wake is not always doing it
twice (a follow-up may have fallen due by then), so the finding is a REVIEW that lists what was written.

It also notes each dispatch rule that never applied, because the agent never asked for that wake: a scenario
that says "the third booking is dropped" against an agent that books twice tests nothing it meant to.
"""

from __future__ import annotations

from datetime import timedelta

from minutehand.checks._waits import VISIBLE, span
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.clock import AGENT_SOURCES, PLANNED_BY, REACHED, DueClosed, DueEntry
from minutehand.domain.scenario import DispatchFault, DispatchRule, PlannedBy
from minutehand.domain.world import Actor


class ActedOnRepeatedWake:
    id = "acted_on_repeated_wake"
    needs = frozenset({Needs.WORLD})  # a repeat is found from the table; with no wake recorded there is none
    pattern = "no_double_tick"

    def run(self, view: RunView) -> CheckReport:
        dues = view.dues
        if dues is None:
            return CheckReport(
                blocked=[f"{self.id}: the run kept no table of what was due, so no wake is known to be a repeat"]
            )
        findings: list[Finding] = []
        for again in dues:
            if again.fault is not DispatchFault.TWICE or again.asked_for is None or again.closed is not DueClosed.FIRED:
                continue
            assert again.closed_at is not None and again.closed_wake is not None
            wake = next(
                (w for w in view.wakes if w.index == again.closed_wake + 1 and w.sim_time == again.closed_at), None
            )
            if wake is None:
                continue
            written = [
                e.seq for e in view.events if e.wake == wake.index and e.actor is Actor.AGENT and e.operation in VISIBLE
            ]
            if not written:
                continue
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=f"wake {wake.index} was the second delivery of the agent's own wake for "
                    f"{again.asked_for:%Y-%m-%d %H:%M} UTC, {_gap(again.due.at - again.asked_for)} after the first, "
                    f"and the agent changed the world {len(written)} time{'s' if len(written) != 1 else ''} in it",
                    at=again.closed_at,
                    wake=wake.index,
                    evidence=written,
                    pattern=self.pattern,
                )
            )
        return CheckReport(findings=findings, notes=_unapplied(view.scenario.dispatch, dues))


def _unapplied(rules: list[DispatchRule], dues: list[DueEntry]) -> list[str]:
    reached: dict[PlannedBy, int] = {}
    for d in dues:
        if d.source in AGENT_SOURCES and d.asked_for is None and d.closed in REACHED:
            reached[PLANNED_BY[d.source]] = reached.get(PLANNED_BY[d.source], 0) + 1
    notes = []
    for i, rule in enumerate(rules):
        count = reached.get(rule.wakes, 0)
        if count == 0 or (rule.nth is not None and rule.nth > count):
            which = f"the {_nth(rule.nth)} {rule.wakes.value} wake" if rule.nth else f"each {rule.wakes.value} wake"
            notes.append(
                f"dispatch rule {i + 1} ({which} {rule.fault.value}) never applied: "
                f"{count} {rule.wakes.value} wake{'s' if count != 1 else ''} fell due"
            )
    return notes


def _gap(delta: timedelta) -> str:
    minutes = round(delta.total_seconds() / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes < 60 else span(delta)


def _nth(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
