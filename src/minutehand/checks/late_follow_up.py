"""A wait fell due and the agent did follow up, but later than a grace after it.

A wait falls due at its expected date and again its patience after each follow-up
(`checks/_waits.py`), so one wait can be late more than once.

The defect this exists for: on a captured run a reminder went out 33 hours after the
wait it chased had expired. The agent did follow up, so a check for silence would
pass it; the cost is the stretch between expiry and the reminder.
"""

from __future__ import annotations

from minutehand.checks._waits import GRACE, blocked, chase, ended_at
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
        by_seq = {e.seq: e for e in view.events}
        ended = ended_at(view)
        findings: list[Finding] = []
        for o in view.obligations:
            for expiry in chase(o, by_seq, ended).expiries:
                if expiry.touch is None or expiry.touched_at is None or expiry.gap <= GRACE:
                    continue
                findings.append(
                    Finding(
                        check=self.id,
                        severity=Severity.WARNING,
                        kind=FindingKind.FAIL,
                        message=f"followed up {_hours(expiry.gap.total_seconds())} after the wait expired",
                        at=expiry.touched_at,
                        wake=by_seq[expiry.touch].wake,
                        evidence=[o.opened_by, expiry.touch] if o.opened_by in by_seq else [expiry.touch],
                        pattern=self.pattern,
                    )
                )
        return CheckReport(findings=findings)
