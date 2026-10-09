"""The simulated world's health: what kept the world from playing as its files declare it.

Always read, over every run, and never about the agent: each finding is a fact about the world Minutehand played
(a person it never let act, an answer it never booked, a model call for a person that failed, an event it pushed
that never arrived) or about how much of what the files declare the run reached. None is counted among the agent's
findings, and none is a judgement of how the agent should behave.

A kind in `domain.checks.INCOMPLETE` makes the run `SIMULATION_INCOMPLETE` (`checks.runner.verdict`): the world did
not play what was declared, so a pass or a failure on that run would be read against a world that never was. The
rest state coverage (a declared person, service or collection nothing touched; a scripted step whose ask never
came; an item pending on someone the files declare never acts) and change no verdict.

Which is which, and why:

| Kind | Incomplete | When |
|---|---|---|
| `responder_never_acts` | yes | An item of a declared service waited on a responder whose script ends in silence (`scripted`, `then: silent`) with no `takes` for the service: declared to respond, they never can. Declare the responder `{kind: silent}` (or list no responders) to say nobody decides |
| `waits_on_nobody` | yes | An item of a declared service sat in a state only a person moves it out of, with responders declared, and nobody held it pending |
| `owed_unbooked` | yes | Someone owes a move or an answer (they speak for themselves, or a take pins it) and no moment was booked, or a moment was booked and the run's table of what is due never held it |
| `model_failed` | yes | A people model call failed and no later call with the same context answered, or a move is still owed with a model's failure on it |
| `push_failed` | yes | An event the world pushed to the agent (a Slack event, a declared service's push) was not taken, after every retry its service makes |
| `waits_by_declaration` | no | An item waited on someone the files declare never acts: a `Silent` person, or one whose script ends in silence where no service declares them a responder |
| `never_exercised` | no | A declared service, `store` collection or person nothing in the run touched |
| `step_never_fired` | no | A scripted step whose ask never came |
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime

from minutehand.checks.facts import ended_at
from minutehand.domain.assessments import StoppedBy
from minutehand.domain.checks import INCOMPLETE, HealthFinding, HealthKind, RunView
from minutehand.domain.clock import DueKind
from minutehand.domain.people import Plan
from minutehand.domain.scenario import AfterScript, Answers, Person, Scenario, Scripted, Silent, WrittenScenario
from minutehand.domain.services import Machine, Trigger
from minutehand.domain.transitions import AUTOMATIC_REPLY
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    MessageSnapshot,
    Operation,
    PendingSnapshot,
    PendingStatus,
    PushSnapshot,
    ServiceItemSnapshot,
    ServiceRecordKind,
    ServiceRecordSnapshot,
    StoredSnapshot,
    WorldEvent,
)

CHECK = "simulation"
"""What the run's notes name these under."""


def _found(
    kind: HealthKind,
    words: str,
    *,
    person: str | None = None,
    entity: EntityRef | None = None,
    since: datetime | None = None,
    evidence: list[int] | None = None,
) -> HealthFinding:
    return HealthFinding(
        kind=kind,
        incomplete=kind in INCOMPLETE,
        words=words,
        person=person,
        entity=entity,
        since=since,
        evidence=evidence or [],
    )


def health(view: RunView) -> list[HealthFinding]:
    """Every fact about the world's health the run's record holds, in the order of `HealthKind`."""
    found = [
        *_pending(view),
        *_waits_on_nobody(view),
        *_model_failures(view),
        *_pushes(view),
        *_never_exercised(view),
        *_steps(view),
    ]
    order = list(HealthKind)
    return sorted(found, key=lambda f: order.index(f.kind))


def _speaks(person: Person) -> bool:
    """Whether the person decides for themselves where nothing pins what they do (`application.people._picks`)."""
    reply = person.reply
    if isinstance(reply, Silent):
        return False
    if isinstance(reply, Answers):
        return True
    return reply.then is AfterScript.ANSWERS


def _silenced(person: Person) -> bool:
    return isinstance(person.reply, Scripted) and person.reply.then is AfterScript.SILENT


class _Held:
    def __init__(self, first: WorldEvent, last: PendingSnapshot) -> None:
        self.first = first
        self.last = last


def _held(view: RunView) -> dict[EntityRef, _Held]:
    """Every record the people engine held, as it last stood, with the event it began at."""
    found: dict[EntityRef, _Held] = {}
    for e in view.events:
        if not isinstance(e.after, PendingSnapshot):
            continue
        if e.entity in found:
            found[e.entity].last = e.after
        else:
            found[e.entity] = _Held(e, e.after)
    return found


def _pending(view: RunView) -> Iterator[HealthFinding]:
    people = {p.key: p for p in view.scenario.people}
    responders = {r: s.key for s in view.scenario.services for r in s.responders}
    services = {s.key for s in view.scenario.services}
    booked = {d.due.ref for d in view.dues or [] if d.due.kind in (DueKind.PERSON_REPLY, DueKind.TRANSITION)}
    for ref, held in _held(view).items():
        p = held.last
        if p.status is not PendingStatus.PENDING or p.take == AUTOMATIC_REPLY or p.person not in people:
            continue
        person = people[p.person]
        since = held.first.sim_time
        what = f"{p.item.provider} {p.item.kind.value} {p.item.external_id}"
        evidence = [held.first.seq]
        if p.due_at is not None:
            if view.dues is not None and f"transition:{ref.external_id}" not in booked:
                yield _found(
                    HealthKind.OWED_UNBOOKED,
                    f"{p.person}'s move on {what} was due at {p.due_at.isoformat()} and the run's table of what is "
                    "due never held it",
                    person=p.person,
                    entity=p.item,
                    since=since,
                    evidence=evidence,
                )
            continue
        if p.asked is not None:
            continue  # an ask their script plans no answer to: the script's own word
        pinned = p.pinned is not None or p.take is not None
        on_service = p.item.provider in services and p.item.kind is EntityKind.SERVICE_ITEM
        if _speaks(person) or pinned:
            yield _found(
                HealthKind.OWED_UNBOOKED,
                f"{what} has waited on {p.person} since {since.isoformat()}, who acts on it, and no moment was ever "
                "booked for them to",
                person=p.person,
                entity=p.item,
                since=since,
                evidence=evidence,
            )
        elif on_service and _silenced(person) and responders.get(p.person) == p.item.provider:
            yield _found(
                HealthKind.RESPONDER_NEVER_ACTS,
                f"{p.person} is a responder of service {p.item.provider}, and their script ends in silence (`then: "
                f"silent`) with no `takes` for it, so {what} has waited on them since {since.isoformat()} and they "
                "can never act on it; to say nobody decides, declare them `{kind: silent}` or list no responders",
                person=p.person,
                entity=p.item,
                since=since,
                evidence=evidence,
            )
        else:
            said = "is declared silent" if isinstance(person.reply, Silent) else "has a script that ends in silence"
            yield _found(
                HealthKind.WAITS_BY_DECLARATION,
                f"{what} has waited on {p.person} since {since.isoformat()}, who {said}: by the files, nobody acts "
                "on it",
                person=p.person,
                entity=p.item,
                since=since,
                evidence=evidence,
            )


def _machines(view: RunView) -> dict[str, Machine]:
    found = {s.key: s.machine for s in view.scenario.services if s.machine is not None}
    for e in view.events:
        if (
            isinstance(e.after, ServiceRecordSnapshot)
            and e.after.record is ServiceRecordKind.MACHINE
            and e.after.service not in found
        ):
            found[e.after.service] = Machine.model_validate_json(e.after.text)
    return found


def _waits_on_nobody(view: RunView) -> Iterator[HealthFinding]:
    """A declared service's item left, at the last moment the engine looked, in a state only a person moves it out of,
    with responders declared, and held pending on nobody."""
    machines = _machines(view)
    responders = {s.key: s.responders for s in view.scenario.services}
    last = max((w.index for w in view.wakes), default=0)
    unlooked = view.stopped in (StoppedBy.AGENT_FAILED, StoppedBy.ENVIRONMENT_FAILED)
    items: dict[tuple[str, str], WorldEvent] = {}
    for e in view.events:
        if isinstance(e.after, ServiceItemSnapshot):
            key = (e.after.service, e.after.item)
            if key not in items or items[key].after != e.after:
                items[key] = e
    held = _held(view)
    for (service, item), entered in items.items():
        snap = entered.after
        assert isinstance(snap, ServiceItemSnapshot)
        if service not in machines or not responders.get(service) or (unlooked and entered.wake >= last):
            continue
        machine = machines[service]
        if not machine.legal(snap.state, Trigger.PERSON) or any(
            t for t in machine.transitions if snap.state in t.from_ and t.by is not Trigger.PERSON
        ):
            continue
        waiting = [
            h
            for h in held.values()
            if h.last.item.provider == service
            and h.last.item.external_id == item
            and h.first.seq >= entered.seq
            and h.last.status is PendingStatus.PENDING
        ]
        if waiting:
            continue
        yield _found(
            HealthKind.WAITS_ON_NOBODY,
            f"{service} item {item} has been {snap.state} since {entered.sim_time.isoformat()}, a state only a person "
            f"moves it out of, and it was never held pending on any of its responders ({', '.join(responders[service])})",
            entity=entered.entity,
            since=entered.sim_time,
            evidence=[entered.seq],
        )


def _model_failures(view: RunView) -> Iterator[HealthFinding]:
    answered = {c.key for c in view.person_calls if c.answer is not None}
    seen: set[str] = set()
    for call in view.person_calls:
        if call.failure is None or call.key in answered or call.key in seen:
            continue
        seen.add(call.key)
        who = call.person or call.service or "a declared service"
        yield _found(
            HealthKind.MODEL_FAILED,
            f"the model writing {call.wrote.value} for {who} failed at {call.sim_time.isoformat()} and was never "
            f"answered after: {call.failure}",
            person=call.person,
            entity=call.asked,
            since=call.sim_time,
        )
    calls = [c.asked for c in view.person_calls if c.failure is not None and c.key not in answered]
    for held in _held(view).values():
        p = held.last
        if p.status is PendingStatus.PENDING and p.failure is not None and p.item not in calls:
            yield _found(
                HealthKind.MODEL_FAILED,
                f"{p.person}'s move on {p.item.provider} {p.item.external_id} is still owed: {p.failure}",
                person=p.person,
                entity=p.item,
                since=held.first.sim_time,
                evidence=[held.first.seq],
            )


def _pushes(view: RunView) -> Iterator[HealthFinding]:
    """Each push whose last send was not taken: no later send of the same push (`attempt` one more) followed it."""
    sends = [e for e in view.events if isinstance(e.after, PushSnapshot)]
    for i, e in enumerate(sends):
        snap = e.after
        assert isinstance(snap, PushSnapshot)
        if snap.status is not None and 200 <= snap.status < 300:
            continue
        retried = any(
            isinstance(later.after, PushSnapshot)
            and later.after.service == snap.service
            and later.after.item == snap.item
            and later.after.attempt == snap.attempt + 1
            for later in sends[i + 1 :]
        )
        if retried:
            continue
        tries = [
            s.seq
            for s in sends
            if isinstance(s.after, PushSnapshot) and s.after.service == snap.service and s.after.item == snap.item
        ]
        answered = f"answered {snap.status}" if snap.status is not None else (snap.failure or "was not answered")
        yield _found(
            HealthKind.PUSH_FAILED,
            f"{snap.service} pushed {snap.item} to {snap.url} {snap.attempt + 1} time(s) and the agent never took it: "
            f"the last send {answered}",
            entity=e.entity,
            since=e.sim_time,
            evidence=tries if snap.attempt else [e.seq],
        )


def _never_exercised(view: RunView) -> Iterator[HealthFinding]:
    providers = {e.entity.provider for e in view.events if e.actor is not Actor.SCENARIO}
    for service in view.scenario.services:
        if service.key not in providers:
            yield _found(
                HealthKind.NEVER_EXERCISED,
                f"service {service.key} ({service.host}) was declared and nothing in the run touched it",
                entity=EntityRef(provider=service.key, kind=EntityKind.SERVICE_ITEM, external_id=service.host),
            )
    stored = {(e.after.host, e.after.collection) for e in view.events if isinstance(e.after, StoredSnapshot)}
    for c in view.collections:
        if (c.host, c.collection) not in stored:
            yield _found(
                HealthKind.NEVER_EXERCISED,
                f"collection {c.collection} of {c.host} was declared and held nothing in the run",
            )
    touched: set[str] = set()
    emails = {p.email: p.key for p in view.scenario.people}
    for e in view.events:
        if isinstance(e.after, MessageSnapshot) and e.operation is not Operation.READ:
            touched |= {emails[r] for r in e.after.recipient_emails if r in emails}
        if isinstance(e.after, PendingSnapshot):
            touched.add(e.after.person)
    touched |= {r.person for r in view.replies}
    for person in view.scenario.people:
        if person.key not in touched:
            yield _found(
                HealthKind.NEVER_EXERCISED,
                f"{person.key} was declared and was never written to, asked or heard from in the run",
                person=person.key,
            )


def _steps(view: RunView) -> Iterator[HealthFinding]:
    fired: set[tuple[str, int]] = set()
    for held in _held(view).values():
        if held.last.plan is None:
            continue
        plan = Plan.model_validate_json(held.last.plan)
        if plan.step is not None:
            fired.add((plan.person, plan.step.to_ask))
    ended = ended_at(view)
    for person in view.scenario.people:
        if not isinstance(person.reply, Scripted):
            continue
        for step in person.reply.replies:
            if (person.key, step.to_ask) not in fired:
                said = json.dumps(step.verbatim) if step.verbatim is not None else f"facts {step.facts}"
                yield _found(
                    HealthKind.STEP_NEVER_FIRED,
                    f"{person.key}'s scripted step for ask {step.to_ask} ({said}) never fired: the run ended at "
                    f"{ended.isoformat()} before that ask came",
                    person=person.key,
                )


def silent_responders(scenario: Scenario | WrittenScenario) -> list[str]:
    """What `minutehand validate` warns of before any run: each responder of a declared service who never acts on its
    items, by declaration (`Silent`) or because their script ends in silence with nothing pinned."""
    people = {p.key: p for p in scenario.people}
    said: list[str] = []
    for n, service in enumerate(scenario.services):
        for key in service.responders:
            person = people[key] if key in people else None
            if person is None:
                continue
            pinned = any(t.provider == service.key or t.provider is None for t in person.takes)
            if isinstance(person.reply, Silent):
                said.append(
                    f"services[{n}] ({service.key}): responder {key} is declared silent, so nobody ever acts on its "
                    "items; that is how to model a responder who never decides"
                )
            elif _silenced(person) and not pinned:
                said.append(
                    f"services[{n}] ({service.key}): responder {key}'s script ends in silence (`then: silent`) and no "
                    f"`takes` pins what they do here, so they can never act on its items and the run will be "
                    f"simulation_incomplete; declare {key} `{{kind: silent}}` to model a responder who never decides, "
                    "or `then: answers` for one who does"
                )
    return said


def never_decides(scenario: Scenario, service: str) -> bool:
    """Whether nobody can ever act on the declared service's items: no responders, or each one declared silent or
    scripted into silence with nothing pinned. Then no timing of the agent's against a decision is the agent's to
    answer for."""
    people = {p.key: p for p in scenario.people}
    declared = next((s for s in scenario.services if s.key == service), None)
    if declared is None:
        return False
    for key in declared.responders:
        person = people[key] if key in people else None
        if person is None:
            continue
        pinned = any(t.provider == service or t.provider is None for t in person.takes)
        if pinned or _speaks(person):
            return False
    return True
