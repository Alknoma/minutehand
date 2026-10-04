"""The scorecard: how much of the waiting was the world's, and how much the agent added."""

from __future__ import annotations

from datetime import datetime, timedelta

from minutehand.application.ledger import recipients
from minutehand.checks._waits import follow_up, reaction
from minutehand.domain.checks import Effectiveness, Finding, FindingKind, ObligationKind, PersonBurden, RunView
from minutehand.domain.world import Actor, Operation


def measure(view: RunView, findings: list[Finding], met: int, ended_at: datetime) -> Effectiveness:
    when = {e.seq: e.sim_time for e in view.events}
    due = made = late = 0
    lost = timedelta(0)
    slowest: timedelta | None = None
    for o in view.obligations:
        wait = follow_up(o, when, ended_at)
        if wait is None:
            continue                                   # settled, or the run ended, before it was due
        due += 1
        made += wait.touch is not None
        if wait.late:
            late += 1
            lost += wait.gap                           # never followed up: the whole stretch is the agent's
            if wait.touch is not None:
                slowest = wait.gap if slowest is None or wait.gap > slowest else slowest
    reactions = [r for r in (reaction(o, when, ended_at) for o in view.obligations) if r is not None]
    slow = [r for r in reactions if r.slow]
    lost += sum((r.gap for r in slow), timedelta(0))
    burden = _burden(view)
    sent = sum(b.messages for b in burden)
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
        reactions_due=len(reactions),
        reactions_slow=len(slow),
        slowest_reaction=max((r.gap for r in slow), default=None),
        messages_to_people=sent,
        burden=burden,
        messages_per_outcome=sent / met if met else None,
        wakes=len(view.wakes),
        idle_wakes=sum(1 for w in view.wakes if w.world_changes == 0 and not w.commitments_changed),
        failed_checks=sum(1 for f in findings if f.kind is FindingKind.FAIL),
    )


def _burden(view: RunView) -> list[PersonBurden]:
    """Messages the agent sent each person, and how many of them chased an ask still open."""
    messages = {p.key: 0 for p in view.scenario.people}
    chasers = {p.key: 0 for p in view.scenario.people}
    waits = [o for o in view.obligations if o.kind is not ObligationKind.DATE and o.person is not None]
    for event in view.events:
        if event.actor is not Actor.AGENT or event.operation is not Operation.CREATE:
            continue
        for person in recipients(event, view.scenario):
            messages[person.key] += 1
            if any(
                o.person == person.key and o.opened_by < event.seq
                and (o.settled_at is None or event.sim_time < o.settled_at)
                for o in waits
            ):
                chasers[person.key] += 1
    return [PersonBurden(person=k, messages=messages[k], follow_ups=chasers[k]) for k in messages]
