"""A wait passed its expected date and the agent did follow up, but later than a grace after it.

The defect this exists for: on a captured run a reminder went out 33 hours after the
wait it chased had expired. The agent did follow up, so a check for silence would
pass it; the cost is the stretch between expiry and the reminder.
"""

from __future__ import annotations

from minutehand.checks._waits import GRACE, blocked, ended_at, follow_up
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.0f} hours"


class LateFollowUp:
    id = "late_follow_up"
    needs = frozenset({Needs.WORLD, Needs.OBLIGATIONS})
    pattern = "expiry_on_every_wait"

    def run(self, view: RunView) -> CheckReport:
        missing = blocked(view, self.needs, self.id)
        if missing:
            return CheckReport(blocked=missing)
        when = {e.seq: e.sim_time for e in view.events}
        wake = {e.seq: e.wake for e in view.events}
        ended = ended_at(view)
        findings: list[Finding] = []
        for o in view.obligations:
            wait = follow_up(o, when, ended)
            if wait is None or wait.touch is None or wait.gap <= GRACE:
                continue
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.FAIL,
                    message=f"followed up {_hours(wait.gap.total_seconds())} after the wait expired",
                    at=wait.touched_at,
                    wake=wake[wait.touch],
                    evidence=[o.opened_by, wait.touch] if o.opened_by in when else [wait.touch],
                    pattern=self.pattern,
                )
            )
        return CheckReport(findings=findings)
