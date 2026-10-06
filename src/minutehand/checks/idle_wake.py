"""A wake in which the agent changed nothing in the world and nothing it was waiting on.

The finding states only what was observed: that the wake changed nothing, and how many model calls the run's
telemetry placed in it, or that the run received none to count. Why a wake was idle (nothing was due, a
too-early wake, a model asked to look when code could have) is not observed, so the finding does not say; the
pattern's page lists the causes and what each needs.

A wake that changed nothing and made no model call, by telemetry that reports model calls, cost nothing worth
reporting: it is noted, never raised. A wake can be right to change nothing, so a raised one is `review`.
"""

from __future__ import annotations

from minutehand.checks._waits import blocked
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity, WakeRecord

NOTHING = "changed nothing in the world and nothing the agent was waiting on"


class IdleWake:
    id = "idle_wake"
    needs = frozenset({Needs.WAKES})
    pattern = "check_world_before_model"

    def run(self, view: RunView) -> CheckReport:
        missing = blocked(view, self.needs, self.id)
        if missing:
            return CheckReport(blocked=missing)
        calls = {c.wake: c.calls for c in view.model_calls} if view.model_calls is not None else None
        findings: list[Finding] = []
        notes: list[str] = []
        for wake in (w for w in view.wakes if w.world_changes == 0 and not w.commitments_changed):
            if calls is None:
                findings.append(
                    self._finding(
                        wake, "no telemetry of the agent's model calls was received, so how many it made is not known"
                    )
                )
                continue
            made = calls[wake.index] if wake.index in calls else 0
            if made == 0:
                notes.append(f"wake {wake.index} {NOTHING}, and made no model call: not wasted effort")
                continue
            were = "model call was" if made == 1 else "model calls were"
            findings.append(self._finding(wake, f"{made} {were} received or recorded during it"))
        return CheckReport(findings=findings, notes=notes)

    def _finding(self, wake: WakeRecord, calls: str) -> Finding:
        return Finding(
            check=self.id,
            severity=Severity.INFORMATION,
            kind=FindingKind.REVIEW,
            message=f"wake {wake.index} {NOTHING}; {calls}",
            at=wake.sim_time,
            wake=wake.index,
            pattern=self.pattern,
        )
