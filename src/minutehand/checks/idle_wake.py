"""A wake in which the agent changed nothing in the world and nothing it was waiting on.

A wake can be right to change nothing (it looked, and nothing was due), so this
answers `review`. What it costs is the model call that learned nothing.
"""

from __future__ import annotations

from minutehand.checks._waits import blocked
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity


class IdleWake:
    id = "idle_wake"
    needs = frozenset({Needs.WAKES})
    pattern = "check_world_before_model"

    def run(self, view: RunView) -> CheckReport:
        missing = blocked(view, self.needs, self.id)
        if missing:
            return CheckReport(blocked=missing)
        return CheckReport(
            findings=[
                Finding(
                    check=self.id,
                    severity=Severity.INFORMATION,
                    kind=FindingKind.REVIEW,
                    message=f"wake {w.index} changed nothing in the world and nothing the agent was waiting on",
                    at=w.sim_time,
                    wake=w.index,
                    pattern=self.pattern,
                )
                for w in view.wakes
                if w.world_changes == 0 and not w.commitments_changed
            ]
        )
