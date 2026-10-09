"""The team's own rules, read over the facts of the run (`domain/assessments.py`, `docs/assessments.md`).

A rule is read once for each thing it is for, at each of its moments: the condition first, then the count of facts
between its two moments against its bounds. A rule that names a moment the run never reached, or one a thing does
not have (the answer to an ask nobody answered, the deadline of a scenario with none), is not read for that thing,
and a note says how many were left unread, so a rule can never pass by being skipped unseen.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

from minutehand.checks.facts import (
    Ask,
    Fact,
    Moved,
    asks,
    calls,
    ended_at,
    messages,
    planned_wakes,
    reported,
    transitions,
    writes,
)
from minutehand.domain.agent import CommitmentStatus
from minutehand.domain.assessments import (
    OWNER,
    THIS_PERSON,
    Anchor,
    CommitmentState,
    Each,
    Judged,
    Moment,
    Mover,
    Rule,
    Thing,
    Write,
)
from minutehand.domain.checks import (
    CheckReport,
    Finding,
    FindingKind,
    Needs,
    ObligationKind,
    RuleRead,
    RunView,
    Severity,
)
from minutehand.domain.scenario import Person
from minutehand.domain.templates import fill
from minutehand.domain.world import (
    Actor,
    EntityKind,
    MemorySnapshot,
    Operation,
    RecordedCall,
    StoredSnapshot,
    WorldEvent,
)

_KIND = {Judged.FAIL: FindingKind.FAIL, Judged.REVIEW: FindingKind.REVIEW}
_SEVERITY = {Judged.FAIL: Severity.ERROR, Judged.REVIEW: Severity.WARNING}
_THING = {
    EntityKind.MESSAGE: Thing.MESSAGE,
    EntityKind.TICKET: Thing.TICKET,
    EntityKind.COMMENT: Thing.COMMENT,
    EntityKind.DOCUMENT: Thing.DOCUMENT,
    EntityKind.RECORD: Thing.RECORD,
    EntityKind.INBOX_ITEM: Thing.INBOX_ITEM,
    EntityKind.FILE: Thing.FILE,
    EntityKind.TOOL_CALL: Thing.TOOL_CALL,
    EntityKind.STORED: Thing.STORED,
}

_MOVER = {
    Actor.AGENT: Mover.AGENT,
    Actor.PERSON: Mover.PERSON,
    Actor.SYSTEM: Mover.SYSTEM,
    Actor.TIMER: Mover.TIMER,
}
_OPERATION = {Write.CREATE: Operation.CREATE, Write.UPDATE: Operation.UPDATE, Write.DELETE: Operation.DELETE}
_STATUS = {
    CommitmentState.OPEN: CommitmentStatus.OPEN,
    CommitmentState.MET: CommitmentStatus.MET,
    CommitmentState.DROPPED: CommitmentStatus.DROPPED,
}


def _thing(kind: EntityKind) -> Thing | None:
    return _THING[kind] if kind in _THING else None


@dataclass(frozen=True)
class _Subject:
    """One thing a rule is read for: the run, an ask or hand-off, or a person."""

    person: Person | None = None
    ask: Ask | None = None
    moved: Moved | None = None
    never_moved: bool = False
    """For a rule read for the first transition of a kind (`where.first`) when there was none: its moment is the
    run's end."""

    def label(self) -> str:
        if self.never_moved:
            return "no such transition by the end of the run"
        if self.moved is not None:
            t = self.moved.transition
            return f"the transition {t.name!r} of {t.provider} {t.item.external_id} at {t.at:%Y-%m-%d %H:%M} UTC"
        if self.ask is not None:
            whom = self.ask.person or "someone"
            return f"the ask of {whom} at {self.ask.at:%Y-%m-%d %H:%M} UTC"
        if self.person is not None:
            return self.person.key
        return "the run"


class _Unread(Exception):
    """The rule names a moment this subject does not have, or one the run never reached."""


class Assessments:
    """Every rule of the team's, read over the facts. With no rule, it has nothing to say."""

    id = "assessments"
    needs = frozenset({Needs.WORLD})

    def run(self, view: RunView) -> CheckReport:
        """Every rule read over the facts."""
        if not view.rules:
            return CheckReport()
        reader = _Reader(view)
        findings: list[Finding] = []
        notes: list[str] = []
        tallies: list[RuleRead] = []
        for rule in view.rules:
            found, read, unread = reader.read(rule)
            findings += found
            tallies.append(RuleRead(rule=rule.id, read=read, unread=unread))
            if unread:
                notes.append(
                    f"rule {rule.id} was not read {unread} time{'s' if unread != 1 else ''}: it names a moment the run "
                    "never reached, or one that was not there (an answer never given, a deadline never set), or counts "
                    "what the run did not record (the agent's planned wakes, its calls, what an item holds back)"
                )
        return CheckReport(findings=findings, notes=notes, rules_read=tallies)


class _Reader:
    def __init__(self, view: RunView) -> None:
        self.view = view
        self.end = ended_at(view)
        self.people = {p.key: p for p in view.scenario.people}
        self.asks = asks(view)
        self.handoffs = asks(view, ObligationKind.WORK_WITH_PERSON)
        self.sent = messages(view)
        self.written = writes(view)
        self.planned = planned_wakes(view, self.end)
        self.said = reported(view)
        self.moves = transitions(view)
        self.called = calls(view)
        answered = [a.answered_at for a in self.asks]
        self.all_answered = (
            max(t for t in answered if t is not None) if answered and all(t is not None for t in answered) else None
        )

    def read(self, rule: Rule) -> tuple[list[Finding], int, int]:
        """The rule's findings, the times it applied and was read, and the times it could not be read."""
        findings: list[Finding] = []
        read = unread = 0
        for subject in self._subjects(rule):
            for at in rule.at or [None]:
                try:
                    applied, finding = self._one(rule, subject, at)
                except _Unread:
                    unread += 1
                    continue
                read += applied
                if finding is not None:
                    findings.append(finding)
        return findings, read, unread

    def _subjects(self, rule: Rule) -> list[_Subject]:
        if rule.each is Each.RUN:
            return [_Subject()]
        if rule.each is Each.PERSON:
            return [_Subject(person=p) for p in self.view.scenario.people if self._picked(rule, p.key)]
        if rule.each is Each.TRANSITION:
            w = rule.where
            read = [
                _Subject(person=self.people[m.transition.who] if m.transition.who in self.people else None, moved=m)
                for m in self.moves
                if _among(m.transition.provider, w.provider)
                and _among(m.transition.name, w.name)
                and _among(m.transition.to_state, w.to)
                and (not w.by or _mover(m) in w.by)
                and (
                    not (w.person or w.person_not)
                    or (m.transition.who is not None and self._picked(rule, m.transition.who))
                )
            ]
            if w.first:
                return read[:1] or [_Subject(never_moved=True)]
            return read
        found = self.asks if rule.each is Each.ASK else self.handoffs
        return [
            _Subject(person=self.people[a.person] if a.person in self.people else None, ask=a)
            for a in found
            if a.person is not None and self._picked(rule, a.person)
        ]

    def _phrases(self, said: list[str], subject: _Subject, known: dict[str, str]) -> list[str]:
        """The phrases a rule names, filled for `subject`: `{ask.facts}` alone is each fact the answer carried."""
        phrases: list[str] = []
        for p in said:
            if p.strip() == "{ask.facts}":
                if subject.ask is None or subject.ask.answer is None:
                    raise _Unread
                phrases += subject.ask.answer_facts or [subject.ask.answer]
            else:
                if "{ask.answer}" in p and (subject.ask is None or subject.ask.answer is None):
                    raise _Unread
                phrases.append(str(fill(p, known)))
        return phrases

    def _picked(self, rule: Rule, key: str) -> bool:
        keys = [self._key(w, None) for w in rule.where.person]
        not_keys = [self._key(w, None) for w in rule.where.person_not]
        return (not keys or key in keys) and key not in not_keys

    def _key(self, who: str, subject: _Subject | None) -> str:
        if who == OWNER:
            return self.view.scenario.owner
        if who == THIS_PERSON:
            assert subject is not None and subject.person is not None
            return subject.person.key
        return who

    def _when(self, said: str, subject: _Subject, at: str | None, *, past_end: bool = False) -> datetime:
        """The moment `said` names for `subject`; past the run's end it is not read, unless `past_end`."""
        moment = Moment.read(said)
        base: datetime | None
        if moment.anchor is Anchor.START:
            base = self.view.scenario.starts_at
        elif moment.anchor is Anchor.DEADLINE:
            base = self.view.scenario.deadline
        elif moment.anchor is Anchor.END:
            base = self.end
        elif moment.anchor is Anchor.MOMENT:
            assert at is not None
            base = self._when(at, subject, None)
        elif moment.anchor is Anchor.ALL_ANSWERED:
            base = self.all_answered
        elif moment.anchor is Anchor.TRANSITION:
            if subject.moved is None:
                assert subject.never_moved
                return self.end  # never made: the whole run, whatever the offset
            base = subject.moved.at
        else:
            ask = subject.ask
            assert ask is not None
            if moment.anchor is Anchor.ASK:
                base = ask.at
            elif moment.anchor is Anchor.ANSWER:
                base = ask.answered_at
            elif moment.anchor is Anchor.CLOSED:
                base = ask.answered_at or self.end
            else:
                base = ask.obligation.expected_by
        if base is None:
            raise _Unread
        found = base + moment.offset
        if found > self.end and not past_end:
            raise _Unread
        return found

    def _one(self, rule: Rule, subject: _Subject, at: str | None) -> tuple[bool, Finding | None]:
        """Whether the rule applied (its `when` held), and its finding when its count broke a bound."""
        moment = self._when(at, subject, None) if at is not None else None
        if not self._holds(rule, subject, at):
            return False, None
        since = self._when(rule.count.since, subject, at) if rule.count.since is not None else None
        until = self._when(rule.count.until, subject, at, past_end=True) if rule.count.until is not None else None
        counted = [
            f
            for f in self._facts(rule, subject, at)
            if (since is None or f.at >= since) and (until is None or f.at <= until)
        ]
        broke = _broken(rule, counted)
        if until is not None and until > self.end and (broke is None or not _for_good(rule, counted)):
            # The window runs past the end: only what more facts could not undo is said; the rest is unread.
            raise _Unread
        if broke is None:
            return True, None
        cited = sorted({c for f in counted for c in f.calls})
        evidence = sorted(
            {s for f in counted for s in f.seqs}
            | ({subject.ask.obligation.opened_by} if subject.ask else set())
            | ({subject.moved.event.seq} if subject.moved else set())
        )
        moved = subject.moved.transition if subject.moved is not None else None
        values = {
            "person.key": subject.person.key if subject.person is not None else "",
            "person.name": subject.person.name if subject.person is not None else "",
            "ask.at": f"{subject.ask.at:%Y-%m-%d %H:%M} UTC" if subject.ask is not None else "",
            "ask.answer": subject.ask.answer or "" if subject.ask is not None else "",
            "rule.id": rule.id,
            "rule.count": str(len(counted)),
            "rule.moment": f"{at} ({moment:%Y-%m-%d %H:%M} UTC)" if at is not None and moment is not None else "",
            "transition.provider": moved.provider if moved is not None else "",
            "transition.item": moved.item.external_id if moved is not None else "",
            "transition.name": moved.name if moved is not None else "",
            "transition.from": (moved.from_state or "") if moved is not None else "",
            "transition.to": moved.to_state if moved is not None else "",
            "transition.by": moved.by.value if moved is not None else "",
            "transition.who": (moved.who or "") if moved is not None else "",
        }
        said = fill(rule.message, values) if rule.message is not None else None
        window = _window(rule, since, until)
        return True, Finding(
            check=rule.id,
            severity=_SEVERITY[rule.severity],
            kind=_KIND[rule.severity],
            message=str(said)
            if said is not None
            else f"{subject.label()}: {len(counted)} {rule.count.counted.replace('_', '-')}{window}; {broke}",
            at=until or moment or (counted[-1].at if counted else None),
            evidence=evidence,
            calls=cited,
            pattern=rule.pattern,
        )

    def _holds(self, rule: Rule, subject: _Subject, at: str | None) -> bool:
        when = rule.when
        if when.stopped and (self.view.stopped is None or self.view.stopped not in when.stopped):
            return False
        ask = subject.ask
        if when.answered is not None:
            assert ask is not None
            if (ask.answered_at is not None) != when.answered:
                return False
        if when.open_at is not None:
            assert ask is not None
            if not ask.open_at(self._when(when.open_at, subject, at)):
                return False
        return True

    def _facts(self, rule: Rule, subject: _Subject, at: str | None) -> list[Fact]:
        count = rule.count
        if count.follow_ups is not None:
            assert subject.ask is not None
            return subject.ask.follow_ups
        if count.touches is not None:
            assert subject.ask is not None
            return subject.ask.touches
        if count.messages is not None:
            m = count.messages
            to = {self._key(w, subject) for w in m.to}
            to_not = {self._key(w, subject) for w in m.to_not}
            known = {
                "ask.answer": subject.ask.answer or "" if subject.ask is not None else "",
                "person.key": subject.person.key if subject.person is not None else "",
                "person.name": subject.person.name if subject.person is not None else "",
            }
            phrases = self._phrases(m.holding, subject, known)
            thread = (
                subject.ask.obligation.entity.external_id if subject.ask and subject.ask.obligation.entity else None
            )
            return [
                Fact(at=s.event.sim_time, seqs=[s.event.seq])
                for s in self.sent
                if (not to or to & set(s.to))
                and not (to_not & set(s.to))
                and (m.in_thread is None or (s.thread_of == thread) == m.in_thread)
                and all(p.casefold() in s.text.casefold() for p in phrases)
                and (m.to_away is None or bool(s.to_away and (not to or to & set(s.to_away))) == m.to_away)
            ]
        if count.writes is not None:
            w = count.writes
            return [
                Fact(at=x.event.sim_time, seqs=[x.event.seq])
                for x in self.written
                if (not w.things or _thing(x.event.entity.kind) in w.things)
                and _thing(x.event.entity.kind) not in w.things_not
                and (not w.operations or x.event.operation in {_OPERATION[o] for o in w.operations})
                and (w.repeats_open_ticket is None or x.repeats_open_ticket == w.repeats_open_ticket)
                and (w.in_repeated_wake is None or x.in_repeated_wake == w.in_repeated_wake)
            ]
        if count.wakes is not None:
            k = count.wakes
            return [
                Fact(at=wake.sim_time)
                for wake in self.view.wakes
                if (k.changed_world is None or (wake.world_changes > 0) == k.changed_world)
                and (k.changed_commitments is None or wake.commitments_changed == k.changed_commitments)
            ]
        if count.planned_wakes is not None:
            if self.view.dues is None:
                raise _Unread  # a captured run or a standing world keeps no table of what the agent planned
            return self.planned
        if count.commitments is not None:
            c = count.commitments
            emails = {
                self.people[self._key(w, subject)].email for w in c.waiting_on if self._key(w, subject) in self.people
            }
            states = {_STATUS[s] for s in c.status}
            return [
                Fact(at=r.at)
                for r in self.said
                if (not states or r.status in states) and (not emails or r.person_email in emails)
            ]
        if count.memory is not None:
            return self._memory(rule, subject, at)
        if count.stored is not None:
            return self._stored(rule, subject, at)
        if count.transitions is not None:
            t = count.transitions
            who = {self._key(w, subject) for w in t.who}
            item = subject.moved.transition.item if subject.moved is not None else None
            return [
                Fact(at=m.at, seqs=[m.event.seq])
                for m in self.moves
                if _among(m.transition.provider, t.provider)
                and _among(m.transition.name, t.name)
                and _among(m.transition.to_state, t.to)
                and (not t.from_ or (m.transition.from_state is not None and _among(m.transition.from_state, t.from_)))
                and (not t.by or _mover(m) in t.by)
                and (not who or m.transition.who in who)
                and (not t.reached or any(_among(r, t.reached) for r in m.reached))
                and not any(_among(r, t.not_reached) for r in m.reached if t.not_reached)
                and (t.same_item is None or (m.transition.item == item) == t.same_item)
            ]
        if count.calls is not None:
            if self.view.calls is None:
                raise _Unread  # nobody recorded the run's calls
            k = count.calls
            routes = [_route(r) for r in k.route]
            return [
                Fact(at=c.at, seqs=_seqs(c.call), calls=[c.position])
                for c in self.called
                if _among(c.call.exchange.host, k.host)
                and _among(c.call.exchange.method, k.method)
                and (not routes or any(r.fullmatch(c.route) for r in routes))
                and (not k.status or any(_status(c.call.exchange.status, s) for s in k.status))
                and (k.refused is None or (c.call.exchange.status >= 400) == k.refused)
                and (k.answer_changed is None or c.answer_changed == k.answer_changed)
            ]
        if count.replies is not None:
            r = count.replies
            by = {self._key(w, subject) for w in r.by}
            how = {w.value for w in r.written}
            return [
                Fact(at=x.at)
                for x in self.view.replies
                if x.at <= self.end and (not by or x.person in by) and (not how or x.writing.value in how)
            ]
        assert count.asks is not None
        a = count.asks
        of = {self._key(w, subject) for w in a.of}
        open_at = self._when(a.open_at, subject, at) if a.open_at is not None else None
        return [
            Fact(at=x.at, seqs=[x.obligation.opened_by])
            for x in self.asks
            if (not of or x.person in of) and (open_at is None or x.open_at(open_at))
        ]

    def _memory(self, rule: Rule, subject: _Subject, at: str | None) -> list[Fact]:
        """Each key of the agent's memory the rule picks, as it stood at the count's `until` (the end without one),
        counted at the moment its value there was written."""
        m = rule.count.memory
        assert m is not None
        until = self._when(rule.count.until, subject, at, past_end=True) if rule.count.until is not None else self.end
        named = {"person.key": subject.person.key if subject.person is not None else ""}
        key = str(fill(m.key, named)) if m.key is not None else None
        prefix = str(fill(m.prefix, named)) if m.prefix is not None else None
        held: dict[str, WorldEvent] = {}
        for event in self.view.events:
            after = event.after
            if (
                event.entity.kind is not EntityKind.MEMORY
                or not isinstance(after, MemorySnapshot)
                or after.collection != m.collection
                or event.sim_time > until
            ):
                continue
            if event.operation is Operation.DELETE:
                held.pop(after.key, None)
            elif event.operation in (Operation.CREATE, Operation.UPDATE) and after.value is not None:
                held[after.key] = event
        return [
            Fact(at=event.sim_time, seqs=[event.seq])
            for name, event in sorted(held.items())
            if (key is None or name == key)
            and (prefix is None or name.startswith(prefix))
            and _matches(event, m.values)
        ]

    def _stored(self, rule: Rule, subject: _Subject, at: str | None) -> list[Fact]:
        """Each item of a `store` host the rule picks, as it stood at the count's `until` (the end without one),
        counted at the moment its version there was written."""
        s = rule.count.stored
        assert s is not None
        until = self._when(rule.count.until, subject, at, past_end=True) if rule.count.until is not None else self.end
        held: dict[tuple[str, str], WorldEvent] = {}
        for event in self.view.events:
            after = event.after
            if (
                event.entity.kind is not EntityKind.STORED
                or not isinstance(after, StoredSnapshot)
                or (s.host is not None and after.host != s.host)
                or (s.collection is not None and after.collection != s.collection)
                or event.sim_time > until
            ):
                continue
            item = (event.entity.provider, event.entity.external_id)
            if event.operation is Operation.DELETE:
                held.pop(item, None)
            elif event.operation in (Operation.CREATE, Operation.UPDATE) and after.item is not None:
                held[item] = event
        return [
            Fact(at=event.sim_time, seqs=[event.seq]) for _, event in sorted(held.items()) if _matches(event, s.values)
        ]


def _route(said: str) -> re.Pattern[str]:
    """A route as written (`/v1/requests/{id}`) as a pattern of paths: each `{name}` one segment."""
    parts = re.split(r"(\{[^/{}]+\})", said.split("?", 1)[0])
    return re.compile("".join("[^/]+" if p.startswith("{") and p.endswith("}") else re.escape(p) for p in parts))


def _status(status: int, said: str) -> bool:
    return said[0] == str(status)[0] if said.endswith("xx") else str(status) == said


def _seqs(call: RecordedCall) -> list[int]:
    return list(range(call.first_seq, call.last_seq + 1)) if call.first_seq <= call.last_seq else []


def _among(said: str, wanted: list[str]) -> bool:
    """Whether `said` is one of `wanted`, in any case; any is when none is wanted."""
    return not wanted or said.casefold() in {w.casefold() for w in wanted}


def _mover(moved: Moved) -> Mover | None:
    by = moved.transition.by
    return _MOVER[by] if by in _MOVER else None


def _matches(event: WorldEvent, wanted: dict[str, str | int | float | bool | None]) -> bool:
    """Whether the value a memory write, or a stored item's write, left has each field as the rule wants it."""
    if not wanted:
        return True
    after = event.after
    text = (
        after.value if isinstance(after, MemorySnapshot) else after.item if isinstance(after, StoredSnapshot) else None
    )
    assert text is not None
    value: object = json.loads(text)
    for path, expected in wanted.items():
        here = value
        for part in path.split("."):
            if not isinstance(here, dict) or part not in here:
                return False
            here = here[part]
        if here != expected or isinstance(here, bool) != isinstance(expected, bool):
            return False
    return True


def _broken(rule: Rule, counted: list[Fact]) -> str | None:
    """How the count breaks the rule's bounds; None when it keeps them."""
    n = len(counted)
    if rule.exactly is not None and n != rule.exactly:
        return f"expected exactly {rule.exactly}"
    if rule.at_least is not None and n < rule.at_least:
        return f"expected at least {rule.at_least}"
    if rule.at_most is not None and n > rule.at_most:
        return f"expected at most {rule.at_most}"
    return _too_close(rule, counted)


def _too_close(rule: Rule, counted: list[Fact]) -> str | None:
    if rule.gap_at_least is None:
        return None
    times = sorted(f.at for f in counted)
    close = [b - a for a, b in pairwise(times) if b - a < rule.gap_at_least]
    if not close:
        return None
    were = "were" if len(close) != 1 else "was"
    return (
        f"{len(close)} {were} closer than {_hours(rule.gap_at_least)} to the one before, the closest "
        f"{_hours(min(close))}"
    )


def _for_good(rule: Rule, counted: list[Fact]) -> bool:
    """Whether the count already breaks a bound more facts could not mend: too many, or two too close."""
    n = len(counted)
    too_many = (rule.at_most is not None and n > rule.at_most) or (rule.exactly is not None and n > rule.exactly)
    return too_many or _too_close(rule, counted) is not None


def _hours(delta: timedelta) -> str:
    minutes = round(delta.total_seconds() / 60)
    if minutes < 120:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{minutes / 60:g} hours"


def _window(rule: Rule, since: datetime | None, until: datetime | None) -> str:
    parts = []
    if since is not None:
        parts.append(f" from {rule.count.since} ({since:%Y-%m-%d %H:%M})")
    if until is not None:
        parts.append(f" until {rule.count.until} ({until:%Y-%m-%d %H:%M} UTC)")
    return "".join(parts)
