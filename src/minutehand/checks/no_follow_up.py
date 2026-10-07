"""A wait fell due and the agent never came back to it after that: it is still open, or it settled only later,
by itself, with nothing from the agent in between.

What the agent did before then is said, not hidden: an agent that reminded a
silent person once, early, and then let the wait sit has followed up once and
then abandoned it, and the finding says when the one follow-up went out.
"""

from __future__ import annotations

from minutehand.checks._waits import Chase, Expiry, blocked, chase, ended_at, plan_at, span
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, ObligationKind, RunView, Severity

_TIMES = {1: "once", 2: "twice"}


class NoFollowUp:
    id = "no_follow_up"
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
            if o.kind is ObligationKind.DATE:
                continue
            chased = chase(o, by_seq, ended)
            left = chased.abandoned
            if left is None or not left.late:
                continue
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.ERROR,
                    kind=FindingKind.FAIL,
                    message=_message(chased, left)
                    + (
                        f"; when it fell due, {plan_at(view.dues, left.expired, since=chased.since(left.expired)).said()}"
                        if view.dues is not None
                        else ""
                    ),
                    at=left.expired,
                    evidence=[s for s in (o.opened_by, *chased.follow_ups) if s in by_seq],
                    pattern=self.pattern,
                )
            )
        return CheckReport(findings=findings)


def _message(chased: Chase, left: Expiry) -> str:
    o = chased.obligation
    whom = f" on {o.person}" if o.person else ""
    if o.settled_at is not None:
        return (
            f"wait{whom} fell due and the agent did not follow it up in the {span(left.gap)} before it settled by "
            "itself"
        )
    if not chased.follow_ups:
        return f"wait{whom} expired {span(left.gap)} before the run ended and the agent never came back to it"
    count = len(chased.follow_ups)
    times = _TIMES.get(count, f"{count} times")
    since = "ask" if o.kind is ObligationKind.ANSWER_FROM_PERSON else "hand-off"
    last = chased.follow_up_times[-1] - o.opened_at
    return (
        f"wait{whom}: the agent followed up {times}, the last {span(last)} after the {since}, then nothing; "
        f"due again {span(left.expired - o.opened_at)} after the {since}, it sat {span(left.gap)} "
        "until the run ended"
    )
