"""The scorecard: how much of the waiting was the world's, and how much the agent added."""

from __future__ import annotations

from datetime import datetime, timedelta

from minutehand.domain.checks import Effectiveness, Finding, FindingKind, RunView

GRACE = timedelta(hours=1)


def measure(view: RunView, findings: list[Finding], met: int, ended_at: datetime) -> Effectiveness:
    when = {e.seq: e.sim_time for e in view.events}
    due = made = late = 0
    lost = timedelta(0)
    slowest: timedelta | None = None
    for o in view.obligations:
        closes = o.settled_at or ended_at
        if o.expected_by is None or o.expected_by >= closes:
            continue                                   # settled, or the run ended, before it was due
        due += 1
        expired = max(o.expected_by, o.opened_at)
        after = sorted(when[s] for s in o.agent_touches if when[s] >= expired)
        if not after:
            lost += closes - expired                   # never followed up: the whole stretch is the agent's
            late += 1
            continue
        made += 1
        gap = after[0] - expired
        if gap > GRACE:
            late += 1
            lost += gap
            slowest = gap if slowest is None or gap > slowest else slowest
    return Effectiveness(
        expectations_met=met,
        expectations_total=len(view.scenario.expect),
        waits_opened=len(view.obligations),
        waits_open_at_end=sum(1 for o in view.obligations if o.settled_at is None),
        follow_ups_due=due,
        follow_ups_made=made,
        follow_ups_late=late,
        time_lost=lost,
        slowest_follow_up=slowest,
        wakes=len(view.wakes),
        idle_wakes=sum(1 for w in view.wakes if w.world_changes == 0 and not w.commitments_changed),
        failed_checks=sum(1 for f in findings if f.kind is FindingKind.FAIL),
    )
