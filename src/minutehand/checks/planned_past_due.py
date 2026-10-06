"""A wait fell due while the agent's own plan had it away, and it followed up in time only because something else
woke it.

Read from the run loop's table as the log recorded it (`RunView.dues`): the agent's own plan at a moment is the
earliest wake it had reported, booked or declared a rhythm for that was still in the table then. A follow-up that
came within the grace of a wait falling due passes `late_follow_up`; when the agent had planned to be away past
that grace, what brought it back was a person's reply, a happening or a direction, not its own plan, and a run
where nothing else arrives would find it late. Late or missing follow-ups are `late_follow_up` and `no_follow_up`,
which say the plan themselves; this check speaks only of the ones that were in time by luck.

It answers REVIEW: whether the agent meant to rely on being woken by the other wait's answer is a judgement.
Without a table (a captured run, a standing world) it does not run, and says so.
"""

from __future__ import annotations

from datetime import datetime

from minutehand.checks._waits import GRACE, blocked, chase, ended_at, plan_at
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.clock import AGENT_SOURCES, DueClosed, DueEntry, DueSource

_WOKEN_BY = {
    DueSource.REPLY: "a person's reply",
    DueSource.FATE: "a ticket's fate",
    DueSource.HAPPENING: "something a person did by themselves",
    DueSource.DIRECTION: "the owner's direction",
}


class PlannedPastDue:
    id = "planned_past_due"
    needs = frozenset({Needs.WORLD, Needs.OBLIGATIONS})
    pattern = "expiry_on_every_wait"

    def run(self, view: RunView) -> CheckReport:
        missing = blocked(view, self.needs, self.id)
        dues = view.dues
        if dues is None:
            missing.append(f"{self.id}: the run kept no table of what was due, so nothing says what the agent planned")
        if missing or dues is None:
            return CheckReport(blocked=missing)
        by_seq = {e.seq: e for e in view.events}
        ended = ended_at(view)
        findings: list[Finding] = []
        for o in view.obligations:
            for expiry in chase(o, by_seq, ended).expiries:
                if expiry.touch is None or expiry.touched_at is None or expiry.gap > GRACE:
                    continue
                plan = plan_at(dues, expiry.expired)
                if not plan.past_due:
                    continue
                whom = f" on {o.person}" if o.person else ""
                woken = _woken_by(dues, expiry.expired, expiry.touched_at)
                findings.append(
                    Finding(
                        check=self.id,
                        severity=Severity.WARNING,
                        kind=FindingKind.REVIEW,
                        message=f"wait{whom} fell due while {plan.said()}; it followed up in time only because "
                        f"{woken} woke it",
                        at=expiry.expired,
                        wake=by_seq[expiry.touch].wake,
                        evidence=[s for s in (o.opened_by, expiry.touch) if s in by_seq],
                        pattern=self.pattern,
                    )
                )
        return CheckReport(findings=findings)


def _woken_by(dues: list[DueEntry], since: datetime, until: datetime) -> str:
    """What the run loop dispatched between the wait falling due and the follow-up, other than the agent's own."""
    fired = [
        d
        for d in dues
        if d.closed is DueClosed.FIRED
        and d.source not in AGENT_SOURCES
        and d.closed_at is not None
        and since <= d.closed_at <= until
    ]
    names = list(dict.fromkeys(_WOKEN_BY[d.source] for d in fired if d.source in _WOKEN_BY))
    return " and ".join(names) if names else "something other than its own plan"
