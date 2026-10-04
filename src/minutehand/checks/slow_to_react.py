"""An answer landed, or the work was done, and the agent came back to it late or never."""

from __future__ import annotations

from minutehand.checks._waits import blocked, ended_at, reaction
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, ObligationKind, RunView, Severity


class SlowToReact:
    id = "slow_to_react"
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
        untimed = 0
        for o in view.obligations:
            r = reaction(o, when, ended)
            if r is None:
                untimed += o.settled_at is not None and o.person is None and o.entity is None
                continue
            if not r.slow:
                continue
            hours = r.gap.total_seconds() / 3600
            whom = f" from {o.person}" if o.person else ""
            what = f"answer{whom} landed" if o.kind is ObligationKind.ANSWER_FROM_PERSON else f"work{whom} was finished"
            message = (
                f"{what} and the agent came back to it {hours:.0f} hours later"
                if r.touch is not None
                else f"{what} and the agent never came back to it in the {hours:.0f} hours left"
            )
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.FAIL,
                    message=message,
                    at=r.touched_at or r.settled,
                    wake=wake[r.touch] if r.touch is not None and r.touch in wake else None,
                    evidence=[s for s in (o.opened_by, r.touch) if s is not None and s in when],
                    pattern=self.pattern,
                )
            )
        notes = [f"{untimed} settled obligation(s) name no person or entity; no reaction can be timed"] if untimed else []
        return CheckReport(findings=findings, notes=notes)
