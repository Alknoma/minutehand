"""The agent's due records (`AgentUnderTest.due`), as the run read them: one it was woken for and left due, and one
whose due time the run could not read.

A record woken for at its instant and still holding that instant when the wake ended is not woken for again
(`application.due_records`): the agent took its wake and left the work due. In production a poller whose wait and
whose query disagree wakes for such a record without end, with nothing to do, and nothing done. Whether it is a
fault is a judgement (`review`): the work may have moved to a record the agent file does not name.

A record whose field holds something other than an ISO 8601 instant with an offset (a date alone, a time with no
offset, a number) is not read as due, so nothing woke the agent for it.
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import JsonValue

from minutehand.checks.facts import blocked
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.clock import DueClosed, DueEntry, DueSource
from minutehand.domain.memory import DueRecords, field_at, instant, matches
from minutehand.domain.world import EntityKind, MemorySnapshot, Operation, WorldEvent

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


def _due_at(source: DueRecords, text: str) -> tuple[bool, JsonValue]:
    """Whether the value is one of `source`'s records, and what its field holds (None when absent)."""
    value: JsonValue = json.loads(text)
    if not matches(value, source.where):
        return False, None
    there, written = field_at(value, source.field)
    return True, written if there else None


def _malformed(view: RunView) -> list[Finding]:
    """Each record whose field held something that is not an instant with an offset, once for each thing held."""
    findings: list[Finding] = []
    said: set[tuple[str, str, str]] = set()
    for event in view.events:
        after = event.after
        if not isinstance(after, MemorySnapshot) or after.value is None or event.operation is Operation.DELETE:
            continue
        for source in view.due:
            if after.collection != source.collection or not after.key.startswith(source.prefix):
                continue
            mine, written = _due_at(source, after.value)
            if not mine or written is None or instant(written) is not None:
                continue
            shown = json.dumps(written, ensure_ascii=False)
            if (after.collection, after.key, shown) in said:
                continue
            said.add((after.collection, after.key, shown))
            findings.append(
                Finding(
                    check=DueRecordsCheck.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=f"{after.collection}/{after.key} holds {source.field} = {shown}, which is not an ISO 8601 "
                    "instant with an offset: the run did not read it as due, so nothing woke the agent for it",
                    at=event.sim_time,
                    wake=event.wake,
                    evidence=[event.seq],
                )
            )
    return findings


def _left_due(view: RunView) -> list[Finding]:
    """Each record the agent was woken for and still held due at the same instant when that wake ended."""
    findings: list[Finding] = []
    for entry in view.dues or []:
        if entry.source is not DueSource.RECORDED or entry.closed is not DueClosed.FIRED:
            continue
        woke = _wake_of(view, entry)
        if woke is None:
            continue
        memory = _fold(view.events, until_wake=woke)
        for record in entry.records:
            if record.source >= len(view.due) or (record.collection, record.key) not in memory:
                continue
            source = view.due[record.source]
            text, seq = memory[(record.collection, record.key)]
            mine, written = _due_at(source, text)
            still = instant(written) if mine else None
            if still is None or still != record.at:
                continue
            findings.append(
                Finding(
                    check=DueRecordsCheck.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=f"the agent was woken for {record.collection}/{record.key}, due at "
                    f"{_said(record.at)}, and left it due at that instant: it is not woken for it again. A poller "
                    "whose wait and whose query disagree wakes for such a record without end, with nothing to do",
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
