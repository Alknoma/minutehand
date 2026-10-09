"""Each view of the read model over a run written by hand (`fixture.py`), value for value, and over its fork."""

from __future__ import annotations

import json
import sqlite3

from minutehand.adapters.query.reader import Record, Value, open_model, query
from minutehand.adapters.query.schema import VERSION
from minutehand.session import RUNS, WORLD
from tests.query.fixture import (
    ANSWER,
    FOLLOW_UP,
    FORK_FOLLOW_UP,
    HISTORY,
    PRICES,
    QUESTION,
    TO_DANIA,
    TOLD,
    Fixture,
)


def rows(run: Fixture, sql: str, *, fork: bool = False) -> list[Record]:
    db = open_model(run.state, run.fork_id if fork else run.run_id, PRICES)
    try:
        return query(db, sql).records()
    finally:
        db.close()


def column(run: Fixture, sql: str, *, fork: bool = False) -> list[Value]:
    return [next(iter(r.values())) for r in rows(run, sql, fork=fork)]


def test_the_run_row_names_the_run_its_verdict_and_the_read_models_version(run: Fixture) -> None:
    [row] = rows(run, "SELECT * FROM run")
    assert row == {
        "run_id": run.run_id,
        "root_run": run.run_id,
        "parent_run": None,
        "forked_at_seq": None,
        "scenario": "partner_pricing",
        "goal": "The partner pricing is confirmed with Sofia.",
        "seed": 7,
        "starts_at": "2026-08-24T10:00:00.000Z",
        "ended_at": "2026-08-25T16:00:00.000Z",
        "stop": "agent_done",
        "verdict": "failed",
        "verdict_words": "Failed: 1 check failed; the agent reported it was done.",
        "finished": 1,
        "read_model_version": VERSION,
    }


def test_people_carry_their_working_hours(run: Fixture) -> None:
    assert rows(run, "SELECT key, is_owner, reply, timezone, opens, closes, weekdays_only FROM people") == [
        {
            "key": "dania",
            "is_owner": 0,
            "reply": "silent",
            "timezone": "America/New_York",
            "opens": "09:00:00",
            "closes": "17:00:00",
            "weekdays_only": 1,
        },
        {
            "key": "owner",
            "is_owner": 1,
            "reply": "scripted",
            "timezone": None,
            "opens": None,
            "closes": None,
            "weekdays_only": None,
        },
        {
            "key": "sofia",
            "is_owner": 0,
            "reply": "scripted",
            "timezone": "Europe/Rome",
            "opens": "09:00:00",
            "closes": "18:00:00",
            "weekdays_only": 1,
        },
    ]


def test_a_body_kept_compressed_in_the_world_file_reads_back_decoded(run: Fixture) -> None:
    world = sqlite3.connect(run.state / RUNS / run.run_id / WORLD)
    try:
        inline, codec = world.execute(
            "SELECT x.response_body, c.codec FROM exchange x JOIN content c ON c.hash = x.response_ref"
            " WHERE x.exchange LIKE '%conversations.history%'"
        ).fetchone()
    finally:
        world.close()
    assert (inline, codec) == (None, "zstd")  # the world file holds a hash and compressed bytes, not the text
    [call] = rows(run, "SELECT * FROM calls WHERE path LIKE '/api/conversations.history%'")
    assert call["response_body"] == HISTORY
    assert call["response_size"] == len(HISTORY.encode())
    assert (call["first_seq"], call["last_seq"], call["answered_by"], call["request_body"]) == (
        run.history_read,
        run.history_read,
        "provider",
        None,
    )
    assert json.loads(str(call["response_body"]))["messages"][39]["text"] == "earlier message number 39"


def test_calls_say_who_answered_each(run: Fixture) -> None:
    assert rows(
        run, "SELECT call_id, method, host, status, answered_by, capture_mode, first_seq, duration_ms FROM calls"
    ) == [
        {
            "call_id": 1,
            "method": "GET",
            "host": "slack.com",
            "status": 200,
            "answered_by": "provider",
            "capture_mode": None,
            "first_seq": run.history_read,
            "duration_ms": None,
        },
        {
            "call_id": 2,
            "method": "POST",
            "host": "slack.com",
            "status": 200,
            "answered_by": "provider",
            "capture_mode": None,
            "first_seq": run.asked,
            "duration_ms": None,
        },
        {
            "call_id": 3,
            "method": "POST",
            "host": "slack.com",
            "status": 200,
            "answered_by": "provider",
            "capture_mode": None,
            "first_seq": run.to_dania,
            "duration_ms": None,
        },
        {
            "call_id": 4,
            "method": "POST",
            "host": "hooks.example.com",
            "status": 202,
            "answered_by": "declaration",
            "capture_mode": "acknowledge",
            "first_seq": None,
            "duration_ms": 12.0,
        },
        {
            "call_id": 5,
            "method": "GET",
            "host": "api.unknown.example",
            "status": 502,
            "answered_by": "refused",
            "capture_mode": None,
            "first_seq": None,
            "duration_ms": None,
        },
        {
            "call_id": 6,
            "method": "POST",
            "host": "slack.com",
            "status": 200,
            "answered_by": "provider",
            "capture_mode": None,
            "first_seq": run.follow_up,
            "duration_ms": None,
        },
        {
            "call_id": 7,
            "method": "POST",
            "host": "slack.com",
            "status": 200,
            "answered_by": "provider",
            "capture_mode": None,
            "first_seq": run.told,
            "duration_ms": None,
        },
    ]
    [sent] = rows(run, "SELECT request_body, trace_id, caller_span_id FROM calls WHERE call_id = 2")
    assert json.loads(str(sent["request_body"])) == {"channel": "D0SOFIA", "text": QUESTION}
    assert (sent["trace_id"], sent["caller_span_id"]) == ("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7")


def test_messages_say_what_the_ledger_knows_of_each(run: Fixture) -> None:
    assert rows(
        run,
        "SELECT seq, from_actor, from_person, to_people, text, is_ask, is_follow_up, ask_seq, is_reply, answers_seq,"
        " joined_by, call_id FROM messages",
    ) == [
        {
            "seq": run.asked,
            "from_actor": "agent",
            "from_person": None,
            "to_people": '["sofia"]',
            "text": QUESTION,
            "is_ask": 1,
            "is_follow_up": 0,
            "ask_seq": run.asked,
            "is_reply": 0,
            "answers_seq": None,
            "joined_by": "trace",
            "call_id": 2,
        },
        {
            "seq": run.to_dania,
            "from_actor": "agent",
            "from_person": None,
            "to_people": '["dania"]',
            "text": TO_DANIA,
            "is_ask": 1,
            "is_follow_up": 0,
            "ask_seq": run.to_dania,
            "is_reply": 0,
            "answers_seq": None,
            "joined_by": "wake",
            "call_id": 3,
        },
        {
            "seq": run.follow_up,
            "from_actor": "agent",
            "from_person": None,
            "to_people": '["sofia"]',
            "text": FOLLOW_UP,
            "is_ask": 0,
            "is_follow_up": 1,
            "ask_seq": run.asked,
            "is_reply": 0,
            "answers_seq": None,
            "joined_by": "content",
            "call_id": 6,
        },
        {
            "seq": run.answered,
            "from_actor": "person",
            "from_person": "sofia",
            "to_people": "[]",
            "text": ANSWER,
            "is_ask": 0,
            "is_follow_up": 0,
            "ask_seq": None,
            "is_reply": 1,
            "answers_seq": run.asked,
            "joined_by": None,
            "call_id": None,
        },
        {
            "seq": run.told,
            "from_actor": "agent",
            "from_person": None,
            "to_people": '["owner"]',
            "text": TOLD,
            "is_ask": 0,
            "is_follow_up": 0,
            "ask_seq": None,
            "is_reply": 0,
            "answers_seq": None,
            "joined_by": None,
            "call_id": 7,
        },
    ]
    [follow_up] = rows(
        run, f"SELECT channel, thread_of, change, model_call_span_id FROM messages WHERE seq = {run.follow_up}"
    )
    assert follow_up == {
        "channel": "D0SOFIA",
        "thread_of": "D0SOFIA/1724493600.000100",
        "change": "sent",
        "model_call_span_id": "b2b2b2b2b2b2b2b2",
    }


def test_recipients_are_placed_in_their_own_working_day(run: Fixture) -> None:
    assert rows(run, "SELECT * FROM recipients") == [
        {
            "seq": run.asked,
            "person": "sofia",
            "email": "sofia@example.com",
            "local_time": "2026-08-24T12:00:00+02:00",
            "in_working_hours": 1,
            "away": 0,
        },
        {
            "seq": run.to_dania,
            "person": "dania",
            "email": "dania@example.com",
            "local_time": "2026-08-24T06:00:00-04:00",
            "in_working_hours": 0,
            "away": 0,
        },
        {
            "seq": run.follow_up,
            "person": "sofia",
            "email": "sofia@example.com",
            "local_time": "2026-08-25T14:00:00+02:00",
            "in_working_hours": 1,
            "away": 0,
        },
        {
            "seq": run.told,
            "person": "owner",
            "email": "owner@example.com",
            "local_time": None,
            "in_working_hours": None,
            "away": 0,
        },
    ]


def test_memory_shows_each_key_over_time_and_what_each_get_found(run: Fixture) -> None:
    assert rows(run, "SELECT wake, actor, op, key, value FROM memory") == [
        {"wake": 0, "actor": "scenario", "op": "put", "key": "team", "value": '{"name":"core"}'},
        {"wake": 1, "actor": "agent", "op": "get", "key": "asks/sofia", "value": None},
        {"wake": 1, "actor": "agent", "op": "put", "key": "asks/sofia", "value": '{"status":"asked"}'},
        {"wake": 2, "actor": "agent", "op": "get", "key": "asks/sofia", "value": '{"status":"asked"}'},
        {"wake": 2, "actor": "agent", "op": "put", "key": "asks/sofia", "value": '{"status":"chased"}'},
        {"wake": 3, "actor": "agent", "op": "get", "key": "asks/sofia", "value": '{"status":"chased"}'},
        {"wake": 3, "actor": "agent", "op": "put", "key": "asks/sofia", "value": '{"status":"answered"}'},
        {"wake": 3, "actor": "agent", "op": "put", "key": "notes/last", "value": '"told the owner"'},
    ]


def test_stored_items_read_as_stored(run: Fixture) -> None:
    assert rows(run, "SELECT op, host, collection, path, item_id, item FROM stored") == [
        {
            "op": "create",
            "host": "crm.example.com",
            "collection": "contacts",
            "path": "/v1/contacts",
            "item_id": "7",
            "item": '{"id":7,"name":"Sofia Romano"}',
        }
    ]


def test_wakes_say_why_each_began_and_what_the_agent_reported(run: Fixture) -> None:
    assert rows(
        run, "SELECT wake, at, reason, woken_by, reported_status, reported_next_wake, actions, model_calls FROM wakes"
    ) == [
        {
            "wake": 1,
            "at": "2026-08-24T10:00:00.000Z",
            "reason": "start",
            "woken_by": None,
            "reported_status": "working",
            "reported_next_wake": "2026-08-25T12:00:00.000Z",
            "actions": 10,
            "model_calls": 1,
        },
        {
            "wake": 2,
            "at": "2026-08-25T12:00:00.000Z",
            "reason": "due",
            "woken_by": "reported",
            "reported_status": "working",
            "reported_next_wake": None,
            "actions": 4,
            "model_calls": 1,
        },
        {
            "wake": 3,
            "at": "2026-08-25T16:00:00.000Z",
            "reason": "person_replied",
            "woken_by": "reply",
            "reported_status": "done",
            "reported_next_wake": None,
            "actions": 4,
            "model_calls": 0,
        },
    ]


def test_dispatch_holds_each_entry_as_it_last_stood(run: Fixture) -> None:
    assert rows(
        run,
        "SELECT due_id, kind, source, due_at, entered_wake, closed, closed_wake, drawn_from, drawn_offset_seconds FROM dispatch",
    ) == [
        {
            "due_id": 1,
            "kind": "agent_wake",
            "source": "reported",
            "due_at": "2026-08-25T12:00:00.000Z",
            "entered_wake": 1,
            "closed": "fired",
            "closed_wake": 1,
            "drawn_from": None,
            "drawn_offset_seconds": None,
        },
        {
            "due_id": 2,
            "kind": "person_reply",
            "source": "reply",
            "due_at": "2026-08-25T16:00:00.000Z",
            "entered_wake": 1,
            "closed": "fired",
            "closed_wake": 2,
            "drawn_from": "delay",
            "drawn_offset_seconds": 108000.0,
        },
    ]


def test_replies_say_how_their_words_were_written_and_what_they_answer(run: Fixture) -> None:
    assert rows(run, "SELECT * FROM replies") == [
        {
            "reply_id": 1,
            "person": "sofia",
            "at": "2026-08-25T16:00:00.000Z",
            "kind": "message",
            "writing": "script",
            "written_by": "model",
            "model": "people-model",
            "prompt_version": "person-step/1",
            "text": ANSWER,
            "facts": '["40k a year"]',
            "decision": None,
            "answers_seq": run.asked,
            "in_reply_to": "slack/message/D0SOFIA/1724493600.000100",
            "seq": run.answered,
            "drawn_from": "delay",
            "person_call_id": 1,
        }
    ]


def test_model_calls_count_tokens_and_cost_only_what_was_priced(run: Fixture) -> None:
    assert rows(
        run,
        "SELECT side, span_id, person_call_id, person, wake, model, prompt_version, input_tokens, output_tokens, cost,"
        " currency, wrote, wrote_seqs, replayed FROM model_calls ORDER BY side, at",
    ) == [
        {
            "side": "agent",
            "span_id": "a1a1a1a1a1a1a1a1",
            "person_call_id": None,
            "person": None,
            "wake": 1,
            "model": "gpt-test",
            "prompt_version": None,
            "input_tokens": 1200,
            "output_tokens": 80,
            "cost": 0.00304,
            "currency": "USD",
            "wrote": None,
            "wrote_seqs": f"[{run.asked}, {run.to_dania}]",
            "replayed": None,
        },
        {
            "side": "agent",
            "span_id": "b2b2b2b2b2b2b2b2",
            "person_call_id": None,
            "person": None,
            "wake": 2,
            "model": "gpt-test",
            "prompt_version": None,
            "input_tokens": 900,
            "output_tokens": 60,
            "cost": 0.00228,
            "currency": "USD",
            "wrote": None,
            "wrote_seqs": f"[{run.follow_up}]",
            "replayed": None,
        },
        {
            "side": "person",
            "span_id": None,
            "person_call_id": 1,
            "person": "sofia",
            "wake": 1,
            "model": "people-model",
            "prompt_version": "person-step/1",
            "input_tokens": 300,
            "output_tokens": 12,
            "cost": None,
            "currency": None,
            "wrote": "reply",
            "wrote_seqs": f"[{run.asked}]",
            "replayed": 0,
        },
    ]


def test_model_calls_give_cached_input_tokens_apart_from_the_ones_billed_at_the_base_rate(run: Fixture) -> None:
    assert rows(
        run,
        "SELECT side, input_tokens, cache_read_tokens, cache_creation_tokens, uncached_input_tokens FROM model_calls "
        "ORDER BY side, at",
    ) == [
        {
            "side": "agent",
            "input_tokens": 1200,
            "cache_read_tokens": None,
            "cache_creation_tokens": None,
            "uncached_input_tokens": 1200,
        },
        {
            "side": "agent",
            "input_tokens": 900,
            "cache_read_tokens": None,
            "cache_creation_tokens": None,
            "uncached_input_tokens": 900,
        },
        {
            "side": "person",
            "input_tokens": 300,
            "cache_read_tokens": 100,
            "cache_creation_tokens": None,
            "uncached_input_tokens": 200,
        },
    ]


def test_without_declared_prices_no_call_has_a_cost(run: Fixture) -> None:
    db = open_model(run.state, run.run_id)
    try:
        assert query(db, "SELECT DISTINCT cost, currency FROM model_calls").rows == [[None, None]]
    finally:
        db.close()


def test_findings_and_their_evidence(run: Fixture) -> None:
    assert rows(run, "SELECT finding_id, check_id, kind, pattern, evidence FROM findings") == [
        {
            "finding_id": 1,
            "check_id": "follows_up_when_due",
            "kind": "fail",
            "pattern": "expiry_on_every_wait",
            "evidence": f"[{run.asked}]",
        },
        {
            "finding_id": 2,
            "check_id": "expectations",
            "kind": "informational",
            "pattern": None,
            "evidence": f"[{run.asked}, {run.follow_up}]",
        },
        {
            "finding_id": 3,
            "check_id": "expectations",
            "kind": "informational",
            "pattern": None,
            "evidence": f"[{run.told}]",
        },
    ]
    assert column(run, "SELECT seq FROM evidence WHERE finding_id = 2") == [run.asked, run.follow_up]


def test_spans_are_joined_to_the_call_they_made(run: Fixture) -> None:
    assert rows(run, "SELECT span_id, source, wake, is_model_call, call_id FROM spans ORDER BY span_id") == [
        {"span_id": "00f067aa0ba902b7", "source": "received", "wake": 1, "is_model_call": 0, "call_id": 2},
        {"span_id": "a1a1a1a1a1a1a1a1", "source": "wire", "wake": 1, "is_model_call": 1, "call_id": None},
        {"span_id": "b2b2b2b2b2b2b2b2", "source": "wire", "wake": 2, "is_model_call": 1, "call_id": None},
    ]
    [attributes] = column(run, "SELECT attributes FROM spans WHERE span_id = 'a1a1a1a1a1a1a1a1'")
    held = json.loads(str(attributes))
    assert (held["gen_ai.usage.input_tokens"], json.loads(held["gen_ai.output.messages"])[0]["content"]) == (
        1200,
        QUESTION,
    )


def test_actions_are_the_agents_acts_in_the_order_it_made_them(run: Fixture) -> None:
    acted = [
        (r["kind"], r["seq"] or r["call_id"] or r["span_id"], r["person"])
        for r in rows(run, "SELECT * FROM actions ORDER BY position")
    ]
    assert acted == [
        ("memory", 3, None),
        ("read", run.history_read, None),
        ("model_call", "a1a1a1a1a1a1a1a1", None),
        ("message", run.asked, "sofia"),
        ("message", run.to_dania, "dania"),
        ("memory", 7, None),
        ("stored", 8, None),
        ("call", 4, None),
        ("call", 5, None),
        ("next_wake", 9, None),
        ("memory", 14, None),
        ("model_call", "b2b2b2b2b2b2b2b2", None),
        ("message", run.follow_up, "sofia"),
        ("memory", 16, None),
        ("memory", 20, None),
        ("message", run.told, "owner"),
        ("memory", 22, None),
        ("memory", run.late_write, None),
    ]
    assert column(run, "SELECT position FROM actions") == list(range(1, 19))
    [hook] = rows(run, "SELECT target, summary, view FROM actions WHERE call_id = 4")
    assert hook == {
        "target": "hooks.example.com/notify",
        "summary": "POST hooks.example.com/notify -> 202",
        "view": "calls",
    }


def test_events_hold_the_whole_log_with_snapshots_as_json(run: Fixture) -> None:
    assert column(run, "SELECT COUNT(*) FROM events") == [run.late_write]
    [snapshot] = column(run, f"SELECT snapshot FROM events WHERE seq = {run.asked}")
    assert json.loads(str(snapshot))["text"] == QUESTION
    assert column(run, f"SELECT call_id FROM events WHERE seq = {run.asked}") == [2]


def test_a_fork_sees_its_parents_record_up_to_its_checkpoint_and_its_own_after(run: Fixture) -> None:
    [row] = rows(run, "SELECT run_id, root_run, parent_run, forked_at_seq FROM run", fork=True)
    assert row == {
        "run_id": run.fork_id,
        "root_run": run.run_id,
        "parent_run": run.run_id,
        "forked_at_seq": run.fork_at,
    }
    said = rows(run, "SELECT seq, run_id, text, is_follow_up FROM messages", fork=True)
    assert said == [
        {"seq": run.asked, "run_id": run.run_id, "text": QUESTION, "is_follow_up": 0},
        {"seq": run.to_dania, "run_id": run.run_id, "text": TO_DANIA, "is_follow_up": 0},
        {"seq": run.fork_at + 2, "run_id": run.fork_id, "text": FORK_FOLLOW_UP, "is_follow_up": 1},
    ]
    assert column(run, "SELECT key FROM memory WHERE wake > 1", fork=True) == []
    assert column(run, "SELECT span_id FROM model_calls WHERE side = 'agent'", fork=True) == ["a1a1a1a1a1a1a1a1"]
    assert column(run, "SELECT reply_id FROM replies", fork=True) == [1]
    assert column(run, "SELECT COUNT(*) FROM calls", fork=True) == [6]
    assert column(run, "SELECT reason FROM wakes", fork=True) == ["start", "due"]
