"""The agent's effects, assessed by kind of item against the declared world, with nothing for anyone to write: the
deterministic half of "What every run is assessed on" (`docs/assessments.md`). The model's half, for what only
meaning can tell, is `checks/judged/review.py`.

Each provider declares its item types and the checks each takes (`Manifest.item_types`, `domain.items`); a declared
service's items and a declared store's records take the built-in ones. Every check here reads only the record and
what the scenario and the agent file already say: the deadline, the people's reply windows, working hours,
absences and delegates, a declared service's machine, the agent's declared rhythm. Each finding names the
declaration it was measured against (`Assessed.against`) and cites its evidence: the events (`evidence`) and the
agent's calls (`calls`). How each kind of finding counts toward the verdict is `domain.items.FINDING`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from minutehand.checks.facts import Called, body_content, calls, messages, span, transitions, writes
from minutehand.checks.ledger import absences
from minutehand.domain.checks import (
    CheckReport,
    Finding,
    FindingKind,
    Needs,
    Obligation,
    ObligationKind,
    RunView,
    Severity,
)
from minutehand.domain.items import (
    BUILT_IN,
    FINDING,
    Assessed,
    Counts,
    ItemCheck,
    ItemKind,
    ItemType,
    TypedItem,
    stretch_in_hours,
)
from minutehand.domain.scenario import Person
from minutehand.domain.services import Machine, Trigger
from minutehand.domain.world import (
    Actor,
    CaptureMode,
    EntityKind,
    EntityRef,
    Operation,
    RecordedCall,
    ServiceRecordKind,
    ServiceRecordSnapshot,
    WorldEvent,
)

_WRITES = frozenset({Operation.CREATE, Operation.UPDATE, Operation.DELETE})
_BOOKKEEPING = frozenset(
    {
        EntityKind.DUE,
        EntityKind.PENDING,
        EntityKind.MEMORY,
        EntityKind.NEXT_WAKE,
        EntityKind.SERVICE_RECORD,
        EntityKind.CHANNEL,
        EntityKind.PUSH,
    }
)
"""What changes no item a person could learn anything new from: the run loop's table, the people engine's holdings,
the agent's own memory and plan, a service's fixed records and pushes."""
_SAID = frozenset({ItemKind.CHAT_MESSAGE, ItemKind.EMAIL, ItemKind.COMMENT})
_READS = frozenset({"GET", "HEAD"})
_WORD = re.compile(r"\w+")


def type_of(view: RunView, typed: TypedItem) -> ItemType:
    """The item type an item's holder declares for its kind; a declared service's or store's built-in one."""
    for provided in view.item_types:
        if provided.provider == typed.provider:
            found = next((t for t in provided.types if t.kind is typed.kind), None)
            if found is not None:
                return found
    built_in = next((t for t in BUILT_IN if t.kind is typed.kind), None)
    if built_in is not None:
        return built_in
    return ItemType(kind=typed.kind, entity=EntityKind.RECORD)


def agent_calls(view: RunView) -> list[tuple[int, RecordedCall]]:
    """The agent's own calls, each with its place among the run's calls (from 1): neither Minutehand's as a person
    nor a tunnel relayed unopened."""
    return [
        (n, c)
        for n, c in enumerate(view.calls or [], start=1)
        if c.exchange.inbox_call is None and c.exchange.tunnelled is None
    ]


def _plain(text: str) -> str:
    return " ".join(_WORD.findall(text.casefold()))


def _ago(delta: timedelta) -> str:
    """A stretch as findings say it: in minutes under two hours, else as `facts.span` does."""
    minutes = round(abs(delta).total_seconds() / 60)
    if minutes < 120:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return span(delta)


def _when(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M} UTC"


@dataclass
class _Found:
    check: ItemCheck
    item: ItemKind | None
    message: str
    against: str
    at: datetime | None
    evidence: list[int]
    calls: list[int]


class ItemChecks:
    """Every deterministic check of the agent's effects, by the kind of item each was on. Always run: nothing turns
    it on, and a run with no effect has nothing for it to say."""

    id = "items"
    needs = frozenset({Needs.WORLD})

    def run(self, view: RunView) -> CheckReport:
        reader = _Reader(view)
        found = [
            *reader.after_deadline(),
            *reader.duplicates(),
            *reader.inside_reply_window(),
            *reader.to_someone_away(),
            *reader.breaks_thread(),
            *reader.duplicate_tickets(),
            *reader.stale_state(),
            *reader.calendar(),
            *reader.refused_moves(),
            *reader.abandoned(),
            *reader.late_reactions(),
            *reader.written_twice(),
            *reader.redundant_reads(),
            *reader.before_decision(),
            *reader.deadline_missed(),
        ]
        found += reader.repeated_without_news(
            {e for f in found if f.check is ItemCheck.DUPLICATE for e in f.evidence[-1:]}
        )
        return CheckReport(
            findings=[_finding(f) for f in sorted(found, key=lambda f: (f.at or view.scenario.starts_at, f.check))]
        )


def _finding(found: _Found) -> Finding:
    counted = FINDING[found.check]
    fails = counted.counts is Counts.FAIL
    return Finding(
        check=found.check.value,
        severity=Severity.ERROR if fails else Severity.WARNING,
        kind=FindingKind.FAIL if fails else FindingKind.REVIEW,
        message=found.message,
        at=found.at,
        evidence=sorted(set(found.evidence)),
        calls=sorted(set(found.calls)),
        assessed=Assessed(kind=counted.kind, item=found.item, against=found.against),
    )


class _Reader:
    def __init__(self, view: RunView) -> None:
        self.view = view
        self.by_email = {p.email.casefold(): p for p in view.scenario.people}
        self.by_key = {p.key: p for p in view.scenario.people}
        self.typed = view.typed
        self.by_seq = {t.seq: t for t in view.typed}
        self.mine = [t for t in view.typed if t.actor is Actor.AGENT]
        self.calls = agent_calls(view)
        self.services = {s.key: s for s in view.scenario.services}
        self.hosts = {s.host: s for s in view.scenario.services}

    def takes(self, typed: TypedItem, check: ItemCheck) -> bool:
        return check in type_of(self.view, typed).checks

    def person(self, email: str) -> Person | None:
        return self.by_email.get(email.casefold())

    # -- every kind ---------------------------------------------------------------------------------------------

    def after_deadline(self) -> Iterable[_Found]:
        deadline = self.view.scenario.deadline
        if deadline is None:
            return
        for t in self.mine:
            if t.at > deadline and self.takes(t, ItemCheck.AFTER_DEADLINE):
                yield _Found(
                    ItemCheck.AFTER_DEADLINE,
                    t.kind,
                    f"{t.kind.value.replace('_', ' ')} {t.operation.value}d at {_when(t.at)}, {_ago(t.at - deadline)} "
                    f"after the scenario's deadline",
                    f"the scenario's deadline, {_when(deadline)}",
                    t.at,
                    [t.seq],
                    [],
                )

    # -- what is said: chat messages, emails, comments ----------------------------------------------------------

    def duplicates(self) -> Iterable[_Found]:
        """The same words again in one conversation, with nobody else saying anything between."""
        said = [t for t in self.typed if t.kind in _SAID and t.operation is Operation.CREATE]
        for i, t in enumerate(said):
            if t.actor is not Actor.AGENT or not self.takes(t, ItemCheck.DUPLICATE) or not _plain(t.text):
                continue
            place = (t.provider, t.conversation, t.thread_of)
            for earlier in reversed(said[:i]):
                if (earlier.provider, earlier.conversation, earlier.thread_of) != place:
                    continue
                if earlier.actor is not Actor.AGENT:
                    break  # someone else spoke between: the same words again may answer them
                if _plain(earlier.text) == _plain(t.text):
                    yield _Found(
                        ItemCheck.DUPLICATE,
                        t.kind,
                        f"the same {t.kind.value.replace('_', ' ')} sent again at {_when(t.at)} (first at "
                        f"{_when(earlier.at)}, seq {earlier.seq}), with nothing said between: {_quote(t.text)}",
                        "the conversation as it stood: nobody had said anything since the first",
                        t.at,
                        [earlier.seq, t.seq],
                        [],
                    )
                    break

    def inside_reply_window(self) -> Iterable[_Found]:
        """A follow-up sent before the person's declared time to answer had passed since the last message on the
        wait. A person who declares `reminded` answers sooner for a follow-up, so chasing them is theirs to allow."""
        for o in self._asks():
            person = self.by_key.get(o.person or "")
            if person is None or person.reminded is not None or o.patience is None:
                continue
            last = o.opened_at
            for seq in o.agent_touches:
                t = self.by_seq.get(seq)
                if t is None or t.kind not in _SAID or t.operation is not Operation.CREATE:
                    continue
                if o.settled_at is not None and t.at >= o.settled_at:
                    break
                if not self.takes(t, ItemCheck.INSIDE_REPLY_WINDOW):
                    continue
                due = last + o.patience
                if t.at < due:
                    yield _Found(
                        ItemCheck.INSIDE_REPLY_WINDOW,
                        t.kind,
                        f"{person.key} was chased at {_when(t.at)}, {_ago(t.at - last)} after the last message on the "
                        f"ask, before their declared time to answer ran out at {_when(due)}",
                        _window_of(person),
                        t.at,
                        [o.opened_by, t.seq],
                        [],
                    )
                last = t.at

    def to_someone_away(self) -> Iterable[_Found]:
        for sent in messages(self.view):
            t = self.by_seq.get(sent.event.seq)
            if t is None or not sent.to_away or not self.takes(t, ItemCheck.TO_SOMEONE_AWAY):
                continue
            for key in sent.to_away:
                person = self.by_key[key]
                delegates = sorted({a.delegate for a in person.absences if a.delegate is not None})
                yield _Found(
                    ItemCheck.TO_SOMEONE_AWAY,
                    t.kind,
                    f"written to {key} at {_when(t.at)} while they were away, with {', '.join(delegates)} covering",
                    f"{key}'s declared absence and its delegate",
                    t.at,
                    [t.seq],
                    [],
                )

    def breaks_thread(self) -> Iterable[_Found]:
        """An email asking a person again outside the thread of an ask of theirs still open there."""
        asks = self._asks()
        for i, later in enumerate(asks):
            t = self.by_seq.get(later.opened_by)
            if t is None or t.kind is not ItemKind.EMAIL or not self.takes(t, ItemCheck.BREAKS_THREAD):
                continue
            for earlier in asks[:i]:
                same = earlier.person == later.person and earlier.entity is not None and later.entity is not None
                if not same or earlier.entity is None or earlier.entity.provider != t.provider:
                    continue
                if earlier.settled_at is None or earlier.settled_at > later.opened_at:
                    yield _Found(
                        ItemCheck.BREAKS_THREAD,
                        t.kind,
                        f"{later.person} was emailed at {_when(t.at)} outside the thread of the ask still open since "
                        f"{_when(earlier.opened_at)} (seq {earlier.opened_by})",
                        f"the open ask of {later.person}, in its own thread",
                        t.at,
                        [earlier.opened_by, t.seq],
                        [],
                    )
                    break

    def repeated_without_news(self, duplicated: set[int]) -> list[_Found]:
        """A message telling a person something again, neither an ask of theirs nor a follow-up on one, when nothing
        new happened since the agent last wrote to them: no word from anyone, no move of any item, no write of the
        agent's other than messages."""
        asked = {o.opened_by for o in self.view.obligations} | {
            s for o in self.view.obligations for s in o.agent_touches
        }
        events = sorted(self.view.events, key=lambda e: e.seq)
        found: list[_Found] = []
        last: dict[str, TypedItem] = {}
        for t in [t for t in self.mine if t.kind in _SAID and t.operation is Operation.CREATE]:
            for email in t.people:
                person = self.person(email)
                if person is None:
                    continue
                before = last.get(person.key)
                last[person.key] = t
                if (
                    before is None
                    or t.seq in asked
                    or t.seq in duplicated
                    or not self.takes(t, ItemCheck.REPEATED_WITHOUT_NEWS)
                    or _news(events, before.seq, t.seq, self.by_seq)
                ):
                    continue
                found.append(
                    _Found(
                        ItemCheck.REPEATED_WITHOUT_NEWS,
                        t.kind,
                        f"{person.key} was written to again at {_when(t.at)}, {_ago(t.at - before.at)} after the last "
                        f"message to them (seq {before.seq}), with nothing new since: {_quote(t.text)}",
                        f"the world as it stood since seq {before.seq}: no word from anyone, no move of any item",
                        t.at,
                        [before.seq, t.seq],
                        [],
                    )
                )
        return found

    # -- tickets, documents ------------------------------------------------------------------------------------

    def duplicate_tickets(self) -> Iterable[_Found]:
        for w in writes(self.view):
            t = self.by_seq.get(w.event.seq)
            if t is None or not w.repeats_open_ticket or not self.takes(t, ItemCheck.DUPLICATE_TICKET):
                continue
            yield _Found(
                ItemCheck.DUPLICATE_TICKET,
                t.kind,
                f"a ticket filed at {_when(t.at)} with the title of one still open in the same project: "
                f"{_quote(t.text)}",
                "the tracker as it stood: a ticket of that title was open",
                t.at,
                [t.seq],
                [],
            )

    def stale_state(self) -> Iterable[_Found]:
        """A write over a version someone else made after the agent last touched the item: read it, wrote it, or
        called a route naming it (`TypedItem.last_read`)."""
        events = sorted(self.view.events, key=lambda e: e.seq)
        for t in self.mine:
            if t.operation not in (Operation.UPDATE, Operation.DELETE) or t.last_read is None:
                continue
            if not self.takes(t, ItemCheck.STALE_STATE):
                continue
            seen = t.last_read
            newer = [
                e
                for e in events
                if e.entity == t.item and seen < e.seq < t.seq and e.actor is not Actor.AGENT and e.operation in _WRITES
            ]
            if newer:
                yield _Found(
                    ItemCheck.STALE_STATE,
                    t.kind,
                    f"{t.kind.value.replace('_', ' ')} {t.item.external_id} written at {_when(t.at)} over a change "
                    f"{_actor(newer[-1])} made at {_when(newer[-1].sim_time)} that the agent had not read since",
                    f"the {t.kind.value.replace('_', ' ')} as it stood after seq {newer[-1].seq}; the agent last "
                    f"read it at seq {seen}",
                    t.at,
                    [seen, newer[-1].seq, t.seq],
                    [],
                )

    # -- calendar events ---------------------------------------------------------------------------------------

    def calendar(self) -> Iterable[_Found]:
        events = [t for t in self.typed if t.kind is ItemKind.CALENDAR_EVENT]
        away = absences(self.view.scenario, self.view.events)
        held: dict[str, TypedItem] = {}
        for t in events:
            before = held.get(t.item.external_id)
            if t.operation is Operation.DELETE:
                held.pop(t.item.external_id, None)
                continue
            held[t.item.external_id] = t
            if t.actor is not Actor.AGENT or t.starts is None or t.ends is None:
                continue
            starts, ends = t.starts, t.ends
            if self.takes(t, ItemCheck.OUTSIDE_WORKING_HOURS):
                for email in t.people:
                    person = self.person(email)
                    if person is None:
                        continue
                    if person.working_hours is not None and not stretch_in_hours(starts, ends, person.working_hours):
                        yield _Found(
                            ItemCheck.OUTSIDE_WORKING_HOURS,
                            t.kind,
                            f"an event set for {_when(starts)} to {_when(ends)}, outside {person.key}'s working hours",
                            f"{person.key}'s working_hours ({person.working_hours.opens:%H:%M}-"
                            f"{person.working_hours.closes:%H:%M} {person.working_hours.timezone})",
                            t.at,
                            [t.seq],
                            [],
                        )
                    for a in away:
                        if a.person == person.key and a.starts < ends and starts < a.ends:
                            yield _Found(
                                ItemCheck.OUTSIDE_WORKING_HOURS,
                                t.kind,
                                f"an event set for {_when(starts)} to {_when(ends)}, while {person.key} is away "
                                f"({_when(a.starts)} to {_when(a.ends)})",
                                f"{person.key}'s declared absence",
                                t.at,
                                [t.seq],
                                [],
                            )
            if self.takes(t, ItemCheck.DOUBLE_BOOKED):
                for other in held.values():
                    if other.item == t.item or other.starts is None or other.ends is None:
                        continue
                    shared = sorted({e.casefold() for e in t.people} & {e.casefold() for e in other.people})
                    if shared and other.starts < ends and starts < other.ends:
                        yield _Found(
                            ItemCheck.DOUBLE_BOOKED,
                            t.kind,
                            f"an event set for {_when(starts)} to {_when(ends)} over another event "
                            f"({_when(other.starts)} to {_when(other.ends)}, seq {other.seq}) {', '.join(shared)} "
                            "already has",
                            f"the calendars as they stood: {', '.join(shared)} already had the event of seq {other.seq}",
                            t.at,
                            [other.seq, t.seq],
                            [],
                        )
            moved = before is not None and (before.starts, before.ends) != (starts, ends)
            if moved and before is not None and self.takes(t, ItemCheck.MOVED_WITHOUT_NOTICE):
                told = t.notified is True or (
                    t.notified is None
                    and any(
                        m.kind in (ItemKind.CHAT_MESSAGE, ItemKind.EMAIL)
                        and self._wake(m.seq) == self._wake(t.seq)
                        and {e.casefold() for e in m.people} & {e.casefold() for e in t.people}
                        for m in self.mine
                    )
                )
                if not told:
                    yield _Found(
                        ItemCheck.MOVED_WITHOUT_NOTICE,
                        t.kind,
                        f"an event moved at {_when(t.at)} from {_when(before.starts or t.at)} to {_when(starts)} with "
                        "no word to its attendees",
                        "its attendees, who had it at the earlier time",
                        t.at,
                        [before.seq, t.seq],
                        [],
                    )

    # -- declared services ------------------------------------------------------------------------------------

    def refused_moves(self) -> Iterable[_Found]:
        """A write to a declared service its machine refused."""
        for n, c in self.calls:
            x = c.exchange
            service = self.hosts.get(x.host)
            if service is None or x.captured is None or x.captured.mode is not CaptureMode.SERVICE:
                continue
            if x.method in _READS or not 400 <= x.status < 500:
                continue
            evidence = list(range(c.first_seq, c.last_seq + 1)) if c.first_seq <= c.last_seq else []
            yield _Found(
                ItemCheck.REFUSED_MOVE,
                ItemKind.SERVICE_ITEM,
                f"{x.method} {x.path} to {service.key} was refused {x.status} at {_when(c.sim_time)}: "
                f"{_quote(x.response_body or '')}",
                f"the service {service.key}'s machine{_machine_words(self.view, service.key)}",
                c.sim_time,
                evidence,
                [n],
            )

    def abandoned(self) -> Iterable[_Found]:
        """An item the agent filed, left at the end in a state only the agent could move it on from."""
        last: dict[tuple[str, str], tuple[WorldEvent, str]] = {}
        filed: set[tuple[str, str]] = set()
        moved_by_agent: dict[tuple[str, str], int] = {}
        for m in transitions(self.view):
            t = m.transition
            if t.provider not in self.services:
                continue
            key = (t.provider, t.item.external_id)
            if t.from_state is None and t.by is Actor.AGENT:
                filed.add(key)
            last[key] = (m.event, t.to_state)
            if t.by is Actor.AGENT:
                moved_by_agent[key] = m.event.seq
        for key, (event, state) in last.items():
            machine = machine_of(self.view, key[0])
            if key not in filed or machine is None:
                continue
            onward = [t for t in machine.transitions if state in t.from_]
            if not onward or any(t.by is not Trigger.AGENT for t in onward):
                continue
            if moved_by_agent.get(key, 0) > event.seq:
                continue
            yield _Found(
                ItemCheck.ABANDONED,
                ItemKind.SERVICE_ITEM,
                f"{key[0]} {key[1]} was left in {state!r} from {_when(event.sim_time)} to the end; only the agent could "
                f"move it on ({', '.join(t.name for t in onward)}), and it never did",
                f"the service {key[0]}'s machine: from {state!r} only the agent moves it",
                event.sim_time,
                [event.seq],
                [],
            )

    def late_reactions(self) -> Iterable[_Found]:
        """Someone else moved an item the agent had filed or worked on, and the agent reacted later than its own
        declared rhythm, or never. Reacting is coming back to the item (a read, a call naming it, a write), except
        where the move left it in a state only the agent can move it on from: there it is the agent's move."""
        rhythm = self.view.rhythm
        if rhythm is None:
            return
        end = max([e.sim_time for e in self.view.events], default=self.view.scenario.starts_at)
        moves = transitions(self.view)
        mine: set[EntityRef] = set()
        for m in moves:
            t = m.transition
            if t.by is Actor.AGENT:
                mine.add(t.item)
                continue
            if t.item not in mine or t.provider not in self.services:
                continue
            machine = machine_of(self.view, t.provider)
            onward = [x for x in machine.transitions if t.to_state in x.from_] if machine is not None else []
            waits_on_agent = bool(onward) and all(x.by is Trigger.AGENT for x in onward)
            if waits_on_agent:
                came = [
                    n.at
                    for n in moves
                    if n.event.seq > m.event.seq and n.transition.item == t.item and n.transition.by is Actor.AGENT
                ]
                doing = f"moved it on ({', '.join(x.name for x in onward)})"
            else:
                came = [
                    c.sim_time
                    for _, c in self.calls
                    if c.sim_time >= m.at and t.item.external_id in c.exchange.path and c.provider is None
                ]
                came += [
                    e.sim_time
                    for e in self.view.events
                    if e.actor is Actor.AGENT and e.sim_time >= m.at and e.seq > m.event.seq and e.entity == t.item
                ]
                doing = "came back to it"
            back = min(came, default=None)
            took = (back or end) - m.at
            if took <= rhythm:
                continue
            said = f"{doing} {_ago(took)} later" if back is not None else f"never {doing} ({_ago(took)} to the end)"
            yield _Found(
                ItemCheck.LATE_REACTION,
                ItemKind.SERVICE_ITEM,
                f"{t.who or t.by.value} moved {t.provider} {t.item.external_id} to {t.to_state!r} at {_when(m.at)}; "
                f"the agent {said}",
                f"the agent file's declared rhythm, every {_ago(rhythm)}"
                + (f"; from {t.to_state!r} only the agent moves it on" if waits_on_agent else ""),
                m.at,
                [m.event.seq],
                [],
            )

    def awaited(self) -> dict[str, list[str]]:
        """For each declared service, the states of its machine the goal names, its first state aside: what the goal
        waits on (`once it's approved` waits on `approved`). Read from the goal's words and the machine's names."""
        goal = self.view.scenario.goal.casefold()
        found: dict[str, list[str]] = {}
        for key in self.services:
            machine = machine_of(self.view, key)
            if machine is None:
                continue
            named = [
                state
                for state in machine.states
                if state != machine.initial
                and re.search(rf"\b{re.escape(state.casefold().replace('_', ' '))}\b", goal.replace("_", " "))
            ]
            if named:
                found[key] = named
        return found

    def _states_at(self, service: str, seq: int) -> dict[str, tuple[str, int]]:
        """Each item of the service, its state and the seq that put it there, as the log stood before `seq`."""
        held: dict[str, tuple[str, int]] = {}
        for m in transitions(self.view):
            if m.event.seq >= seq:
                break
            if m.transition.provider == service:
                held[m.transition.item.external_id] = (m.transition.to_state, m.event.seq)
        return held

    def before_decision(self) -> Iterable[_Found]:
        """A write that acts (a record stored, a ticket, a document, an event) while no item of a declared service is
        in the state the goal waits on: before the decision it depends on, or after it went the other way."""
        awaited = self.awaited()
        if not awaited:
            return
        goal = self.view.scenario.goal
        for t in self.mine:
            if t.operation is Operation.DELETE or not self.takes(t, ItemCheck.BEFORE_DECISION):
                continue
            for service, states in awaited.items():
                held = self._states_at(service, t.seq)
                if any(state in states for state, _ in held.values()):
                    continue
                stood = (
                    "; ".join(f"{service} {item} was {state}" for item, (state, _) in held.items())
                    or f"nothing was filed with {service} yet"
                )
                yield _Found(
                    ItemCheck.BEFORE_DECISION,
                    t.kind,
                    f"{t.kind.value.replace('_', ' ')} {t.operation.value}d at {_when(t.at)} while no {service} item "
                    f"was {' or '.join(states)}: {stood}",
                    f'the goal ("{_quote(goal, 160)[1:-1]}") waits on {" or ".join(states)}, a state of the service '
                    f"{service}'s machine",
                    t.at,
                    [t.seq, *(seq for _, seq in held.values())],
                    [],
                )

    def deadline_missed(self) -> Iterable[_Found]:
        """The run reached the deadline and no item of a declared service reached the state the goal waits on."""
        deadline = self.view.scenario.deadline
        end = max([e.sim_time for e in self.view.events], default=self.view.scenario.starts_at)
        if deadline is None or end < deadline:
            return
        for service, states in self.awaited().items():
            reached = [
                m
                for m in transitions(self.view)
                if m.transition.provider == service and m.transition.to_state in states
            ]
            if any(m.at <= deadline for m in reached):
                continue
            held = self._states_at(service, max((e.seq for e in self.view.events), default=0) + 1)
            stood = "; ".join(f"{item} was {state}" for item, (state, _) in held.items()) or "nothing was filed"
            yield _Found(
                ItemCheck.DEADLINE_MISSED,
                ItemKind.SERVICE_ITEM,
                f"the deadline, {_when(deadline)}, came and no {service} item was {' or '.join(states)}: {stood}",
                f"the scenario's deadline and the goal, which waits on {' or '.join(states)}",
                deadline,
                [seq for _, seq in held.values()],
                [],
            )

    # -- declared stores and reads ----------------------------------------------------------------------------

    def written_twice(self) -> Iterable[_Found]:
        seen: dict[tuple[str, str, str], int] = {}
        for n, c in self.calls:
            x = c.exchange
            if x.captured is None or x.captured.mode is not CaptureMode.STORE or x.method != "POST" or x.status >= 300:
                continue
            key = (x.host, x.path.split("?", 1)[0], body_content(x.request_body or ""))
            if key in seen:
                yield _Found(
                    ItemCheck.WRITTEN_TWICE,
                    ItemKind.STORED_RECORD,
                    f"the same record written again to {x.host}{key[1]} at {_when(c.sim_time)} (first in call "
                    f"{seen[key]})",
                    f"the store {x.host} as it stood: it already held that record",
                    c.sim_time,
                    [c.first_seq] if c.first_seq <= c.last_seq else [],
                    [seen[key], n],
                )
            else:
                seen[key] = n

    def redundant_reads(self) -> Iterable[_Found]:
        """One resource read again and again, more of the reads seeing what the read before saw than seeing anything
        new."""
        reads: dict[tuple[str, str], list[Called]] = {}
        for c in calls(self.view):
            x = c.call.exchange
            if x.method == "GET" and x.status < 400:
                reads.setdefault((x.host, x.path), []).append(c)
        for (host, path), done in reads.items():
            same = sum(1 for c in done[1:] if not c.answer_changed)
            changed = len(done) - 1 - same
            if same < 2 or same <= changed:
                continue
            service = self.hosts.get(host)
            yield _Found(
                ItemCheck.REDUNDANT_READS,
                ItemKind.SERVICE_ITEM if service is not None else None,
                f"GET {host}{path} was read {len(done)} times from {_when(done[0].at)} to {_when(done[-1].at)}; "
                f"{same} reads answered what the read before had, {changed} saw a change",
                f"what {host} answered: the same {same} times",
                done[-1].at,
                [],
                [c.position for c in done],
            )

    # -- helpers -----------------------------------------------------------------------------------------------

    def _asks(self) -> list[Obligation]:
        return [o for o in self.view.obligations if o.kind is ObligationKind.ANSWER_FROM_PERSON]

    def _wake(self, seq: int) -> int | None:
        return next((e.wake for e in self.view.events if e.seq == seq), None)


def _news(events: list[WorldEvent], after: int, before: int, typed: dict[int, TypedItem]) -> bool:
    """Whether anything a person could learn from happened strictly between two seqs: anyone else's write to an
    item, or a write of the agent's that is not a message."""
    for e in events:
        if e.seq <= after or e.operation not in _WRITES or e.entity.kind in _BOOKKEEPING:
            continue
        if e.seq >= before:
            break
        if e.actor is Actor.SCENARIO:
            continue
        if e.actor is not Actor.AGENT:
            return True
        t = typed.get(e.seq)
        if e.entity.kind is EntityKind.TRANSITION or (t is not None and t.kind not in _SAID):
            return True
    return False


def machine_of(view: RunView, service: str) -> Machine | None:
    """The service's machine: declared, or as the run fixed it."""
    declared = next((s.machine for s in view.scenario.services if s.key == service), None)
    if declared is not None:
        return declared
    for e in view.events:
        after = e.after
        if (
            isinstance(after, ServiceRecordSnapshot)
            and after.service == service
            and after.record is ServiceRecordKind.MACHINE
        ):
            return Machine.model_validate_json(after.text)
    return None


def _machine_words(view: RunView, service: str) -> str:
    machine = machine_of(view, service)
    if machine is None:
        return ""
    moves = "; ".join(f"{t.name}: {', '.join(t.from_)} -> {t.to} by {t.by.value}" for t in machine.transitions)
    return f" ({moves})"


def _window_of(person: Person) -> str:
    if person.reply_within is not None:
        w = person.reply_within
        return f"{person.key}'s reply_within ({_ago(w.min)} to {_ago(w.max)} of their available time)"
    return f"{person.key}'s reply delay"


def _actor(event: WorldEvent) -> str:
    return event.actor.value


def _quote(text: str, most: int = 120) -> str:
    flat = " ".join(text.split())
    return f'"{flat[:most]}{"…" if len(flat) > most else ""}"'
