"""The read model of one run, built on demand into an SQLite database of its own from what Minutehand's readers
answer: every view of `schema.VIEWS` as a table, every body decoded.

Why a companion database and not views over the world file: much of what a reader wants is not in a column of the
world file. A body of 512 bytes or more is a zstd-compressed row of `content`; a fork sees its parent's rows only up
to its checkpoint, by rules `SqliteStore` keeps (`_visible`, the calls and spans it may see); whether a message asked,
followed up or answered is the ledger's (`checks.ledger`); the model call behind a message is the join of
`application.model_calls`; findings are in `result.json`. Views over the raw tables would restate each of those in
SQL and drift from them. Built here from the same readers the viewer, the checks and the MCP tools use, the read
model says what they say, and `minutehand query --export` writes it as a plain SQLite file any tool reads.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from minutehand import session
from minutehand.adapters.query.schema import BY_NAME, VERSION, VIEWS, SqlType
from minutehand.application.checkpoint import Checkpoint, checkpoints
from minutehand.application.dues import due_entries
from minutehand.application.model_calls import EventTrace, is_model_call, model_call, trace_of
from minutehand.checks.facts import asks
from minutehand.checks.ledger import absences
from minutehand.checks.patterns import pattern
from minutehand.checks.runner import view_of
from minutehand.domain.checks import WakeRecord
from minutehand.domain.clock import DueClosed, DueEntry
from minutehand.domain.conversation import PersonCall, Wrote
from minutehand.domain.people import PersonReply, Writing
from minutehand.domain.prices import Prices
from minutehand.domain.scenario import Answers, Scenario, Scripted
from minutehand.domain.telemetry import (
    ArrayValue,
    AttributeValue,
    BoolValue,
    BytesValue,
    DoubleValue,
    IntValue,
    MapValue,
    StoredSpan,
    StringValue,
)
from minutehand.domain.world import (
    Actor,
    AnsweredBy,
    EntityRef,
    InboxItemSnapshot,
    InteractionSnapshot,
    MemorySnapshot,
    MessageSnapshot,
    NextWakeSnapshot,
    Operation,
    PendingSnapshot,
    PendingStatus,
    RecordedCall,
    StoredSnapshot,
    TransitionSnapshot,
    WorldEvent,
)

Cell = str | int | float | bytes | None
Row = dict[str, Cell]
JsonValue = str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"] | None

WRITES = frozenset({Operation.CREATE, Operation.UPDATE, Operation.DELETE})
READS = frozenset({Operation.READ, Operation.SEARCH})
SUMMARY = 200

_ANSWERED_BY = {
    AnsweredBy.DECLARATION: "declaration",
    AnsweredBy.REAL_HOST: "pass_through",
    AnsweredBy.RECORDING: "recording",
    AnsweredBy.REFUSAL: "refused",
    AnsweredBy.EMULATOR: "emulator",
    AnsweredBy.MODEL: "model",
}
_MEMORY_OP = {
    Operation.READ: "get",
    Operation.SEARCH: "list",
    Operation.CREATE: "put",
    Operation.UPDATE: "put",
    Operation.DELETE: "delete",
}


def at(moment: datetime | None) -> str | None:
    """A moment as every view writes one: UTC, milliseconds, Z, so text order is time order."""
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ms(began: datetime | None, ended: datetime | None) -> float | None:
    if began is None or ended is None:
        return None
    return (ended - began).total_seconds() * 1000


def _cut(text: str | None) -> str | None:
    if text is None:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= SUMMARY else flat[: SUMMARY - 1] + "…"


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False)


def _attribute(value: AttributeValue | None) -> JsonValue:
    if value is None:
        return None
    if isinstance(value, StringValue | BoolValue | IntValue | DoubleValue | BytesValue):
        return value.value
    if isinstance(value, ArrayValue):
        return [_attribute(v) for v in value.values]
    assert isinstance(value, MapValue)
    return {a.key: _attribute(a.value) for a in value.values}


def _size(text: str | None, raw: bytes | None) -> int | None:
    if raw is not None:
        return len(raw)
    return len(text.encode("utf-8")) if text is not None else None


class _Tables:
    """The database being filled: one table per view, each row checked against the view's columns."""

    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        for view in VIEWS:
            columns = ", ".join(f'"{c.name}" {c.type.value}' for c in view.columns)
            self.db.execute(f'CREATE TABLE "{view.name}" ({columns})')

    def put(self, name: str, rows: Sequence[Row]) -> None:
        view = BY_NAME[name]
        names = [c.name for c in view.columns]
        for row in rows:
            if list(row) != names:
                raise AssertionError(f"a row of {name} holds {list(row)}, not the view's columns {names}")
            for column in view.columns:
                value = row[column.name]
                expected = {SqlType.TEXT: str, SqlType.INTEGER: int, SqlType.REAL: (int, float)}[column.type]
                if value is not None and not isinstance(value, expected):
                    raise AssertionError(f"{name}.{column.name} is {column.type.value}, not {type(value).__name__}")
        quoted = ", ".join(f'"{n}"' for n in names)
        marks = ", ".join("?" for _ in names)
        self.db.executemany(f'INSERT INTO "{name}" ({quoted}) VALUES ({marks})', [[r[n] for n in names] for r in rows])


def build(state: Path, run_id: str, prices: Prices | None = None) -> sqlite3.Connection:
    """The read model of `run_id` (a fork's is what the fork sees), in a database of its own in memory."""
    entry = session.find(state, run_id)
    scenario = session.scenario_of(state, run_id)
    outcome = session.load(state, run_id) if entry.finished else None
    with session.reading(state, run_id) as world:
        events = world.events()
        calls = world.calls()
        spans = world.spans()
        replies = world.replies()
        person_calls = world.person_calls()
        wakes = session.wakes_of(state, run_id, world)
        dues = due_entries(world)
        kept = checkpoints(world)
        joins = {
            e.seq: trace_of(e, world)
            for e in events
            if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot) and e.operation in WRITES
        }
    tables = _Tables()

    call_of: dict[int, int] = {}
    for number, call in enumerate(calls, start=1):
        for seq in range(call.first_seq, call.last_seq + 1):
            call_of[seq] = number
    by_caller = {
        caller.split("-")[2]: number
        for number, call in enumerate(calls, start=1)
        if (caller := call.exchange.traceparent) is not None and caller.count("-") >= 3
    }

    tables.put(
        "minutehand_schema",
        [
            {
                "version": VERSION,
                "view_name": view.name,
                "position": position,
                "column_name": column.name,
                "type": column.type.value,
                "description": column.description,
            }
            for view in sorted(VIEWS, key=lambda v: v.name)
            for position, column in enumerate(view.columns, start=1)
        ],
    )
    record = outcome.record if outcome is not None else None
    verdict = outcome.result.verdict if outcome is not None else None
    tables.put(
        "run",
        [
            {
                "run_id": run_id,
                "root_run": entry.root,
                "parent_run": entry.parent_run,
                "forked_at_seq": entry.forked_at,
                "scenario": scenario.name,
                "goal": scenario.goal,
                "seed": record.seed if record is not None else None,
                "starts_at": at(scenario.starts_at),
                "ended_at": at(record.ended_at) if record is not None else None,
                "stop": record.stop.value if record is not None else None,
                "verdict": verdict.kind.value if verdict is not None else None,
                "verdict_words": verdict.words if verdict is not None else None,
                "finished": int(entry.finished),
                "read_model_version": VERSION,
            }
        ],
    )
    tables.put("people", _people(scenario))
    tables.put(
        "events",
        [
            {
                "seq": e.seq,
                "run_id": e.run_id,
                "wake": e.wake,
                "at": at(e.sim_time),
                "wall_time": at(e.wall_time),
                "actor": e.actor.value,
                "operation": e.operation.value,
                "provider": e.entity.provider,
                "entity_kind": e.entity.kind.value,
                "entity_id": e.entity.external_id,
                "snapshot": e.after.model_dump_json() if e.after is not None else None,
                "call_id": call_of[e.seq] if e.seq in call_of else None,
            }
            for e in events
        ],
    )

    landed = _landed(replies, events)
    messages = _messages(scenario, events, wakes, replies, landed, joins, call_of)
    tables.put("messages", messages)
    tables.put("recipients", _recipients(scenario, events))
    tables.put("calls", [_call(number, call) for number, call in enumerate(calls, start=1)])
    tables.put("memory", _memory(events))
    tables.put(
        "stored",
        [
            {
                "seq": e.seq,
                "at": at(e.sim_time),
                "wake": e.wake,
                "actor": e.actor.value,
                "op": e.operation.value,
                "host": e.after.host,
                "collection": e.after.collection,
                "path": e.after.path,
                "item_id": e.after.id,
                "item": e.after.item,
            }
            for e in events
            if isinstance(e.after, StoredSnapshot)
        ],
    )
    tables.put(
        "dispatch",
        [
            {
                "due_id": number,
                "kind": d.due.kind.value,
                "ref": d.due.ref,
                "source": d.source.value,
                "due_at": at(d.due.at),
                "entered_at": at(d.entered_at),
                "entered_wake": d.entered_wake,
                "closed": d.closed.value if d.closed is not None else None,
                "closed_at": at(d.closed_at),
                "closed_wake": d.closed_wake,
                "fault": d.fault.value if d.fault is not None else None,
                "asked_for": at(d.asked_for),
                "drawn_from": d.drawn.source.value if d.drawn is not None else None,
                "drawn_offset_seconds": d.drawn.offset.total_seconds() if d.drawn is not None else None,
            }
            for number, d in enumerate(dues, start=1)
        ],
    )
    tables.put(
        "transitions",
        [
            {
                "seq": e.seq,
                "at": at(e.sim_time),
                "wake": e.wake,
                "provider": e.entity.provider,
                "item_kind": e.after.item.kind.value,
                "item_id": e.after.item.external_id,
                "name": e.after.name,
                "from_state": e.after.from_state,
                "to_state": e.after.to_state,
                "actor": e.actor.value,
                "who": e.after.who,
                "content": e.after.content,
                "call_id": call_of[e.seq] if e.seq in call_of else None,
            }
            for e in events
            if isinstance(e.after, TransitionSnapshot)
        ],
    )
    tables.put("items", _items(events))
    first_seq: dict[EntityRef, int] = {}
    for e in events:
        if e.entity not in first_seq:
            first_seq[e.entity] = e.seq
    tables.put("replies", _replies(replies, person_calls, first_seq, landed))

    agent_calls = [s for s in spans if is_model_call(s)]
    traces: dict[str, list[StoredSpan]] = {}
    for s in spans:
        traces.setdefault(s.span.trace_id, []).append(s)
    wrote: dict[str, list[int]] = {}
    for message in messages:
        span_id = message["model_call_span_id"]
        seq = message["seq"]
        if isinstance(span_id, str) and isinstance(seq, int):
            wrote.setdefault(span_id, []).append(seq)
    wake_times = {w.index: w.sim_time for w in wakes}
    prices = prices or Prices()
    model_rows: list[Row] = []
    for s in agent_calls:
        call = model_call(s, traces[s.span.trace_id])
        cost = prices.cost(call.model, call.input_tokens, call.output_tokens)
        model_rows.append(
            {
                "side": "agent",
                "span_id": s.span.span_id,
                "person_call_id": None,
                "source": s.source.value,
                "person": None,
                "wake": s.wake,
                "at": at(wake_times[s.wake] if s.wake in wake_times else s.sim_time),
                "started": at(s.span.start),
                "ended": at(s.span.end),
                "duration_ms": _ms(s.span.start, s.span.end),
                "model": call.model,
                "prompt_version": None,
                "input_tokens": call.input_tokens,
                "output_tokens": call.output_tokens,
                "cost": cost[0] if cost is not None else None,
                "currency": cost[1] if cost is not None else None,
                "trace_id": s.span.trace_id,
                "wrote": None,
                "wrote_seqs": _json(list(wrote[s.span.span_id])) if s.span.span_id in wrote else "[]",
                "replayed": None,
                "failure": None,
            }
        )
    for number, p in enumerate(person_calls, start=1):
        cost = prices.cost(p.model, p.input_tokens, p.output_tokens)
        model_rows.append(
            {
                "side": "person",
                "span_id": None,
                "person_call_id": number,
                "source": "person",
                "person": p.person,
                "wake": p.wake,
                "at": at(p.sim_time),
                "started": None,
                "ended": None,
                "duration_ms": None,
                "model": p.model,
                "prompt_version": p.prompt_version,
                "input_tokens": p.input_tokens,
                "output_tokens": p.output_tokens,
                "cost": cost[0] if cost is not None else None,
                "currency": cost[1] if cost is not None else None,
                "trace_id": None,
                "wrote": p.wrote.value,
                "wrote_seqs": _json([first_seq[p.asked]] if p.asked is not None and p.asked in first_seq else []),
                "replayed": int(p.replayed),
                "failure": p.failure,
            }
        )
    model_rows.sort(key=lambda r: (str(r["at"]), str(r["span_id"] or ""), int(r["person_call_id"] or 0)))
    tables.put("model_calls", model_rows)

    findings = outcome.result.findings if outcome is not None else []
    tables.put(
        "findings",
        [
            {
                "finding_id": number,
                "check_id": f.check,
                "kind": f.kind.value,
                "severity": f.severity.value,
                "message": f.message,
                "at": at(f.at),
                "wake": f.wake,
                "pattern": f.pattern,
                "pattern_title": pattern(f.pattern).title if f.pattern is not None else None,
                "evidence": _json(list(f.evidence)),
            }
            for number, f in enumerate(findings, start=1)
        ],
    )
    tables.put(
        "evidence",
        [
            {"finding_id": number, "seq": seq}
            for number, f in enumerate(findings, start=1)
            for seq in sorted(set(f.evidence))
        ],
    )
    tables.put(
        "spans",
        [
            {
                "span_id": s.span.span_id,
                "trace_id": s.span.trace_id,
                "parent_span_id": s.span.parent_span_id,
                "name": s.span.name,
                "service": s.span.service_name,
                "source": s.source.value,
                "wake": s.wake,
                "placed_by": s.placed_by.value,
                "arrived_wake": s.arrived_in_wake,
                "after_seq": s.after_seq,
                "at": at(s.sim_time),
                "started": at(s.span.start),
                "ended": at(s.span.end),
                "duration_ms": _ms(s.span.start, s.span.end),
                "status": s.span.status.value,
                "status_message": s.span.status_message,
                "attributes": _json({a.key: _attribute(a.value) for a in s.span.attributes}),
                "is_model_call": int(is_model_call(s)),
                "call_id": by_caller[s.span.span_id] if s.span.span_id in by_caller else None,
            }
            for s in sorted(spans, key=lambda s: (s.span.start, s.span.span_id))
        ],
    )
    actions = _actions(events, calls, agent_calls, traces, call_of, messages, wake_times)
    tables.put("actions", actions)
    tables.put("wakes", _wakes(wakes, kept, dues, actions, agent_calls))
    tables.db.commit()
    return tables.db


def _items(events: list[WorldEvent]) -> list[Row]:
    """Each item the people engine held pending, as its record last stood, with when it began and ended."""
    began: dict[str, WorldEvent] = {}
    last: dict[str, WorldEvent] = {}
    for e in events:
        if isinstance(e.after, PendingSnapshot):
            began.setdefault(e.entity.external_id, e)
            last[e.entity.external_id] = e
    rows: list[Row] = []
    for pending_id in sorted(began):
        first, latest = began[pending_id], last[pending_id]
        held = latest.after
        assert isinstance(held, PendingSnapshot)
        rows.append(
            {
                "pending_id": pending_id,
                "person": held.person,
                "nth": held.nth,
                "provider": held.item.provider,
                "item_kind": held.item.kind.value,
                "item_id": held.item.external_id,
                "state": held.state,
                "turn": held.turn,
                "since": at(first.sim_time),
                "status": held.status.value,
                "due_at": at(held.due_at),
                "take": held.take,
                "drawn_from": held.drawn.source.value if held.drawn is not None else None,
                "transition_seq": held.transition,
                "closed_at": at(latest.sim_time) if held.status is not PendingStatus.PENDING else None,
                "failure": held.failure,
            }
        )
    return rows


def _people(scenario: Scenario) -> list[Row]:
    rows: list[Row] = []
    for p in sorted(scenario.people, key=lambda p: p.key):
        hours = p.working_hours
        reply = p.reply
        rows.append(
            {
                "key": p.key,
                "name": p.name,
                "email": p.email,
                "is_owner": int(p.key == scenario.owner),
                "reply": "scripted"
                if isinstance(reply, Scripted)
                else "answers"
                if isinstance(reply, Answers)
                else "silent",
                "timezone": hours.timezone if hours is not None else None,
                "opens": hours.opens.isoformat() if hours is not None else None,
                "closes": hours.closes.isoformat() if hours is not None else None,
                "weekdays_only": int(hours.weekdays_only) if hours is not None else None,
            }
        )
    return rows


def _landed(replies: list[PersonReply], events: list[WorldEvent]) -> dict[int, int]:
    """Each reply's position, by the seq of the person's event it landed as: the first of that person's provider at
    its moment not already taken by another reply."""
    taken: set[int] = set()
    found: dict[int, int] = {}
    people_events = [
        e
        for e in events
        if e.actor is Actor.PERSON
        and e.operation in (Operation.CREATE, Operation.UPDATE)
        and isinstance(e.after, MessageSnapshot | InteractionSnapshot | InboxItemSnapshot)
    ]
    for position, reply in enumerate(replies):
        match = next(
            (
                e
                for e in people_events
                if e.seq not in taken and e.sim_time == reply.at and e.entity.provider == reply.in_reply_to.provider
            ),
            None,
        )
        if match is not None:
            taken.add(match.seq)
            found[position] = match.seq
    return found


def _messages(
    scenario: Scenario,
    events: list[WorldEvent],
    wakes: list[WakeRecord],
    replies: list[PersonReply],
    landed: dict[int, int],
    joins: dict[int, EventTrace],
    call_of: dict[int, int],
) -> list[Row]:
    view = view_of(scenario, events, wakes, replies)
    opened: dict[int, int] = {}
    following: dict[int, int] = {}
    for ask in asks(view):
        opened[ask.obligation.opened_by] = ask.obligation.opened_by
        for fact in ask.follow_ups:
            for seq in fact.seqs:
                if seq != ask.obligation.opened_by:
                    following.setdefault(seq, ask.obligation.opened_by)
    away = [a for a in absences(scenario, events) if a.delegate is not None]
    by_email = {p.email: p.key for p in scenario.people}
    first_seq: dict[EntityRef, int] = {}
    for e in events:
        if e.entity not in first_seq:
            first_seq[e.entity] = e.seq
    reply_at = {seq: replies[position] for position, seq in landed.items()}
    said: dict[EntityRef, str] = {}
    rows: list[Row] = []
    for e in events:
        after = e.after
        if not isinstance(after, MessageSnapshot) or e.operation not in WRITES:
            continue
        before = said.get(e.entity)
        said[e.entity] = after.text
        change = (
            "deleted"
            if e.operation is Operation.DELETE
            else "sent"
            if e.operation is Operation.CREATE
            else "edited"
            if before != after.text
            else "updated"
        )
        to = [by_email[m] for m in after.recipient_emails if m in by_email]
        reply = reply_at[e.seq] if e.seq in reply_at else None
        joined = joins[e.seq] if e.seq in joins else None
        found = joined.model_call if joined is not None else None
        joined_by = joined.joined_by if joined is not None else None
        agent = e.actor is Actor.AGENT
        rows.append(
            {
                "seq": e.seq,
                "run_id": e.run_id,
                "at": at(e.sim_time),
                "wake": e.wake,
                "change": change,
                "provider": e.entity.provider,
                "channel": after.channel,
                "thread_of": after.thread_of,
                "message_id": e.entity.external_id,
                "from_actor": e.actor.value,
                "from_person": reply.person if reply is not None else None,
                "to_people": _json(list(to)),
                "to_emails": _json(list(after.recipient_emails)),
                "text": after.text,
                "text_before": before if e.operation is Operation.UPDATE else None,
                "is_ask": int(agent and e.seq in opened),
                "is_follow_up": int(agent and e.seq in following),
                "ask_seq": opened[e.seq] if e.seq in opened else following[e.seq] if e.seq in following else None,
                "is_reply": int(reply is not None),
                "answers_seq": first_seq[reply.in_reply_to]
                if reply is not None and reply.in_reply_to in first_seq
                else None,
                "to_away": int(
                    agent
                    and e.operation is Operation.CREATE
                    and any(a.person in to and a.away_when_written_to(e.sim_time) for a in away)
                ),
                "model_call_span_id": found.span_id if found is not None else None,
                "joined_by": joined_by.value if joined_by is not None else None,
                "call_id": call_of[e.seq] if e.seq in call_of else None,
            }
        )
    return rows


def _recipients(scenario: Scenario, events: list[WorldEvent]) -> list[Row]:
    away = [a for a in absences(scenario, events) if a.delegate is not None]
    people = {p.email: p for p in scenario.people}
    rows: list[Row] = []
    for e in events:
        after = e.after
        if not isinstance(after, MessageSnapshot) or e.operation is not Operation.CREATE:
            continue
        for email in dict.fromkeys(after.recipient_emails):
            person = people[email] if email in people else None
            hours = person.working_hours if person is not None else None
            local = e.sim_time.astimezone(ZoneInfo(hours.timezone)) if hours is not None else None
            inside = (
                None
                if hours is None or local is None
                else (not hours.weekdays_only or local.weekday() < 5) and hours.opens <= local.time() < hours.closes
            )
            rows.append(
                {
                    "seq": e.seq,
                    "person": person.key if person is not None else None,
                    "email": email,
                    "local_time": local.isoformat() if local is not None else None,
                    "in_working_hours": _flag(inside),
                    "away": int(
                        person is not None
                        and any(a.person == person.key and a.away_when_written_to(e.sim_time) for a in away)
                    ),
                }
            )
    return sorted(rows, key=lambda r: (int(r["seq"] or 0), str(r["person"] or "")))


def _call(number: int, call: RecordedCall) -> Row:
    x = call.exchange
    captured, tunnel = x.captured, x.tunnelled
    if x.inbox_call is not None:
        answered = "minutehand"
    elif tunnel is not None:
        answered = "tunnel"
    elif captured is not None:
        answered = _ANSWERED_BY[captured.answered_by]
    elif call.provider is not None:
        answered = "provider"
    else:
        answered = "refused"
    began = captured.started if captured is not None else tunnel.started if tunnel is not None else None
    ended = captured.ended if captured is not None else tunnel.ended if tunnel is not None else None
    parts = x.traceparent.strip().lower().split("-") if x.traceparent is not None else []
    wrote = call.first_seq <= call.last_seq
    return {
        "call_id": number,
        "at": at(call.sim_time),
        "wake": call.wake,
        "provider": call.provider,
        "host": x.host,
        "method": x.method,
        "path": x.path,
        "status": x.status,
        "outcome": x.outcome.value if x.outcome is not None else None,
        "answered_by": answered,
        "capture_mode": captured.mode.value if captured is not None else None,
        "declared_as": captured.declared_as if captured is not None else None,
        "is_agent": int(x.inbox_call is None),
        "request_body": x.request_body,
        "response_body": x.response_body,
        "request_binary": int(x.request_bytes is not None),
        "response_binary": int(x.response_bytes is not None),
        "request_size": _size(x.request_body, x.request_bytes),
        "response_size": _size(x.response_body, x.response_bytes),
        "first_seq": call.first_seq if wrote else None,
        "last_seq": call.last_seq if wrote else None,
        "trace_id": parts[1] if len(parts) >= 4 else None,
        "caller_span_id": parts[2] if len(parts) >= 4 else None,
        "started": at(began),
        "ended": at(ended),
        "duration_ms": _ms(began, ended),
        "grpc_status": x.grpc.code.value if x.grpc is not None else None,
        "frame_sender": x.frame.sender.value if x.frame is not None else None,
        "failure": x.failure.message if x.failure is not None else None,
    }


def _memory(events: list[WorldEvent]) -> list[Row]:
    held: dict[tuple[str, str], str | None] = {}
    rows: list[Row] = []
    for e in events:
        after = e.after
        if not isinstance(after, MemorySnapshot):
            continue
        place = (after.collection, after.key)
        if e.operation in (Operation.CREATE, Operation.UPDATE):
            held[place] = after.value
        elif e.operation is Operation.DELETE:
            held[place] = None
        value = (
            after.value
            if e.operation in (Operation.CREATE, Operation.UPDATE)
            else held[place]
            if e.operation is Operation.READ and place in held
            else None
        )
        rows.append(
            {
                "seq": e.seq,
                "at": at(e.sim_time),
                "wake": e.wake,
                "actor": e.actor.value,
                "op": _MEMORY_OP[e.operation],
                "collection": after.collection,
                "key": after.key,
                "value": value,
            }
        )
    return rows


def _replies(
    replies: list[PersonReply],
    person_calls: list[PersonCall],
    first_seq: dict[EntityRef, int],
    landed: dict[int, int],
) -> list[Row]:
    rows: list[Row] = []
    for position, r in enumerate(replies):
        written = r.written_by
        call = (
            next(
                (
                    n
                    for n, p in enumerate(person_calls, start=1)
                    if p.person == r.person
                    and p.asked == r.in_reply_to
                    and p.wrote in (Wrote.REPLY, Wrote.DECISION)
                    and p.answer is not None
                ),
                None,
            )
            if written is not None
            else None
        )
        kind = (
            "decision"
            if r.decides is not None
            else "press"
            if r.press is not None
            else "automatic"
            if r.writing is Writing.AUTOMATIC
            else "message"
        )
        by = (
            "model"
            if written is not None
            else "verbatim"
            if r.writing is Writing.VERBATIM
            else "automatic"
            if r.writing is Writing.AUTOMATIC
            else "by_hand"
        )
        ref = r.in_reply_to
        rows.append(
            {
                "reply_id": position + 1,
                "person": r.person,
                "at": at(r.at),
                "kind": kind,
                "writing": r.writing.value,
                "written_by": by,
                "model": written.model if written is not None else None,
                "prompt_version": written.prompt_version if written is not None else None,
                "text": r.text,
                "facts": _json(list(r.facts)),
                "decision": r.decides.decision if r.decides is not None else None,
                "answers_seq": first_seq[ref] if ref in first_seq else None,
                "in_reply_to": f"{ref.provider}/{ref.kind.value}/{ref.external_id}",
                "seq": landed[position] if position in landed else None,
                "drawn_from": r.drawn.source.value if r.drawn is not None else None,
                "person_call_id": call,
            }
        )
    return rows


def _actions(
    events: list[WorldEvent],
    calls: list[RecordedCall],
    agent_calls: list[StoredSpan],
    traces: dict[str, list[StoredSpan]],
    call_of: dict[int, int],
    messages: list[Row],
    wake_times: dict[int, datetime],
) -> list[Row]:
    """The agent's acts in the order it made them: each event by its seq; a call that wrote nothing after the event
    the log had reached when it was recorded; a model call after the last event written before it began, real time."""
    to_person = {r["seq"]: json.loads(str(r["to_people"])) for r in messages}
    keyed: list[tuple[tuple[int, int, float], Row]] = []
    for e in events:
        if e.actor is not Actor.AGENT:
            continue
        after = e.after
        kind, view = "write", "events"
        target = e.entity.external_id
        person: str | None = None
        summary: str | None = None
        if isinstance(after, MessageSnapshot) and e.operation in WRITES:
            kind, view, target = "message", "messages", after.channel
            people = to_person[e.seq] if e.seq in to_person else []
            person = people[0] if people else None
            summary = after.text
        elif isinstance(after, MemorySnapshot):
            kind, view, target = "memory", "memory", f"{after.collection}/{after.key}"
            summary = f"{_MEMORY_OP[e.operation]} {after.key}" + (
                f" = {after.value}" if after.value is not None else ""
            )
        elif isinstance(after, StoredSnapshot):
            kind, view, target = "stored", "stored", f"{after.host}{after.path}/{after.id}"
            summary = after.item
        elif isinstance(after, NextWakeSnapshot):
            kind = "next_wake"
            summary = f"next wake {at(after.at)}" if after.at is not None else "no next wake"
        elif e.operation in READS:
            kind = "read"
            summary = f"{e.operation.value} {e.entity.kind.value} {e.entity.external_id}"
        else:
            summary = f"{e.operation.value} {e.entity.kind.value} {e.entity.external_id}"
            if after is not None:
                summary += f": {after.model_dump_json(exclude={'kind'})}"
        keyed.append(
            (
                (e.seq, 0, 0.0),
                {
                    "position": 0,
                    "seq": e.seq,
                    "call_id": call_of[e.seq] if e.seq in call_of else None,
                    "span_id": None,
                    "at": at(e.sim_time),
                    "wake": e.wake,
                    "kind": kind,
                    "operation": e.operation.value,
                    "provider": e.entity.provider,
                    "target": target,
                    "person": person,
                    "summary": _cut(summary),
                    "view": view,
                },
            )
        )
    for number, call in enumerate(calls, start=1):
        x = call.exchange
        if call.first_seq <= call.last_seq or x.inbox_call is not None:
            continue
        keyed.append(
            (
                (call.last_seq, 1, float(number)),
                {
                    "position": 0,
                    "seq": None,
                    "call_id": number,
                    "span_id": None,
                    "at": at(call.sim_time),
                    "wake": call.wake,
                    "kind": "call",
                    "operation": x.method,
                    "provider": call.provider or x.host,
                    "target": f"{x.host}{x.path}" if x.tunnelled is None else x.host,
                    "person": None,
                    "summary": _cut(f"{x.method} {x.host}{x.path if x.tunnelled is None else ''} -> {x.status}"),
                    "view": "calls",
                },
            )
        )
    walls: list[datetime] = []
    seqs: list[int] = []
    for e in events:
        walls.append(max(e.wall_time, walls[-1]) if walls else e.wall_time)
        seqs.append(e.seq)
    for s in agent_calls:
        found = bisect.bisect_right(walls, s.span.start)
        anchor = seqs[found - 1] if found else 0
        call = model_call(s, traces[s.span.trace_id])
        tokens = f"{call.input_tokens if call.input_tokens is not None else '?'} in, " + (
            f"{call.output_tokens if call.output_tokens is not None else '?'} out"
        )
        keyed.append(
            (
                (anchor, 2, s.span.start.timestamp()),
                {
                    "position": 0,
                    "seq": None,
                    "call_id": None,
                    "span_id": s.span.span_id,
                    "at": at(wake_times[s.wake] if s.wake in wake_times else s.sim_time),
                    "wake": s.wake,
                    "kind": "model_call",
                    "operation": "chat",
                    "provider": call.system,
                    "target": call.model,
                    "person": None,
                    "summary": _cut(f"{call.model or 'a model'}: {tokens} tokens"),
                    "view": "model_calls",
                },
            )
        )
    keyed.sort(key=lambda k: k[0])
    rows: list[Row] = []
    for position, (_, row) in enumerate(keyed, start=1):
        row["position"] = position
        rows.append(row)
    return rows


def _wakes(
    wakes: list[WakeRecord],
    kept: dict[int, Checkpoint],
    dues: list[DueEntry],
    actions: list[Row],
    agent_calls: list[StoredSpan],
) -> list[Row]:
    ended = {c.wake: (seq, c) for seq, c in kept.items()}
    rows: list[Row] = []
    for w in wakes:
        fired = [d.source.value for d in dues if d.closed is DueClosed.FIRED and d.closed_at == w.sim_time]
        point = ended[w.index] if w.index in ended else None
        report = point[1].agent.report if point is not None else None
        rows.append(
            {
                "wake": w.index,
                "at": at(w.sim_time),
                "reason": w.reason,
                "woken_by": ",".join(dict.fromkeys(fired)) or None,
                "world_changes": w.world_changes,
                "memory_reads": w.memory_reads,
                "memory_writes": w.memory_writes,
                "commitments_changed": int(w.commitments_changed),
                "checkpoint_seq": point[0] if point is not None else None,
                "reported_status": report.status.value if report is not None else None,
                "reported_next_wake": at(report.next_wake) if report is not None else None,
                "actions": sum(1 for a in actions if a["wake"] == w.index),
                "model_calls": sum(1 for s in agent_calls if s.wake == w.index),
            }
        )
    return rows
