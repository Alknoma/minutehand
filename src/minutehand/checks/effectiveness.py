"""The scorecard: how much of the waiting was the world's, and how much the agent added."""

from __future__ import annotations

from datetime import datetime

from minutehand.checks.facts import asks
from minutehand.checks.ledger import recipients
from minutehand.domain.checks import Effectiveness, Finding, FindingKind, ObligationKind, PersonBurden, RunView
from minutehand.domain.world import (
    Actor,
    EntityRef,
    InboxItemSnapshot,
    ItemStatus,
    MessageSnapshot,
    Operation,
    WorldEvent,
)


def measure(view: RunView, findings: list[Finding], met: int, ended_at: datetime) -> Effectiveness:
    """Counts and times read from the world, none of them measured against what anyone thinks the agent should
    have done: that is the team's rules (`domain/assessments.py`)."""
    waits = [o for o in view.obligations if o.kind is not ObligationKind.DATE]
    made: set[int] = set()
    for kind in (ObligationKind.ANSWER_FROM_PERSON, ObligationKind.WORK_WITH_PERSON):
        for ask in asks(view, kind):
            made.update(s for f in ask.follow_ups for s in f.seqs)  # one message chasing two waits is one follow-up
    when = {e.seq: e.sim_time for e in view.events}
    reactions = [
        (when[o.first_touch_after_settled] if o.first_touch_after_settled in when else max(ended_at, o.settled_at))
        - o.settled_at
        for o in waits
        if o.settled_at is not None and (o.person is not None or o.entity is not None)
    ]
    burden = _burden(view)
    sent = sum(b.messages for b in burden)
    edited, deleted = rewrites(view)
    asked, decided, pending = decisions(view)
    return Effectiveness(
        expectations_met=met,
        expectations_total=len(view.scenario.expect),
        waits_opened=len(waits),
        waits_open_at_end=sum(1 for o in waits if o.settled_at is None),
        follow_ups_made=len(made),
        waits_settled=len(reactions),
        slowest_reaction=max(reactions, default=None),
        messages_to_people=sent,
        messages_edited=len(edited),
        messages_deleted=len(deleted),
        decisions_asked=asked,
        decisions_made=decided,
        decisions_pending=pending,
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


def decisions(view: RunView) -> tuple[int, int, int]:
    """Items the agent left waiting on a person in its own product, those the person decided, and those still
    waiting when the run ended; one taken back by the agent is none of the last two."""
    asked: set[EntityRef] = set()
    decided: set[EntityRef] = set()
    last: dict[EntityRef, ItemStatus] = {}
    for event in view.events:
        after = event.after
        if not isinstance(after, InboxItemSnapshot) or after.person is None:
            continue
        if event.actor is Actor.AGENT and event.operation is Operation.CREATE:
            asked.add(event.entity)
        if event.actor is Actor.PERSON and after.status is ItemStatus.DECIDED:
            decided.add(event.entity)
        last[event.entity] = after.status
    pending = sum(1 for ref in asked if last[ref] is ItemStatus.PENDING)
    return len(asked), len(decided & asked), pending
