"""A wait passed its expected date, is still open, and the agent never touched it again."""

from __future__ import annotations

from minutehand.checks._waits import blocked, ended_at, follow_up
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, ObligationKind, RunView, Severity


class NoFollowUp:
    id = "no_follow_up"
    needs = frozenset({Needs.WORLD, Needs.OBLIGATIONS})
    pattern = "expiry_on_every_wait"

    def run(self, view: RunView) -> CheckReport:
        missing = blocked(view, self.needs, self.id)
        if missing:
            return CheckReport(blocked=missing)
        when = {e.seq: e.sim_time for e in view.events}
        ended = ended_at(view)
        findings: list[Finding] = []
        for o in view.obligations:
            if o.kind is ObligationKind.DATE or o.settled_at is not None:
                continue
            wait = follow_up(o, when, ended)
            if wait is None or wait.touch is not None:
                continue
            whom = f" on {o.person}" if o.person else ""
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.ERROR,
                    kind=FindingKind.FAIL,
                    message=f"wait{whom} expired {wait.gap.days} days {wait.gap.seconds // 3600} hours before the run"
                    " ended and the agent never came back to it",
                    at=wait.expired,
                    evidence=[o.opened_by] if o.opened_by in when else [],
                    pattern=self.pattern,
                )
            )
        return CheckReport(findings=findings)
