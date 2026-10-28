"""The agent's due records (`AgentUnderTest.due`), as the run read them: one it was woken for and did not pick up,
and one whose due time the run could not read.

A wake begun for due records lasts until the agent writes one of each declaration's, or one `poll` of the sandbox's
clock (`application.due_records`). A record the agent did not write in that wake, nor any other record of its
declaration the wake was for, and still due by the declaration at its instant when the wake ended (its `later`
fields read at that instant), was due and not picked up within one poll, and is not woken for again. In production a
poller whose wait and whose query disagree wakes for such a record without end, with nothing to do, and nothing done;
a poller that is broken never looks. A record left by a wake that wrote another record of its declaration is a
backlog taken a record per poll: it is not said here. Whether it is a fault is a judgement (`review`): the work may
have moved to a record the agent file does not name.

A record whose field, or one of whose `later` fields, holds something other than an ISO 8601 instant with an offset
(a date alone, a time with no offset, a number) is not read as due, so nothing woke the agent for it.
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import JsonValue

from minutehand.checks.facts import blocked
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.clock import DueClosed, DueEntry, DueSource
from minutehand.domain.memory import DueRecords, field_at, instant, later_bound, matches
from minutehand.domain.world import EntityKind, MemorySnapshot, Operation, WorldEvent, unpicked, written

Memory = dict[tuple[str, str], tuple[str, int]]
"""Each key's value as JSON text, and the seq that wrote it."""


class DueRecordsCheck:
    id = "due_records"
    needs = frozenset({Needs.WORLD})  # a run with no wakes woke for no record: nothing to say, not a check that failed
    pattern = None

    def run(self, view: RunView) -> CheckReport:
        if not view.due:
            return CheckReport()  # the agent keeps no due records: nothing to state
        missing = blocked(view, self.needs, self.id)
        if missing:
            return CheckReport(blocked=missing)
        findings = _malformed(view) + _left_due(view)
        return CheckReport(findings=findings)


def _fold(events: list[WorldEvent], *, until_wake: int | None = None) -> Memory:
    memory: Memory = {}
    for event in events:
        if until_wake is not None and event.wake > until_wake:
            break
        after = event.after
        if event.entity.kind is not EntityKind.MEMORY or not isinstance(after, MemorySnapshot):
            continue
        if event.operation is Operation.DELETE:
            memory.pop((after.collection, after.key), None)
        elif event.operation in (Operation.CREATE, Operation.UPDATE) and after.value is not None:
            memory[(after.collection, after.key)] = (after.value, event.seq)
    return memory


def _due_at(source: DueRecords, text: str) -> tuple[bool, JsonValue, JsonValue]:
    """Whether the value is one of `source`'s records, what its field holds (None when absent), and the value."""
    value: JsonValue = json.loads(text)
    if not matches(value, source.where):
        return False, None, value
    there, written = field_at(value, source.field)
    return True, written if there else None, value


def _held(value: JsonValue, path: str) -> JsonValue:
    there, written = field_at(value, path)
    return written if there else None


def _malformed(view: RunView) -> list[Finding]:
    """Each record whose field held something that is not an instant with an offset, once for each thing held."""
    findings: list[Finding] = []
    said: set[tuple[str, str, str, str]] = set()
    for event in view.events:
        after = event.after
        if not isinstance(after, MemorySnapshot) or after.value is None or event.operation is Operation.DELETE:
            continue
        for source in view.due:
            if after.collection != source.collection or not after.key.startswith(source.prefix):
                continue
            mine, _, value = _due_at(source, after.value)
            if not mine:
                continue
            for path in [source.field, *source.later]:
                written = _held(value, path)
                if written is None or instant(written) is not None:
                    continue
                shown = json.dumps(written, ensure_ascii=False)
                if (after.collection, after.key, path, shown) in said:
                    continue
                said.add((after.collection, after.key, path, shown))
                findings.append(
                    Finding(
                        check=DueRecordsCheck.id,
                        severity=Severity.WARNING,
                        kind=FindingKind.REVIEW,
                        message=f"{after.collection}/{after.key} holds {path} = {shown}, which is not an ISO 8601 "
                        "instant with an offset: the run did not read it as due, so nothing woke the agent for it",
                        at=event.sim_time,
                        wake=event.wake,
                        evidence=[event.seq],
                    )
                )
    return findings


def _left_due(view: RunView) -> list[Finding]:
    """Each record the agent was woken for and wrote none of its declaration's in that wake (`domain.world.unpicked`),
    still held due by its declaration at the same instant when the wake ended, its `later` fields read at that
    instant."""
    findings: list[Finding] = []
    for entry in view.dues or []:
        if entry.source is not DueSource.RECORDED or entry.closed is not DueClosed.FIRED:
            continue
        woke = _wake_of(view, entry)
        if woke is None:
            continue
        memory = _fold(view.events, until_wake=woke)
        wrote = written(e for e in view.events if e.wake == woke)
        for record in unpicked(entry.records, wrote):
            if record.source >= len(view.due) or (record.collection, record.key) not in memory:
                continue
            source = view.due[record.source]
            text, seq = memory[(record.collection, record.key)]
            mine, held, value = _due_at(source, text)
            still = instant(held) if mine else None
            if still is None or still != record.at:
                continue
            readable, until = later_bound(value, source.later)
            if not readable or (until is not None and until <= still):
                continue
            findings.append(
                Finding(
                    check=DueRecordsCheck.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=f"{record.collection}/{record.key} was due at {_said(record.at)}, and not picked up within "
                    f"one poll ({_named(source)}, poll {source.poll}): the agent was woken for it and wrote no "
                    "record of that declaration in the wake, and it is not woken for it again. A poller whose wait "
                    "and whose query disagree wakes for such a record without end, with nothing to do; one that is "
                    "broken never looks",
                    at=entry.closed_at,
                    wake=woke,
                    evidence=[seq],
                )
            )
    return findings


def _wake_of(view: RunView, entry: DueEntry) -> int | None:
    """The wake a fired entry began: the first after the one in progress when it left the table."""
    if entry.closed_wake is None:
        return None
    later = [w.index for w in view.wakes if w.index > entry.closed_wake]
    return min(later) if later else None


def _said(moment: datetime) -> str:
    return moment.isoformat()


def _named(source: DueRecords) -> str:
    """The declaration as the agent file spells it: its collection, prefix, `where` and field."""
    where = "".join(f" {k}={json.dumps(v, ensure_ascii=False)}" for k, v in sorted(source.where.items()))
    return f"due {source.collection}/{source.prefix}*{where} by {source.field}"
