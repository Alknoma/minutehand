"""What the viewer's newer paths read out of one run: its marks for the timeline, one thing whole for the
inspector, and the tables of its views (calls, the dispatch table, memory, stored items, people, the team's rules,
`run-all` batches). `app.py` routes to these; each answers a model of `responses.py`."""

from __future__ import annotations

import sqlite3
from bisect import bisect_right
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from minutehand import run_all, session
from minutehand.adapters.web.responses import (
    AssessmentsResponse,
    BatchesResponse,
    BatchRow,
    CallDetail,
    CallRow,
    ConversationLine,
    DispatchResponse,
    DueRow,
    EventDetail,
    FindingMark,
    Lane,
    LaneKind,
    MarkKind,
    Marks,
    MemoryChange,
    MemoryKey,
    MemoryResponse,
    MessageChange,
    MessageLine,
    ModelUse,
    ModelUseResponse,
    PeopleResponse,
    PersonActivity,
    PersonCallLine,
    ReplyLine,
    RuleOutcome,
    RuleStatus,
    SampleRow,
    ScenarioSamples,
    StoredChange,
    StoredResponse,
    TimelineResponse,
    WrittenBy,
)
from minutehand.application.checkpoint import CHECKPOINT, read_checkpoint
from minutehand.application.dues import due_entries
from minutehand.application.model_calls import is_model_call, trace_of
from minutehand.application.steps import STEP
from minutehand.checks.runner import RunResult, view_of
from minutehand.domain.assessments import merged
from minutehand.domain.checks import FindingKind, Obligation, WakeRecord
from minutehand.domain.clock import DueClosed
from minutehand.domain.inboxes import item_words
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import ExpectedOutcome, Scenario
from minutehand.domain.telemetry import SpanSource
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    InboxItemSnapshot,
    InteractionSnapshot,
    ItemStatus,
    MemorySnapshot,
    MessageSnapshot,
    Operation,
    RecordedCall,
    Snapshot,
    StoredSnapshot,
    WorldEvent,
)
from minutehand.domain.world import AnsweredBy as AnsweredByWire
from minutehand.ports.store import Store

WRITES = frozenset({Operation.CREATE, Operation.UPDATE, Operation.DELETE})
OWN = frozenset({EntityKind.DUE, EntityKind.NEXT_WAKE})
"""Kinds the run loop writes for itself: drawn from the dispatch table, never as changes in the world."""
LABEL = 80
"""The most characters of a mark's label: the inspector reads the rest."""
FAILED_DUE = frozenset({DueClosed.DELAYED, DueClosed.DROPPED})


def _ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


def _clip(text: str, most: int = LABEL) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= most else flat[: most - 1] + "…"


_LOOP = frozenset((r.provider, r.kind, r.external_id) for r in (CHECKPOINT, STEP))


def _loop_own(event: WorldEvent) -> bool:
    """A checkpoint or a step mark: the run loop's own row, by its fields (cheaper than comparing models)."""
    return (event.entity.provider, event.entity.kind, event.entity.external_id) in _LOOP


def world_events(world: Store) -> list[WorldEvent]:
    """The world's log without the run loop's own checkpoints and steps."""
    return [e for e in world.events() if not _loop_own(e)]


def _failed(call: RecordedCall) -> bool:
    """Refused, answered with an error status, or answered by Minutehand in the fake's place."""
    captured = call.exchange.captured
    return (
        call.refused
        or (captured is not None and captured.answered_by is AnsweredByWire.REFUSAL)
        or call.exchange.status >= 400
        or call.exchange.failure is not None
    )


# -- what the read model holds (`adapters/query`): the tables the viewer shares with `minutehand query` -----------


def _rows(db: sqlite3.Connection, sql: str, *args: object) -> list[dict[str, object]]:
    cursor = db.execute(sql, args)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


_CALL_COLUMNS = (
    "call_id, at, wake, provider, host, method, path, status, outcome, answered_by, capture_mode, request_size, "
    "response_size, first_seq, last_seq, duration_ms"
)


def call_rows(db: sqlite3.Connection) -> list[CallRow]:
    return [CallRow.model_validate(r) for r in _rows(db, f"SELECT {_CALL_COLUMNS} FROM calls ORDER BY call_id")]


def call_detail(db: sqlite3.Connection, world: Store, call_id: int) -> CallDetail | None:
    found = _rows(db, f"SELECT {_CALL_COLUMNS} FROM calls WHERE call_id = ?", call_id)
    calls = world.calls()
    if not found or not 1 <= call_id <= len(calls):
        return None
    return CallDetail(row=CallRow.model_validate(found[0]), call=calls[call_id - 1])


def dispatch(db: sqlite3.Connection, world: Store) -> DispatchResponse:
    entries = due_entries(world)
    rows = _rows(db, "SELECT * FROM dispatch ORDER BY due_id")
    return DispatchResponse(
        entries=[
            DueRow.model_validate(
                {**r, "drawn": entries[int(str(r["due_id"])) - 1].drawn if len(entries) == len(rows) else None}
            )
            for r in rows
        ]
    )


def memory(db: sqlite3.Connection) -> MemoryResponse:
    changes: list[MemoryChange] = []
    keys: dict[tuple[str, str], MemoryKey] = {}
    reads = 0
    for r in _rows(db, "SELECT seq, at, wake, actor, op, collection, key, value FROM memory ORDER BY seq"):
        collection, key, op = str(r["collection"]), str(r["key"]), r["op"]
        had = keys[(collection, key)] if (collection, key) in keys else None
        if op in ("put", "delete"):  # enum-lint: exempt the read model's memory.op text (docs/querying.md)
            value = str(r["value"]) if op == "put" and r["value"] is not None else None
            changes.append(
                MemoryChange.model_validate(
                    {k: v for k, v in r.items() if k != "op"}  # enum-lint: exempt a column name of the read model
                    | {"value": value, "before": had.value if had else None}
                )
            )
            keys[(collection, key)] = MemoryKey(
                collection=collection,
                key=key,
                writes=(had.writes if had is not None else 0) + 1,
                reads=had.reads if had is not None else 0,
                value=value,
            )
            continue
        reads += 1
        if op == "get":
            keys[(collection, key)] = MemoryKey(
                collection=collection,
                key=key,
                writes=had.writes if had is not None else 0,
                reads=(had.reads if had is not None else 0) + 1,
                value=had.value if had is not None else None,
            )
    return MemoryResponse(keys=[keys[k] for k in sorted(keys)], changes=changes, reads=reads)


def stored(db: sqlite3.Connection) -> StoredResponse:
    changes: list[StoredChange] = []
    held: dict[tuple[str, str, str], str | None] = {}
    for r in _rows(db, "SELECT seq, at, wake, op, host, collection, path, item_id, item FROM stored ORDER BY seq"):
        item = (str(r["host"]), str(r["collection"]), str(r["item_id"]))
        changes.append(
            StoredChange.model_validate(
                {k: v for k, v in r.items() if k not in ("op", "item_id")}  # enum-lint: exempt read model columns
                | {"operation": r["op"], "id": r["item_id"], "before": held[item] if item in held else None}
            )
        )
        held[item] = str(r["item"]) if r["item"] is not None else None
    return StoredResponse(changes=changes)


def model_use(db: sqlite3.Connection, priced: bool) -> ModelUseResponse:
    rows = _rows(
        db,
        "SELECT side, span_id, person_call_id, person, wake, at, duration_ms, model, input_tokens, output_tokens, cost,"
        " currency, wrote, replayed, failure FROM model_calls ORDER BY at, span_id, person_call_id",
    )
    return ModelUseResponse(calls=[ModelUse.model_validate(r) for r in rows], priced=priced)


# -- people and what they said ------------------------------------------------------------------------------------


def _delivered(events: Sequence[WorldEvent], replies: Sequence[PersonReply]) -> dict[int, int]:
    """Each world event that delivered a person's reply, by its seq, to the reply's index: the message carrying the
    reply's words at the moment it landed, the press or the decision the person made then."""
    found: dict[int, int] = {}
    taken: set[int] = set()
    for event in events:
        if event.actor is not Actor.PERSON or event.operation not in WRITES:
            continue
        after = event.after
        for i, reply in enumerate(replies, start=1):
            if i in taken or reply.at != event.sim_time:
                continue
            if (
                (isinstance(after, MessageSnapshot) and after.text == reply.text)
                or (isinstance(after, InteractionSnapshot) and reply.press is not None and after.person == reply.person)
                or (isinstance(after, InboxItemSnapshot) and reply.decides is not None and after.person == reply.person)
            ):
                found[event.seq] = i
                taken.add(i)
                break
    return found


def _change(event: WorldEvent, before: str | None, text: str) -> MessageChange | None:
    """What a message event did; None for an update that left its text as it was (a reaction, a button redrawn)."""
    if event.operation is Operation.DELETE:
        return MessageChange.DELETED
    if event.operation is Operation.UPDATE:
        return None if before == text else MessageChange.EDITED
    return MessageChange.SENT


def message_lines(events: Sequence[WorldEvent], scenario: Scenario, world: Store | None = None) -> list[MessageLine]:
    """Every message sent, rewritten or deleted, and every item in the agent's own product, as a person reads the
    record. With `world`, each of the agent's is joined to the model call that wrote it (a read of its spans each)."""
    names = {p.email: p.name for p in scenario.people}
    said: dict[EntityRef, str] = {}
    lines: list[MessageLine] = []
    for event in events:
        after = event.after
        if isinstance(after, InboxItemSnapshot):
            lines.append(_item_line(event, after, names))
            continue
        if not isinstance(after, MessageSnapshot) or event.operation not in WRITES:
            continue
        before = said.get(event.entity)
        said[event.entity] = after.text
        change = _change(event, before, after.text)
        if change is None:
            continue
        joined = trace_of(event, world) if world is not None and event.actor is Actor.AGENT else None
        lines.append(
            MessageLine(
                seq=event.seq,
                at=event.sim_time,
                wake=event.wake,
                actor=event.actor,
                change=change,
                to=[names[e] if e in names else e for e in after.recipient_emails],
                text=after.text,
                before=before if change is MessageChange.EDITED else None,
                thread=after.thread_of is not None,
                written_by=WrittenBy(
                    span_id=joined.model_call.span_id, model=joined.model_call.model, joined_by=joined.joined_by
                )
                if joined is not None and joined.model_call is not None and joined.joined_by is not None
                else None,
            )
        )
    return lines


def _item_line(event: WorldEvent, item: InboxItemSnapshot, names: dict[str, str]) -> MessageLine:
    """An item in the agent's own product as a line of what was said: the ask, the decision, a refusal of it, or the
    agent taking it back."""
    who = names[item.waits_on] if item.waits_on in names else item.waits_on
    if event.actor is Actor.PERSON:
        change = MessageChange.DECIDED if item.status is ItemStatus.DECIDED else MessageChange.REFUSED
    else:
        change = MessageChange.WITHDRAWN if item.status is ItemStatus.WITHDRAWN else MessageChange.ASKED
    return MessageLine(
        seq=event.seq,
        at=event.sim_time,
        wake=event.wake,
        actor=event.actor,
        change=change,
        to=[who],
        text=item.summary,
        before=None,
        thread=False,
        words=item_words(item, event.actor, who),
    )


def people(world: Store, scenario: Scenario) -> PeopleResponse:
    events = world_events(world)
    replies = world.replies()
    calls = world.person_calls()
    delivered = _delivered(events, replies)
    seq_of = {i: seq for seq, i in delivered.items()}
    asked_seq = {(e.entity.provider, e.entity.kind, e.entity.external_id): e.seq for e in reversed(events)}
    found: list[PersonActivity] = []
    for person in scenario.people:
        lines: list[ConversationLine] = []
        said: dict[EntityRef, str] = {}
        for event in events:
            after = event.after
            if isinstance(after, MessageSnapshot) and event.operation in WRITES:
                before = said.get(event.entity)
                said[event.entity] = after.text
                if event.actor is not Actor.AGENT or person.email not in after.recipient_emails:
                    continue
                change = _change(event, before, after.text)
                if change is None:
                    continue
                lines.append(
                    ConversationLine(
                        seq=event.seq,
                        at=event.sim_time,
                        from_agent=True,
                        change=change,
                        text=after.text,
                        before=before if change is MessageChange.EDITED else None,
                        writing=None,
                        written_by=None,
                        reply=None,
                    )
                )
            elif isinstance(after, InboxItemSnapshot) and after.person == person.key and event.actor is Actor.AGENT:
                lines.append(
                    ConversationLine(
                        seq=event.seq,
                        at=event.sim_time,
                        from_agent=True,
                        change=MessageChange.WITHDRAWN if after.status is ItemStatus.WITHDRAWN else MessageChange.ASKED,
                        text=after.summary,
                        before=None,
                        writing=None,
                        written_by=None,
                        reply=None,
                    )
                )
        own = [(i, r) for i, r in enumerate(replies, start=1) if r.person == person.key]
        for i, reply in own:
            lines.append(
                ConversationLine(
                    seq=seq_of[i] if i in seq_of else None,
                    at=reply.at,
                    from_agent=False,
                    change=MessageChange.DECIDED if reply.decides is not None else MessageChange.SENT,
                    text=reply.text,
                    before=None,
                    writing=reply.writing,
                    written_by=reply.written_by.model if reply.written_by is not None else None,
                    reply=i,
                )
            )
        # At one instant a person's reply comes before what the agent wrote on hearing it.
        lines.sort(key=lambda line: (line.at, line.from_agent, line.seq or 0))
        found.append(
            PersonActivity(
                key=person.key,
                name=person.name,
                email=person.email,
                conversation=lines,
                replies=[
                    ReplyLine(
                        index=i,
                        reply=r,
                        seq=seq_of[i] if i in seq_of else None,
                        asked=asked_seq.get((r.in_reply_to.provider, r.in_reply_to.kind, r.in_reply_to.external_id)),
                    )
                    for i, r in own
                ],
                model_calls=[
                    PersonCallLine(index=i, call=c) for i, c in enumerate(calls, start=1) if c.person == person.key
                ],
            )
        )
    return PeopleResponse(people=found)


# -- one event, whole -------------------------------------------------------------------------------------------------


def event_detail(world: Store, seq: int, scenario: Scenario, result: RunResult | None) -> EventDetail | None:
    events = world_events(world)
    found = next((e for e in events if e.seq == seq), None)
    if found is None:
        return None
    before: Snapshot | None = None
    for event in events:
        if event.seq >= seq:
            break
        if event.entity == found.entity and event.after is not None:
            before = event.after
    calls = world.calls()
    call = next((i for i, c in enumerate(calls, start=1) if c.first_seq <= seq <= c.last_seq), None)
    thread: list[MessageLine] = []
    written_by: WrittenBy | None = None
    after = found.after
    if isinstance(after, MessageSnapshot):
        same = [e for e in events if isinstance(e.after, MessageSnapshot) and e.after.channel == after.channel]
        thread = message_lines(same, scenario)
        if found.actor is Actor.AGENT:
            joined = trace_of(found, world)
            if joined.model_call is not None and joined.joined_by is not None:
                written_by = WrittenBy(
                    span_id=joined.model_call.span_id, model=joined.model_call.model, joined_by=joined.joined_by
                )
    delivered = _delivered(events, world.replies())
    return EventDetail(
        event=found,
        before=before,
        call=call,
        thread=thread,
        written_by=written_by,
        reply=delivered[seq] if seq in delivered else None,
        findings=[i for i, f in enumerate(result.findings, start=1) if seq in f.evidence] if result else [],
    )


# -- the timeline ---------------------------------------------------------------------------------------------------


class _Builder:
    """Marks, gathered unsorted, then sorted once by simulated time."""

    def __init__(self, pairs: list[tuple[int, int]]) -> None:
        self.pairs = sorted(pairs)
        self.sims = [p[0] for p in self.pairs]
        self.lanes: list[Lane] = []
        self.index: dict[str, int] = {}
        self.rows: list[tuple[int, int | None, int, int | None, int, MarkKind, str, str]] = []

    def lane(self, key: str, kind: LaneKind, label: str) -> int:
        if key not in self.index:
            self.index[key] = len(self.lanes)
            self.lanes.append(Lane(key=key, kind=kind, label=label, marks=0))
        return self.index[key]

    def real(self, sim: int) -> int:
        """The real moment of the latest event recorded at or before `sim` on the simulated clock."""
        at = bisect_right(self.sims, sim) - 1
        if at >= 0:
            return self.pairs[at][1]
        return self.pairs[0][1] if self.pairs else sim

    def add(
        self,
        lane: int,
        kind: MarkKind,
        ref: str,
        label: str,
        sim: datetime,
        *,
        sim_end: datetime | None = None,
        real: datetime | None = None,
        real_end: datetime | None = None,
    ) -> None:
        s = _ms(sim)
        e = _ms(sim_end) if sim_end is not None else None
        r = _ms(real) if real is not None else self.real(s)
        re = _ms(real_end) if real_end is not None else (self.real(e) if e is not None else None)
        self.rows.append((s, e, r, re, lane, kind, ref, _clip(label)))

    def marks(self) -> tuple[list[Lane], Marks]:
        self.rows.sort(key=lambda r: (r[0], r[2]))
        counts = [0] * len(self.lanes)
        for row in self.rows:
            counts[row[4]] += 1
        lanes = [lane.model_copy(update={"marks": counts[i]}) for i, lane in enumerate(self.lanes)]
        return lanes, Marks(
            sim=[r[0] for r in self.rows],
            sim_end=[r[1] for r in self.rows],
            real=[r[2] for r in self.rows],
            real_end=[r[3] for r in self.rows],
            lane=[r[4] for r in self.rows],
            kind=[r[5] for r in self.rows],
            ref=[r[6] for r in self.rows],
            label=[r[7] for r in self.rows],
        )


def obligations(state: Path, run_id: str, world: Store, events: list[WorldEvent]) -> list[Obligation]:
    """What the world was waiting on (`checks.ledger`), as the checks read it."""
    last = read_checkpoint(world)
    return view_of(
        session.scenario_of(state, run_id),
        events,
        session.wakes_of(state, run_id, world),
        world.replies(),
        withdrawn=last.withdrawn if last is not None else [],
    ).obligations


def timeline(state: Path, run_id: str, world: Store, result: RunResult | None) -> TimelineResponse:
    scenario = session.scenario_of(state, run_id)
    everything = world.events()
    events = [e for e in everything if not _loop_own(e)]
    wakes = session.wakes_of(state, run_id, world)
    calls = world.calls()
    spans = world.spans()
    replies = world.replies()
    b = _Builder([(_ms(e.sim_time), _ms(e.wall_time)) for e in everything])
    by_email = {p.email: p for p in scenario.people}
    by_key = {p.key: p for p in scenario.people}
    driven = session.driven(state, run_id)

    # the agent's wakes, each from its first act to its last on the real clock
    wakes_lane = b.lane("wakes", LaneKind.WAKES, "Agent steps" if driven else "Agent wakes")
    real_of: dict[int, list[datetime]] = {}
    for event in everything:
        real_of.setdefault(event.wake, []).append(event.wall_time)
    for s in spans:
        real_of.setdefault(s.wake, []).extend((s.span.start, s.span.end))
    sim_of_wake = {w.index: w.sim_time for w in wakes}
    for w in wakes:
        moments = real_of[w.index] if w.index in real_of else []
        b.add(
            wakes_lane,
            MarkKind.WAKE,
            f"wake:{w.index}",
            _wake_label(w, driven),
            w.sim_time,
            real=min(moments) if moments else None,
            real_end=max(moments) if moments else None,
        )
    for p in scenario.people:
        b.lane(f"person:{p.key}", LaneKind.PERSON, p.name)

    # what passed between the agent and each person
    delivered = _delivered(events, replies)
    covered = {seq for c in calls for seq in range(c.first_seq, c.last_seq + 1)}
    said: dict[EntityRef, str] = {}
    for event in events:
        after = event.after
        kind = event.entity.kind
        if kind in OWN:
            continue
        if kind is EntityKind.MEMORY:
            lane = b.lane("memory", LaneKind.MEMORY, "Agent memory")
            write = event.operation in WRITES
            key = after.key if isinstance(after, MemorySnapshot) else event.entity.external_id
            verb = ("deleted " if event.operation is Operation.DELETE else "wrote ") if write else "read "
            b.add(
                lane,
                MarkKind.MEMORY_WRITE if write else MarkKind.MEMORY_READ,
                f"ev:{event.seq}",
                verb + key,
                event.sim_time,
                real=event.wall_time,
            )
            continue
        if kind is EntityKind.STORED and isinstance(after, StoredSnapshot):
            lane = b.lane("stored", LaneKind.STORED, "Stored items")
            b.add(
                lane,
                MarkKind.STORED,
                f"ev:{event.seq}",
                f"{event.operation} {after.collection}/{after.id}",
                event.sim_time,
                real=event.wall_time,
            )
            continue
        if isinstance(after, MessageSnapshot) and event.operation in WRITES:
            before = said.get(event.entity)
            said[event.entity] = after.text
            change = _change(event, before, after.text)
            if change is None:
                continue
            if event.actor is Actor.AGENT:
                reached = [by_email[e] for e in after.recipient_emails if e in by_email]
                for p in reached:
                    b.add(
                        b.index[f"person:{p.key}"],
                        MarkKind.SENT if change is MessageChange.SENT else MarkKind.EDITED,
                        f"ev:{event.seq}",
                        after.text,
                        event.sim_time,
                        real=event.wall_time,
                    )
                if reached:
                    continue
            elif event.seq in delivered:
                continue  # drawn as the reply it delivered
        if isinstance(after, InteractionSnapshot) and event.seq in delivered:
            continue
        if isinstance(after, InboxItemSnapshot) and after.person in by_key:
            if event.seq in delivered:
                continue
            b.add(
                b.index[f"person:{after.person}"],
                MarkKind.SENT if event.actor is Actor.AGENT else MarkKind.SAID,
                f"ev:{event.seq}",
                after.summary,
                event.sim_time,
                real=event.wall_time,
            )
            continue
        if event.actor is Actor.AGENT and event.seq in covered:
            continue  # drawn as the call that made it
        provider = event.entity.provider
        lane = b.lane(f"provider:{provider}", LaneKind.PROVIDER, provider)
        read = event.operation not in WRITES
        b.add(
            lane,
            MarkKind.READ if read else MarkKind.CHANGE,
            f"ev:{event.seq}",
            f"{event.actor} {event.operation} {kind}" + (f": {_snapshot_words(after)}" if after is not None else ""),
            event.sim_time,
            real=event.wall_time,
        )
    for i, reply in enumerate(replies, start=1):
        if reply.person not in by_key:
            continue
        b.add(b.index[f"person:{reply.person}"], MarkKind.SAID, f"reply:{i}", reply.text, reply.at)
    finished = session.find(state, run_id).finished
    record_end = session.load(state, run_id).record.ended_at if finished else None
    reached_at = record_end or max([e.sim_time for e in everything] + [scenario.starts_at])
    for o in obligations(state, run_id, world, events):
        if o.person is None or o.person not in by_key:
            continue
        b.add(
            b.index[f"person:{o.person}"],
            MarkKind.WAIT,
            f"wait:{o.key}",
            f"waiting on {by_key[o.person].name}" + ("" if o.settled_at is not None else ", still open"),
            o.opened_at,
            sim_end=o.settled_at or max(reached_at, o.opened_at),
        )

    # the calls, by provider or host
    walls = {e.seq: e.wall_time for e in everything}
    for i, call in enumerate(calls, start=1):
        e = call.exchange
        if call.provider is not None:
            lane = b.lane(f"provider:{call.provider}", LaneKind.PROVIDER, call.provider)
        else:
            lane = b.lane(f"host:{e.host}", LaneKind.HOST, e.host)
        started = (
            e.captured.started
            if e.captured is not None
            else e.tunnelled.started
            if e.tunnelled is not None
            else walls[call.first_seq]
            if call.first_seq in walls
            else None
        )
        ended = e.captured.ended if e.captured is not None else e.tunnelled.ended if e.tunnelled is not None else None
        b.add(
            lane,
            MarkKind.CALL_FAILED if _failed(call) else MarkKind.CALL,
            f"call:{i}",
            f"{e.method} {e.path.split('?')[0]} → {e.status}",
            call.sim_time,
            real=started,
            real_end=ended,
        )

    # the dispatch table, at the moment each entry was due
    for i, entry in enumerate(due_entries(world), start=1):
        lane = b.lane("dispatch", LaneKind.DISPATCH, "Dispatch table")
        faulted = entry.fault is not None or entry.closed in FAILED_DUE
        b.add(
            lane,
            MarkKind.DUE_FAULT if faulted else MarkKind.DUE,
            f"due:{i}",
            f"{entry.due.kind} ({entry.source})" + (f", {entry.closed}" if entry.closed is not None else ", pending"),
            entry.due.at,
        )

    # the models: the agent's, from its telemetry, and those that wrote what people said
    for s in spans:
        if s.source is SpanSource.LOG:
            continue
        sim = sim_of_wake[s.wake] if s.wake in sim_of_wake else s.sim_time
        if is_model_call(s):
            lane = b.lane("agent_model", LaneKind.AGENT_MODEL, "Agent's model calls")
            b.add(
                lane,
                MarkKind.MODEL_CALL,
                f"mc:{s.span.span_id}",
                s.span.name,
                sim,
                real=s.span.start,
                real_end=s.span.end,
            )
        else:
            lane = b.lane("spans", LaneKind.SPANS, "Agent's spans")
            b.add(
                lane, MarkKind.SPAN, f"span:{s.span.span_id}", s.span.name, sim, real=s.span.start, real_end=s.span.end
            )
    for i, pc in enumerate(world.person_calls(), start=1):
        lane = b.lane("people_model", LaneKind.PEOPLE_MODEL, "People's model calls")
        whom = by_key[pc.person].name if pc.person in by_key else pc.person
        tokens = (pc.input_tokens or 0) + (pc.output_tokens or 0)
        b.add(
            lane,
            MarkKind.MODEL_CALL,
            f"pc:{i}",
            f"{pc.wrote} for {whom}" + (" (replayed)" if pc.replayed else f", {tokens} tokens"),
            pc.sim_time,
        )

    lanes, marks = b.marks()
    fork = session.fork_account(state, run_id)
    return TimelineResponse(
        starts=_ms(scenario.starts_at),
        reached=_ms(reached_at),
        deadline=_ms(scenario.deadline) if scenario.deadline is not None else None,
        split=_ms(fork.at) if fork is not None else None,
        lanes=lanes,
        marks=marks,
        findings=_finding_marks(result, events, reached_at),
    )


def _finding_marks(result: RunResult | None, events: list[WorldEvent], end: datetime) -> list[FindingMark]:
    if result is None:
        return []
    sim_of = {e.seq: e.sim_time for e in events}
    found: list[FindingMark] = []
    for number, f in enumerate(result.findings, start=1):
        cited = [sim_of[s] for s in f.evidence if s in sim_of]
        at = f.at or (min(cited) if cited else end)
        found.append(FindingMark(number=number, kind=f.kind, sim=_ms(at), refs=[f"ev:{s}" for s in f.evidence]))
    return found


def _wake_label(w: WakeRecord, driven: bool) -> str:
    word = "Step" if driven else "Wake"
    changed = "changed nothing" if w.world_changes == 0 and not w.commitments_changed else f"{w.world_changes} changes"
    return f"{word} {w.index}: {changed}" + (f" ({w.reason})" if w.reason else "")


def _snapshot_words(after: Snapshot) -> str:
    if isinstance(after, MessageSnapshot):
        return after.text
    if isinstance(after, InteractionSnapshot):
        return f"{after.person} {after.interaction} {after.label}"
    if isinstance(after, InboxItemSnapshot):
        return after.summary
    if isinstance(after, MemorySnapshot):
        return after.key
    if isinstance(after, StoredSnapshot):
        return f"{after.collection}/{after.id}"
    words = after.model_dump(exclude={"kind"})
    for field in ("title", "text", "path", "tool", "resource", "document"):
        if field in words and isinstance(words[field], str):
            return words[field]
    return after.kind


# -- the team's rules, and run-all batches ------------------------------------------------------------------------


def assessments(state: Path, run_id: str) -> AssessmentsResponse:
    scenario = session.scenario_of(state, run_id)
    agent = session.agent_of(state, run_id)
    rules = merged(agent.assess if agent is not None else [], scenario.assess, scenario.assess_off)
    result = session.load(state, run_id).result if session.find(state, run_id).finished else None
    tallies = {t.rule: t for t in result.rules_read} if result is not None else {}
    outcomes: list[RuleOutcome] = []
    for rule in rules:
        numbers = (
            [i for i, f in enumerate(result.findings, start=1) if f.check == rule.id] if result is not None else []
        )
        kinds = {result.findings[n - 1].kind for n in numbers} if result is not None else set()
        tally = tallies[rule.id] if rule.id in tallies else None
        if tally is None:
            status = RuleStatus.NOT_CHECKED
        elif FindingKind.FAIL in kinds:
            status = RuleStatus.FAILED
        elif FindingKind.REVIEW in kinds:
            status = RuleStatus.REVIEW
        elif tally.read == 0:
            status = RuleStatus.UNREAD if tally.unread else RuleStatus.NOT_APPLIED
        else:
            status = RuleStatus.PASSED
        outcomes.append(
            RuleOutcome(
                rule=rule,
                status=status,
                read=tally.read if tally is not None else 0,
                unread=tally.unread if tally is not None else 0,
                findings=numbers,
            )
        )
    return AssessmentsResponse(rules=outcomes)


def batches(state: Path) -> BatchesResponse:
    rows: list[BatchRow] = []
    for batch in run_all.batches(state):
        scenarios: list[ScenarioSamples] = []
        for played in batch.played:
            expected = played.expected
            scenarios.append(
                ScenarioSamples(
                    scenario=played.scenario,
                    file=played.file,
                    expected=str(expected)
                    if isinstance(expected, ExpectedOutcome)
                    else f"{expected.outcome} {expected.rate}",
                    matched=played.matched,
                    passed=len(played.samples) - len(played.failing_seeds),
                    samples=[
                        SampleRow(
                            seed=s.seed,
                            verdict=s.verdict,
                            words=s.words,
                            run_id=s.run_id
                            if s.run_id is not None and session.run_dir(state, s.run_id).is_dir()
                            else None,
                        )
                        for s in played.samples
                    ],
                    failing_seeds=played.failing_seeds,
                )
            )
        rows.append(BatchRow(batch_id=batch.batch_id, folder=batch.folder, samples=batch.samples, scenarios=scenarios))
    return BatchesResponse(batches=rows)
