"""`minutehand trace` and `explain`: the agent's acts in order, and the chain around one event, each a few queries
over the read model's views, so what they say is what a reader's own SQL over the same views would find."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from pydantic import Field

from minutehand.adapters.query.build import at
from minutehand.adapters.query.reader import QueryRefused, Record, Value
from minutehand.domain.scenario import Model

KINDS = ("message", "write", "read", "memory", "stored", "next_wake", "call", "model_call")


class TraceFilter(Model):
    person: str | None = Field(default=None, description="people.key: messages to them")
    provider: str | None = None
    kind: str | None = Field(default=None, description=f"One of {', '.join(KINDS)}")
    since: str | None = Field(default=None, description="Simulated time, ISO 8601, inclusive")
    until: str | None = Field(default=None, description="Simulated time, ISO 8601, inclusive")
    wake: int | None = None


class Traced(Model):
    run_id: str
    actions: list[Record] = Field(description="Rows of the `actions` view, in order")


def _moment(text: str, said: str) -> str:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as e:
        raise QueryRefused(f"{said} {text!r} is not an ISO 8601 time (2026-08-24T10:00:00Z)") from e
    return str(at(moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)))


def trace(db: sqlite3.Connection, run_id: str, wanted: TraceFilter) -> Traced:
    """The agent's acts, in order, kept to those the filter names."""
    where: list[str] = []
    args: list[Value] = []
    if wanted.person is not None:
        if _one(db, "SELECT key FROM people WHERE key = ?", [wanted.person]) is None:
            known = [str(r["key"]) for r in _records(db, "SELECT key FROM people ORDER BY key", [])]
            raise QueryRefused(f"no person {wanted.person!r} in this run; its people are {', '.join(known)}")
        where.append("(person = ? OR seq IN (SELECT seq FROM recipients WHERE person = ?))")
        args += [wanted.person, wanted.person]
    if wanted.provider is not None:
        where.append("provider = ?")
        args.append(wanted.provider)
    if wanted.kind is not None:
        if wanted.kind not in KINDS:
            raise QueryRefused(f"kind is one of {', '.join(KINDS)}, not {wanted.kind!r}")
        where.append("kind = ?")
        args.append(wanted.kind)
    if wanted.since is not None:
        where.append("at >= ?")
        args.append(_moment(wanted.since, "--from"))
    if wanted.until is not None:
        where.append("at <= ?")
        args.append(_moment(wanted.until, "--to"))
    if wanted.wake is not None:
        where.append("wake = ?")
        args.append(wanted.wake)
    sql = "SELECT * FROM actions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY position"
    return Traced(run_id=run_id, actions=_records(db, sql, args))


def _records(db: sqlite3.Connection, sql: str, args: list[Value]) -> list[Record]:
    """Rows of a query the read model's own code writes, with its parameters bound."""
    cursor = db.execute(sql, args)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _one(db: sqlite3.Connection, sql: str, args: list[Value]) -> Record | None:
    found = _records(db, sql, args)
    return found[0] if found else None


def described(traced: Traced) -> str:
    if not traced.actions:
        return f"run {traced.run_id}: no action of the agent's matches\n"
    lines = [f"run {traced.run_id}: {len(traced.actions)} action(s) of the agent's"]
    for a in traced.actions:
        when = str(a["at"])[:16].replace("T", " ")
        to = f" -> {a['person']}" if a["person"] is not None else ""
        ref = (
            f"seq {a['seq']}"
            if a["seq"] is not None
            else f"call {a['call_id']}"
            if a["call_id"] is not None
            else f"span {a['span_id']}"
        )
        lines.append(
            f"#{a['position']:<4} wake {a['wake']:<3} {when}  {a['kind']:<10} {a['provider'] or ''} {a['target'] or ''}"
            f"{to}: {a['summary'] or ''}  ({ref})"
        )
    return "\n".join(lines) + "\n"


class Explained(Model):
    """One event and the chain around it, each part a row of a view (`docs/querying.md`)."""

    run_id: str
    seq: int
    event: Record = Field(description="The event (events)")
    action: Record | None = Field(
        description="It as one of the agent's acts (actions); None when it is not the agent's"
    )
    message: Record | None = Field(description="It as a message (messages), when it is one")
    call: Record | None = Field(description="The HTTP call that made it (calls)")
    wake: Record | None = Field(description="The wake it happened in (wakes)")
    woken_by: list[Record] = Field(description="What fell due and fired as that wake began (dispatch)")
    replies_landed: list[Record] = Field(description="People's replies that landed as that wake began (replies)")
    read_before: list[Record] = Field(
        description="What the agent read in that wake before it: reads, memory gets and listings, GET calls (actions)"
    )
    model_call: Record | None = Field(description="The agent's model call that wrote it (model_calls)")
    answers: Record | None = Field(
        description="The earlier message it answers: for a person's reply, the agent's message it answers; for the "
        "agent's follow-up, the ask it chases; for another agent message, the last person's message before it in the "
        "same conversation (messages)"
    )
    replies: list[Record] = Field(description="Replies people gave to it (replies)")
    follow_ups: list[Record] = Field(description="The agent's follow-ups on it, when it opened a wait (messages)")
    findings: list[Record] = Field(description="Findings citing it (findings)")
    after: list[Record] = Field(description="The agent's next acts in the same wake (actions), up to five")


def explain(db: sqlite3.Connection, run_id: str, seq: int) -> Explained:
    event = _one(db, "SELECT * FROM events WHERE seq = ?", [seq])
    if event is None:
        head = _one(db, "SELECT MAX(seq) AS head FROM events", [])
        raise QueryRefused(f"run {run_id} has no event {seq}; its seqs run from 1 to {head['head'] if head else 0}")
    action = _one(db, "SELECT * FROM actions WHERE seq = ?", [seq])
    message = _one(db, "SELECT * FROM messages WHERE seq = ?", [seq])
    wake = _one(db, "SELECT * FROM wakes WHERE wake = ?", [event["wake"]])
    woken = (
        _records(db, "SELECT * FROM dispatch WHERE closed = 'fired' AND closed_at = ? ORDER BY due_id", [wake["at"]])
        if wake is not None
        else []
    )
    landed = (
        _records(db, "SELECT * FROM replies WHERE landed = 1 AND at = ? ORDER BY reply_id", [wake["at"]])
        if wake is not None
        else []
    )
    # Where the event sits among the agent's acts: its own place, or, for an event not the agent's, before the
    # first act written after it.
    cut = _one(
        db,
        "SELECT COALESCE((SELECT position FROM actions WHERE seq = ?), (SELECT MIN(position) FROM actions WHERE seq > ?),"
        " (SELECT COALESCE(MAX(position), 0) + 1 FROM actions)) AS position",
        [seq, seq],
    )
    position = cut["position"] if cut is not None else 0
    read_before = _records(
        db,
        "SELECT * FROM actions WHERE wake = ? AND position < ?"
        " AND (kind = 'read' OR (kind = 'memory' AND operation IN ('read', 'search'))"
        " OR (kind = 'call' AND operation = 'GET')) ORDER BY position",
        [event["wake"], position],
    )
    after = _records(
        db,
        "SELECT * FROM actions WHERE wake = ? AND position >= ? AND (seq IS NULL OR seq != ?) ORDER BY position LIMIT 5",
        [event["wake"], position, seq],
    )
    model_call = (
        _one(db, "SELECT * FROM model_calls WHERE span_id = ?", [message["model_call_span_id"]])
        if message is not None and message["model_call_span_id"] is not None
        else None
    )
    answers: Record | None = None
    if message is not None:
        if message["answers_seq"] is not None:
            answers = _one(db, "SELECT * FROM messages WHERE seq = ?", [message["answers_seq"]])
        elif message["is_follow_up"] == 1 and message["ask_seq"] is not None:
            answers = _one(db, "SELECT * FROM messages WHERE seq = ?", [message["ask_seq"]])
        elif message["from_actor"] == "agent":
            answers = _one(
                db,
                "SELECT * FROM messages WHERE from_actor = 'person' AND provider = ? AND channel = ? AND seq < ?"
                " ORDER BY seq DESC LIMIT 1",
                [message["provider"], message["channel"], seq],
            )
    return Explained(
        run_id=run_id,
        seq=seq,
        event=event,
        action=action,
        message=message,
        call=_one(db, "SELECT * FROM calls WHERE call_id = ?", [event["call_id"]])
        if event["call_id"] is not None
        else None,
        wake=wake,
        woken_by=woken,
        replies_landed=landed,
        read_before=read_before,
        model_call=model_call,
        answers=answers,
        replies=_records(db, "SELECT * FROM replies WHERE answers_seq = ? ORDER BY reply_id", [seq]),
        follow_ups=_records(db, "SELECT * FROM messages WHERE ask_seq = ? AND is_follow_up = 1 ORDER BY seq", [seq]),
        findings=_records(
            db,
            "SELECT f.* FROM findings f JOIN evidence e ON e.finding_id = f.finding_id WHERE e.seq = ?"
            " ORDER BY f.finding_id",
            [seq],
        ),
        after=after,
    )


def _line(prefix: str, text: str) -> str:
    return f"  {prefix}: {text}"


def _said(m: Record) -> str:
    who = m["from_person"] or m["from_actor"]
    return f"seq {m['seq']} {m['at']} {who} in {m['provider']} {m['channel']}: {m['text']!s:.160}"


def explanation(found: Explained) -> str:
    e = found.event
    lines = [
        f"run {found.run_id}, seq {found.seq}: {e['actor']} {e['operation']} {e['provider']} {e['entity_kind']} "
        f"{e['entity_id']} at {e['at']} in wake {e['wake']}"
    ]
    if found.message is not None:
        lines.append(_line("said", str(found.message["text"])))
    lines.append("before")
    w = found.wake
    if w is not None:
        lines.append(
            _line(
                "wake",
                f"{w['wake']} at {w['at']}, reason {w['reason'] or 'not recorded'}"
                + (f", woken by {w['woken_by']}" if w["woken_by"] else ""),
            )
        )
    else:
        lines.append(_line("wake", f"{e['wake']} (setup)" if e["wake"] == 0 else f"{e['wake']}, no record of it"))
    for d in found.woken_by:
        lines.append(_line("fell due", f"{d['source']} {d['kind']} due {d['due_at']} (dispatch {d['due_id']})"))
    for r in found.replies_landed:
        lines.append(
            _line("reply landed", f"{r['person']}: {r['text']!s:.120} (reply {r['reply_id']}, seq {r['seq']})")
        )
    for a in found.read_before:
        lines.append(_line("read first", f"#{a['position']} {a['kind']} {a['target']}: {a['summary']}"))
    if found.answers is not None:
        lines.append(_line("answers", _said(found.answers)))
    if found.model_call is not None:
        m = found.model_call
        joined = found.message["joined_by"] if found.message is not None else None
        lines.append(
            _line(
                "written by",
                f"model call {m['span_id']} ({m['model']}, {m['input_tokens']} in / {m['output_tokens']} out, "
                f"joined by {joined})",
            )
        )
    if found.call is not None:
        c = found.call
        lines.append(
            _line(
                "call",
                f"{c['method']} {c['host']}{c['path']} -> {c['status']} ({c['answered_by']}, call {c['call_id']})",
            )
        )
    lines.append("after")
    for r in found.replies:
        lines.append(
            _line("reply", f"{r['person']} at {r['at']}: {r['text']!s:.120} ({r['written_by']}, reply {r['reply_id']})")
        )
    for m in found.follow_ups:
        lines.append(_line("follow-up", _said(m)))
    for f in found.findings:
        lines.append(_line("finding", f"{f['kind']} {f['check_id']}: {f['message']} (finding {f['finding_id']})"))
    for a in found.after:
        lines.append(_line("next", f"#{a['position']} {a['kind']} {a['target']}: {a['summary']}"))
    if not (found.replies or found.follow_ups or found.findings or found.after):
        lines.append("  nothing followed in this run's record")
    return "\n".join(lines) + "\n"
