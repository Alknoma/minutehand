"""The scorecard: how much of the waiting was the world's, and how much the agent added."""

from __future__ import annotations

from datetime import datetime, timedelta

from minutehand.checks._waits import chases, reaction
from minutehand.checks.ledger import recipients
from minutehand.domain.checks import Effectiveness, Finding, FindingKind, ObligationKind, PersonBurden, RunView
from minutehand.domain.world import Actor, EntityRef, MessageSnapshot, Operation, WorldEvent


def measure(view: RunView, findings: list[Finding], met: int, ended_at: datetime) -> Effectiveness:
    when = {e.seq: e.sim_time for e in view.events}
    waits = [o for o in view.obligations if o.kind is not ObligationKind.DATE]
    due = late = 0
    made: set[int] = set()
    early: set[int] = set()
    lost = timedelta(0)
    slowest: timedelta | None = None
    for chased in chases(view, ended_at):
        made.update(chased.follow_ups)  # one message chasing two waits is one follow-up
        early.update(chased.early)
        for expiry in chased.expiries:
            due += 1
            if not expiry.late:
                continue
            late += 1
            lost += expiry.gap  # never followed up: the whole stretch is the agent's
            if expiry.touch is not None:
                slowest = expiry.gap if slowest is None or expiry.gap > slowest else slowest
    reactions = [r for r in (reaction(o, when, ended_at) for o in view.obligations) if r is not None]
    slow = [r for r in reactions if r.slow]
    lost += sum((r.gap for r in slow), timedelta(0))
    burden = _burden(view)
    sent = sum(b.messages for b in burden)
    edited, deleted = rewrites(view)
    return Effectiveness(
        expectations_met=met,
        expectations_total=len(view.scenario.expect),
        waits_opened=len(waits),
        waits_open_at_end=sum(1 for o in waits if o.settled_at is None),
        follow_ups_due=due,
        follow_ups_made=len(made),
        follow_ups_late=late,
        follow_ups_early=len(early),
        time_lost=lost,
        slowest_follow_up=slowest,
        reactions_due=len(reactions),
        reactions_slow=len(slow),
        slowest_reaction=max((r.gap for r in slow), default=None),
        messages_to_people=sent,
        messages_edited=len(edited),
        messages_deleted=len(deleted),
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
                o.person == person.key
                and o.opened_by < event.seq
                and (o.settled_at is None or event.sim_time < o.settled_at)
                for o in waits
            ):
                chasers[person.key] += 1
    return [PersonBurden(person=k, messages=messages[k], follow_ups=chasers[k]) for k in messages]


def rewrites(view: RunView) -> tuple[list[WorldEvent], list[WorldEvent]]:
    """The agent's messages to people rewritten in place after they were sent (an update whose text differs from
    what the message said before), and those it deleted. Judged by nothing: counted, so the scorecard shows them."""
    said: dict[EntityRef, str] = {}
    to_people: set[EntityRef] = set()
    edited: list[WorldEvent] = []
    deleted: list[WorldEvent] = []
    for event in view.events:
        after = event.after
        if event.operation is Operation.DELETE:
            if event.actor is Actor.AGENT and event.entity in to_people:
                deleted.append(event)
            continue
        if not isinstance(after, MessageSnapshot):
            continue
        before = said.get(event.entity)
        said[event.entity] = after.text
        if event.actor is not Actor.AGENT:
            continue
        if event.operation is Operation.CREATE and recipients(event, view.scenario):
            to_people.add(event.entity)
        elif event.operation is Operation.UPDATE and event.entity in to_people and before != after.text:
            edited.append(event)
    return edited, deleted
