"""The read model of a run: every view, every column, what each holds. This is the contract `minutehand query`,
`trace`, `explain` and the MCP tools `query_run` and `schema` answer to, and `docs/querying.md` documents.

A view's name, its columns, their order and their types are fixed for one `VERSION`. Removing or renaming a column,
changing its type or what it holds, or removing a view, is a new `VERSION`; adding a view or a column at the end of
one is not. `tests/query/schema_golden.json` holds the views as they stand and fails when they change without the
version changing too.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from minutehand.domain.scenario import Model

VERSION = 2
"""The read model's version, in `minutehand_schema.version` and `run.read_model_version`."""


class SqlType(StrEnum):
    TEXT = "TEXT"
    INTEGER = "INTEGER"
    REAL = "REAL"


class Column(Model):
    name: str
    type: SqlType
    description: str


class View(Model):
    name: str
    description: str
    columns: list[Column]
    order: str = Field(description="The order its rows are kept in; a query that relies on an order says ORDER BY")


TIME = (
    "UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it"
)


def _c(name: str, kind: SqlType, description: str) -> Column:
    return Column(name=name, type=kind, description=description)


T, N, R = SqlType.TEXT, SqlType.INTEGER, SqlType.REAL

VIEWS: tuple[View, ...] = (
    View(
        name="minutehand_schema",
        description="Every view of the read model and its columns, with the read model's version: this list.",
        order="view_name, position",
        columns=[
            _c("version", N, "The read model's version; a column removed, renamed or changed is a new version"),
            _c("view_name", T, "The view's name"),
            _c("position", N, "The column's place in the view, from 1"),
            _c("column_name", T, "The column's name"),
            _c("type", T, "TEXT, INTEGER or REAL"),
            _c("description", T, "What the column holds"),
        ],
    ),
    View(
        name="run",
        description="The run being read, one row: a fork reads its parent's log up to the checkpoint it was taken at "
        "and its own after it, and every other view holds exactly what the fork can see.",
        order="run_id",
        columns=[
            _c("run_id", T, "The run read"),
            _c("root_run", T, "The run whose world file holds it: itself, or the run its line of forks began with"),
            _c("parent_run", T, "The run it was forked from; NULL for a run played from the beginning"),
            _c("forked_at_seq", N, "The last seq it shares with its parent; NULL when not a fork"),
            _c("scenario", T, "The scenario's name"),
            _c("goal", T, "The goal handed to the agent"),
            _c("seed", N, "The run's seed; NULL while it runs"),
            _c("starts_at", T, f"When the scenario starts, simulated; {TIME}"),
            _c("ended_at", T, "When the run ended, simulated; NULL while it runs"),
            _c("stop", T, "How it stopped (agent_done, wake_limit, deadline_passed, ...); NULL while it runs"),
            _c(
                "verdict",
                T,
                "passed, failed, unfinished, not_judged, tool_failed, environment_failed or simulation_incomplete; "
                "NULL while it runs",
            ),
            _c("verdict_words", T, "The verdict in one sentence, as every surface states it"),
            _c("finished", N, "1 once the run has finished and been judged, else 0"),
            _c("read_model_version", N, "The read model's version"),
        ],
    ),
    View(
        name="people",
        description="The scenario's people: who the agent can reach, and the hours they work.",
        order="key",
        columns=[
            _c("key", T, "Person.key, as every other view names a person"),
            _c("name", T, "Their name"),
            _c("email", T, "Their email, which a message's recipients are matched by"),
            _c("is_owner", N, "1 for the scenario's owner"),
            _c("reply", T, "How they answer: scripted, answers (a model writes from their facts) or silent"),
            _c("timezone", T, "Their working hours' timezone; NULL when they declare no hours"),
            _c("opens", T, "When their working day opens, local HH:MM:SS; NULL when they declare no hours"),
            _c("closes", T, "When it closes, local HH:MM:SS; NULL when they declare no hours"),
            _c("weekdays_only", N, "1 when they work Monday to Friday only; NULL when they declare no hours"),
        ],
    ),
    View(
        name="events",
        description="The world's log, every row of it in order: each change and read by the agent, a person or the "
        "scenario, the run loop's own checkpoints and table of what is due included.",
        order="seq",
        columns=[
            _c("seq", N, "The event's place in the log; every other view's seq is one of these"),
            _c("run_id", T, "The run that wrote it: for a fork, its parent's id on the rows it shares"),
            _c("wake", N, "The wake it happened in; 0 is setup"),
            _c("at", T, f"Simulated time; {TIME}"),
            _c("wall_time", T, "Real time it was written"),
            _c("actor", T, "agent, person, scenario, or a declared service's system or timer"),
            _c("operation", T, "create, update, delete, read or search"),
            _c("provider", T, "The provider (slack, asana, ...), memory, minutehand (the run loop) or a declared host"),
            _c("entity_kind", T, "message, ticket, document, memory, stored, due, record, ..."),
            _c("entity_id", T, "The entity's id within its provider and kind"),
            _c("snapshot", T, "What the entity read after the change, as JSON; NULL when none was kept"),
            _c("call_id", N, "The HTTP call that wrote it (calls.call_id); NULL when no call did"),
        ],
    ),
    View(
        name="actions",
        description="Every act of the agent under test, in the order it acted: messages, other writes, reads, its "
        "memory, items it stored, the next wake it marked, calls that wrote nothing, and its model calls. Each row "
        "names the view holding the whole of it.",
        order="position",
        columns=[
            _c("position", N, "Its place in the agent's acts, from 1"),
            _c("seq", N, "The event it is (events.seq); NULL for a call that wrote nothing or a model call"),
            _c("call_id", N, "The HTTP call it was made by, or is (calls.call_id); NULL when none"),
            _c("span_id", T, "The model call it is (model_calls.span_id); NULL otherwise"),
            _c("at", T, f"Simulated time; {TIME}"),
            _c("wake", N, "The wake it happened in"),
            _c(
                "kind",
                T,
                "message, write, read, memory, stored, next_wake, call (an HTTP call that wrote and read no "
                "event) or model_call",
            ),
            _c(
                "operation",
                T,
                "create, update, delete, read or search; for a call its HTTP method; for a model call chat",
            ),
            _c("provider", T, "The provider, memory, a declared host, or the model's system for a model call"),
            _c("target", T, "What it acted on: a channel, an entity id, a memory key, host and path, or the model"),
            _c("person", T, "people.key of the person a message went to (the first, when several); else NULL"),
            _c("summary", T, "One line: what it said, wrote, read or asked, cut at 200 characters"),
            _c(
                "view", T, "The view that holds the whole of it: messages, memory, stored, events, calls or model_calls"
            ),
        ],
    ),
    View(
        name="messages",
        description="Every message in the world, the agent's and people's, sent, rewritten or deleted, with what the "
        "ledger of waits knows of it: whether it asked, followed up or answered.",
        order="seq",
        columns=[
            _c("seq", N, "The event (events.seq)"),
            _c("run_id", T, "The run that wrote it"),
            _c("at", T, f"Simulated time; {TIME}"),
            _c("wake", N, "The wake it happened in"),
            _c("change", T, "sent, edited or deleted"),
            _c("provider", T, "slack, microsoft, google_workspace, ... or a declared host for a captured send"),
            _c("channel", T, "The conversation: a channel, a chat, a mailbox thread, as the provider names it"),
            _c("thread_of", T, "The message it is a reply under, when it is in a thread"),
            _c("message_id", T, "The message's own id"),
            _c("from_actor", T, "agent, person or scenario"),
            _c("from_person", T, "people.key of a person's message, when a reply of theirs landed as it; else NULL"),
            _c("to_people", T, "JSON array of the people.key it was addressed to"),
            _c("to_emails", T, "JSON array of the addresses it was addressed to, as the provider recorded them"),
            _c("text", T, "What it said, decoded"),
            _c("text_before", T, "For an edit: what it said before"),
            _c("is_ask", N, "1 when it opened a wait on a person (the ledger's ask); agent messages only"),
            _c("is_follow_up", N, "1 when it was a follow-up on a wait still open; agent messages only"),
            _c("ask_seq", N, "The seq of the ask it opened or followed up; NULL when neither"),
            _c("is_reply", N, "1 when it is a person's reply, as decided and landed"),
            _c("answers_seq", N, "For a person's reply: the seq of the agent's message it answers"),
            _c("to_away", N, "1 when it reached a person away while a delegate covered"),
            _c(
                "model_call_span_id", T, "The agent's model call that wrote it (model_calls.span_id); NULL when unknown"
            ),
            _c(
                "joined_by",
                T,
                "How that model call was found: trace, content or wake (the nearest, not a proven cause)",
            ),
            _c("call_id", N, "The HTTP call that sent it"),
        ],
    ),
    View(
        name="recipients",
        description="Each person each message was addressed to, with the moment in their own day.",
        order="seq, person",
        columns=[
            _c("seq", N, "The message (messages.seq)"),
            _c("person", T, "people.key"),
            _c("email", T, "The address it reached them at"),
            _c(
                "local_time",
                T,
                "When it reached them, in their working hours' timezone (ISO 8601 with offset); NULL with no hours",
            ),
            _c("in_working_hours", N, "1 inside their working hours, 0 outside, NULL when they declare none"),
            _c("away", N, "1 when they were away while a delegate covered"),
        ],
    ),
    View(
        name="calls",
        description="Every HTTP, gRPC and WebSocket exchange the proxy saw, bodies decoded: the provider fakes', the "
        "declared outbound hosts', the refused ones, tunnels to model APIs, and Minutehand's own calls as a person.",
        order="call_id",
        columns=[
            _c("call_id", N, "Its place among the calls this run sees, from 1"),
            _c("at", T, f"Simulated time it began; {TIME}"),
            _c("wake", N, "The wake it began in"),
            _c("provider", T, "The provider that answered; NULL when no provider claims the host"),
            _c("host", T, "The host called"),
            _c("method", T, "GET, POST, ...; CONNECT for a tunnel"),
            _c("path", T, "With its query string, credentials redacted; a gRPC call's method"),
            _c("status", N, "The HTTP status answered"),
            _c(
                "outcome",
                T,
                "answered, refused, not_implemented, internal_error, injected_fault or unavailable; NULL unclassified",
            ),
            _c(
                "answered_by",
                T,
                "provider, declaration, recording, pass_through (the real host), emulator, model, tunnel (relayed "
                "unopened), refused (nobody: 502) or minutehand (Minutehand acting as a person)",
            ),
            _c(
                "capture_mode",
                T,
                "For a host no provider claims: acknowledge, pass_through, replay, store, forward, discovered or service",
            ),
            _c("declared_as", T, "The declaration's host pattern that captured it"),
            _c("is_agent", N, "1 for the agent's own call; 0 for one Minutehand made as a person"),
            _c("request_body", T, "The request body as text, decoded; NULL when none or not text"),
            _c("response_body", T, "The answer's body as text, decoded; NULL when none or not text"),
            _c("request_binary", N, "1 when the request body is bytes, not text"),
            _c("response_binary", N, "1 when the answer's body is bytes, not text"),
            _c("request_size", N, "Bytes of the request body kept; NULL when none"),
            _c("response_size", N, "Bytes of the answer's body kept; NULL when none"),
            _c("first_seq", N, "The first event it wrote; NULL when it wrote none"),
            _c("last_seq", N, "The last event it wrote; NULL when it wrote none"),
            _c("trace_id", T, "The trace its traceparent named"),
            _c("caller_span_id", T, "The agent's span that made it, as its traceparent named it"),
            _c("started", T, "Real time it began, when the proxy kept it (captured and tunnelled calls)"),
            _c("ended", T, "Real time it ended, when kept"),
            _c("duration_ms", R, "ended less started, in milliseconds; NULL when not kept"),
            _c("grpc_status", T, "A gRPC call's status (OK, NOT_FOUND, ...)"),
            _c("frame_sender", T, "A WebSocket message's sender: agent or service"),
            _c("failure", T, "Why Minutehand answered in the fake's place: not implemented, or its own error"),
        ],
    ),
    View(
        name="wakes",
        description="Every wake of the agent, with why it began, what the agent reported as it ended, and what it did.",
        order="wake",
        columns=[
            _c("wake", N, "The wake, from 1"),
            _c("at", T, f"Simulated time it ran at; {TIME}"),
            _c(
                "reason",
                T,
                "start, due, person_replied, direction or tick; NULL for a run recorded before reasons were kept",
            ),
            _c("woken_by", T, "Comma-separated sources of what fell due and fired at that moment (dispatch.source)"),
            _c("world_changes", N, "The agent's changes to the world in it"),
            _c("memory_reads", N, "Gets and listings of its memory"),
            _c("memory_writes", N, "Keys of its memory written or deleted"),
            _c("commitments_changed", N, "1 when its reported commitments changed"),
            _c("checkpoint_seq", N, "The seq of the checkpoint written as it ended; NULL when none"),
            _c(
                "reported_status",
                T,
                "working, idle or done, as the agent reported at that checkpoint; NULL when it reported nothing",
            ),
            _c("reported_next_wake", T, "The next wake it reported then"),
            _c("actions", N, "The agent's acts in it (actions rows)"),
            _c("model_calls", N, "The agent's model calls placed in it"),
        ],
    ),
    View(
        name="dispatch",
        description="The run loop's table of what is due next, every entry as it last stood: what entered, when it "
        "was due, and how it left (fired, replaced, cancelled, delayed or dropped by the scenario's dispatch rules).",
        order="due_id",
        columns=[
            _c("due_id", N, "The entry, numbered in the order it entered"),
            _c("kind", T, "agent_wake, person_reply, direction, happening, machine, transition or service"),
            _c("ref", T, "What it refers to, in the run loop's words"),
            _c(
                "source",
                T,
                "reported, booked, polled, reply, happening, direction, machine, timer, transition, service, or call (a call of the agent's held until the world could answer it)",
            ),
            _c("due_at", T, f"When it was due; {TIME}"),
            _c("entered_at", T, "When it entered the table"),
            _c("entered_wake", N, "The wake in progress then; 0 is setup"),
            _c("closed", T, "fired, replaced, cancelled, delayed or dropped; NULL while still in the table"),
            _c("closed_at", T, "When it left"),
            _c("closed_wake", N, "The wake in progress when it left"),
            _c("fault", T, "The dispatch rule's fault that applied: late, twice or dropped"),
            _c("asked_for", T, "On a late or second delivery: the moment the agent asked for"),
            _c("drawn_from", T, "For a person's reply: delay, window, reminded, pinned or automatic"),
            _c("drawn_offset_seconds", R, "The span drawn after the ask, in seconds"),
        ],
    ),
    View(
        name="memory",
        description="The agent's memory (`minutehand.agent.store`) over time: every write and delete, and each get or "
        "listing that was the first of its key in its wake or found something other than the last one kept "
        "(`wakes.memory_reads` counts every one).",
        order="seq",
        columns=[
            _c("seq", N, "The event"),
            _c("at", T, f"Simulated time; {TIME}"),
            _c("wake", N, "The wake"),
            _c("actor", T, "agent, or scenario for the scenario's seeded memory and a fork's memory edit"),
            _c("op", T, "get, list, put or delete"),
            _c("collection", T, "The collection"),
            _c("key", T, "The key; for a listing, the prefix listed"),
            _c(
                "value",
                T,
                "JSON: what a put wrote, or what a get found then; NULL for a delete, a listing or a key not held",
            ),
        ],
    ),
    View(
        name="stored",
        description="Items the agent wrote to outbound hosts its agent file declares `store`, as each was stored.",
        order="seq",
        columns=[
            _c("seq", N, "The event"),
            _c("at", T, f"Simulated time; {TIME}"),
            _c("wake", N, "The wake"),
            _c("actor", T, "agent, or scenario"),
            _c("op", T, "create, update or delete"),
            _c("host", T, "The declaration's host pattern"),
            _c("collection", T, "The collection's name in the declaration"),
            _c("path", T, "The collection's path the item is under, as called"),
            _c("item_id", T, "The item's id"),
            _c("item", T, "The item as stored, JSON; NULL for a delete"),
        ],
    ),
    View(
        name="replies",
        description="What people said and decided, each reply as it landed: who, when, how its words were written, "
        "the facts it carried, the agent's message it answers, and the model call that wrote it.",
        order="reply_id",
        columns=[
            _c("reply_id", N, "The reply, numbered from 1 in the order decided"),
            _c("person", T, "people.key"),
            _c("at", T, f"When it lands, simulated; {TIME}"),
            _c(
                "kind",
                T,
                "message, press (a control used), decision (on an item in the agent's product) or automatic (away)",
            ),
            _c("writing", T, "script, verbatim, conversing, automatic or by_hand: where its words came from"),
            _c(
                "written_by",
                T,
                "model (a model wrote the words), verbatim (the scenario's exact words, or a control pressed), "
                "automatic (an away message) or by_hand (whoever drives a standing world)",
            ),
            _c("model", T, "The model that wrote it; NULL when none did"),
            _c("prompt_version", T, "The prompt it was written under"),
            _c("text", T, "What they said"),
            _c("facts", T, "JSON array of the facts a script step gave it to carry"),
            _c("decision", T, "The decision made, for a decision"),
            _c("answers_seq", N, "The seq of the agent's message or item it answers"),
            _c("in_reply_to", T, "The entity it answers: provider/kind/id"),
            _c("seq", N, "The event it landed as, matched by its moment and provider; NULL when none was found"),
            _c("drawn_from", T, "How its moment was drawn: delay, window, reminded, pinned or automatic"),
            _c("person_call_id", N, "The model call that wrote it (model_calls.person_call_id); NULL when none"),
        ],
    ),
    View(
        name="transitions",
        description="Every move of an item's state, by anyone: the agent through a provider's API, a person through "
        "the people engine, a person's own act (docs/design-transitions.md). A provider's recorded transition is "
        "given in its own words; every other write to an item in the world is the move it made, named create, "
        "update or delete, from the state its last write left (NULL: it did not exist) to `exists`, the "
        "ticket's state, or `deleted`. The world as the scenario set it up, the agent's memory and the run's "
        "own tables are no moves.",
        order="seq",
        columns=[
            _c("seq", N, "The event it is (events.seq)"),
            _c("at", T, f"Simulated time; {TIME}"),
            _c("wake", N, "The wake it happened in"),
            _c("provider", T, "The provider whose item it moved"),
            _c("item_kind", T, "The item's kind: ticket, message (an invitation), ..."),
            _c("item_id", T, "The item's id within its provider and kind"),
            _c("name", T, "The provider's own name for it: 'Start work', 'accepted'"),
            _c("from_state", T, "The state it left; NULL when it created the item"),
            _c("to_state", T, "The state it reached"),
            _c("actor", T, "agent, person, scenario, or a declared service's system or timer"),
            _c("who", T, "people.key of the person who made it; NULL for the agent"),
            _c("content", T, "What it carried, as a JSON object: a comment, the reasons"),
            _c("call_id", N, "The HTTP call that made it (calls.call_id); NULL when no call did"),
        ],
    ),
    View(
        name="items",
        description="Every item the people engine held pending on a person (docs/design-transitions.md): when it "
        "began to wait on them, when they act, and how it ended. A person acts once per turn: after their move it "
        "is pending on them again only once someone else moves it, as a row of its own.",
        order="pending_id",
        columns=[
            _c("pending_id", T, "The engine's record of it (events.entity_id of its pending rows)"),
            _c("person", T, "people.key it waits on"),
            _c("nth", N, "Which item pending on them in its provider it is, from 1"),
            _c("provider", T, "The provider it is in"),
            _c("item_kind", T, "The item's kind"),
            _c("item_id", T, "The item's id within its provider and kind (transitions.item_id)"),
            _c("state", T, "Its state when it began to wait on them"),
            _c("turn", N, "The seq of the last move anyone else made on it then; 0: none"),
            _c("since", T, f"When it began to wait on them, simulated; {TIME}"),
            _c("status", T, "pending, acted (they moved it) or gone (it stopped waiting on them first)"),
            _c("due_at", T, f"When they act; NULL: never (silent, or nothing pinned); {TIME}"),
            _c("take", T, "The transition the scenario pinned (`takes`); NULL: a model picks"),
            _c("drawn_from", T, "How `due_at` was drawn: delay, window or pinned"),
            _c("transition_seq", N, "The transition they took (transitions.seq); NULL until they act"),
            _c("closed_at", T, f"When it stopped waiting on them, simulated; NULL while it waits; {TIME}"),
            _c("failure", T, "Why their last try did not land, or why it was dropped"),
        ],
    ),
    View(
        name="model_calls",
        description="Model calls: the agent's, from the telemetry it exported or the wire with --record-model-calls, "
        "and the ones Minutehand made: to write what people say and declared services answer, and to judge the run. "
        "Cost only from prices the user declares.",
        order="at, span_id, person_call_id",
        columns=[
            _c(
                "side",
                T,
                "agent; person (a person's words or move); service (a declared service's); judge (a judged check, or "
                "a person's reply checked against what they know); assessor (the reviewer of the agent's effects)",
            ),
            _c("span_id", T, "The agent's model call span; NULL for a person's"),
            _c("person_call_id", N, "A person's model call, numbered from 1; NULL for the agent's"),
            _c(
                "source",
                T,
                "received (the agent exported it), wire (recorded on the wire) or person (Minutehand's own)",
            ),
            _c("person", T, "people.key, for a person's"),
            _c("wake", N, "The wake it is placed in"),
            _c("at", T, f"Simulated time: when it arrived (agent) or was made (person); {TIME}"),
            _c("started", T, "Real time it began (agent)"),
            _c("ended", T, "Real time it ended (agent)"),
            _c("duration_ms", R, "In milliseconds (agent)"),
            _c("model", T, "The model"),
            _c("prompt_version", T, "The prompt's version (person)"),
            _c(
                "input_tokens",
                N,
                "Every input token, as reported, cached ones included (an Anthropic call's input_tokens, "
                "cache_read_input_tokens and cache_creation_input_tokens summed)",
            ),
            _c("output_tokens", N, "Tokens out, as reported"),
            _c("cost", R, "From the prices the user declared for this model (--prices); NULL when none was declared"),
            _c("currency", T, "The declared price's currency"),
            _c("trace_id", T, "The agent's trace"),
            _c(
                "wrote",
                T,
                "For a person's: reply, transition or summary; for a declared service's: service_machine, service_route or "
                "service_answer; for a judge's: judgement or fact_check; for the assessor's: review",
            ),
            _c(
                "wrote_seqs",
                T,
                "JSON array of the agent messages it is joined to (agent) or of the message it answered (person)",
            ),
            _c("replayed", N, "1 for a person's call answered from the record: no model was called"),
            _c("failure", T, "Why a person's call failed"),
            _c("cache_read_tokens", N, "Of input_tokens, those read from the prompt cache; NULL when not reported"),
            _c(
                "cache_creation_tokens",
                N,
                "Of input_tokens, those written to the prompt cache (Anthropic); NULL when not reported",
            ),
            _c(
                "uncached_input_tokens",
                N,
                "Of input_tokens, those billed at the base input rate: input_tokens less both cached counts",
            ),
        ],
    ),
    View(
        name="findings",
        description="Every finding the run's checks raised: the assessment of the agent's effects against the declared "
        "world, the team's rules, the scenario's expectations and protected names, the agent's own checks, and the "
        "run's integrity checks.",
        order="finding_id",
        columns=[
            _c("finding_id", N, "Numbered from 1, as list_findings numbers them"),
            _c("check_id", T, "The check, or the team's rule by its id"),
            _c("kind", T, "fail, review or informational"),
            _c("severity", T, "error, warning or information"),
            _c("message", T, "What it says"),
            _c("at", T, f"When, simulated; {TIME}"),
            _c("wake", N, "The wake it is about"),
            _c("pattern", T, "The pattern that fixes it"),
            _c("pattern_title", T, "Its title"),
            _c("evidence", T, "JSON array of the seqs it cites"),
            _c("calls", T, "JSON array of the agent's calls it cites (calls.call_id)"),
            _c(
                "assessed_kind",
                T,
                "For the assessment of the agent's effects (`items`, `review`): violation, wrong_action or "
                "wrong_timing; NULL for any other finding",
            ),
            _c("item_kind", T, "The kind of item the effect was on: chat_message, email, ticket, ...; NULL: none"),
            _c("against", T, "The declaration the effect was measured against, named or quoted"),
            _c("judged_by", T, "The model that judged it, for a judged finding; NULL for a deterministic one"),
        ],
    ),
    View(
        name="evidence",
        description="Each seq a finding cites, one row each, to join findings to events, messages and actions.",
        order="finding_id, seq",
        columns=[
            _c("finding_id", N, "findings.finding_id"),
            _c("seq", N, "events.seq"),
        ],
    ),
    View(
        name="simulation_health",
        description="The simulated world's health, kept apart from the agent's findings: what did not play as the "
        "files declare (incomplete: the run is simulation_incomplete) and how much of what they declare the run "
        "reached (coverage).",
        order="health_id",
        columns=[
            _c("health_id", N, "Numbered from 1, in the order the report lists them"),
            _c(
                "kind",
                T,
                "responder_never_acts, waits_on_nobody, owed_unbooked, model_failed, service_failed, push_failed, "
                "waits_by_declaration, never_exercised or step_never_fired",
            ),
            _c("incomplete", N, "1 when it makes the run simulation_incomplete; 0 for coverage"),
            _c("words", T, "What happened, in one sentence"),
            _c("person", T, "people.key it is about; NULL when none"),
            _c("provider", T, "The provider or declared service of what it is about; NULL when nothing"),
            _c("entity_kind", T, "The kind of what it is about; NULL when nothing"),
            _c("entity_id", T, "What it is about, within its provider and kind; NULL when nothing"),
            _c("since", T, f"When it began, simulated; NULL when it has no moment; {TIME}"),
            _c("evidence", T, "JSON array of the seqs it cites"),
        ],
    ),
    View(
        name="pushes",
        description="Every send of an event the world pushed to the agent (a Slack event, a declared service's push), "
        "each retry a row of its own, with how the agent's address answered: duplicates and timeouts are counted here.",
        order="seq",
        columns=[
            _c("seq", N, "events.seq of the send"),
            _c("at", T, f"Simulated time; {TIME}"),
            _c("wake", N, "The wake in progress"),
            _c("provider", T, "The provider or declared service that pushed it"),
            _c("item", T, "What it was about: a Slack event's event_id, a declared service's item id"),
            _c("url", T, "The agent's address it was sent to"),
            _c("attempt", N, "0 for the first send, then each retry's number"),
            _c("retry_reason", T, "Why it was sent again, in the service's words; NULL for a first send"),
            _c("status", N, "The address's HTTP status; NULL when none came"),
            _c("failure", T, "Why it was not delivered; NULL when it was"),
            _c("seconds", R, "How long the address took to answer, real time; NULL when not measured"),
            _c("delivered", N, "1 when the address answered 2xx"),
        ],
    ),
    View(
        name="spans",
        description="The agent's own telemetry, every span it exported (and the model calls recorded on the wire), "
        "joined to the HTTP call it made and so to the world events that call wrote.",
        order="started, span_id",
        columns=[
            _c("span_id", T, "The span"),
            _c("trace_id", T, "Its trace"),
            _c("parent_span_id", T, "Its parent"),
            _c("name", T, "Its name"),
            _c("service", T, "The resource's service.name"),
            _c("source", T, "received, wire or log"),
            _c("wake", N, "The wake it is placed in"),
            _c("placed_by", T, "window (its start fell in the wake) or arrival (the wake it arrived in)"),
            _c("arrived_wake", N, "The wake in progress when it arrived"),
            _c("after_seq", N, "The head of the log when it arrived"),
            _c("at", T, f"Simulated time it arrived; {TIME}"),
            _c("started", T, "Real time, as the agent's SDK stamped it"),
            _c("ended", T, "Real time"),
            _c("duration_ms", R, "In milliseconds"),
            _c("status", T, "unset, ok or error"),
            _c("status_message", T, "The status message"),
            _c("attributes", T, "JSON object of every attribute, values decoded"),
            _c("is_model_call", N, "1 when the GenAI conventions mark it a model call"),
            _c(
                "call_id",
                N,
                "The HTTP call whose traceparent names it (calls.call_id); its events are first_seq..last_seq",
            ),
        ],
    ),
)

BY_NAME = {v.name: v for v in VIEWS}


def described() -> str:
    """Every view with its columns, as `minutehand query --schema` prints it."""
    lines = [f"Minutehand read model, version {VERSION}", ""]
    for view in VIEWS:
        lines.append(f"{view.name}: {view.description}")
        width = max(len(c.name) for c in view.columns)
        for column in view.columns:
            lines.append(f"  {column.name.ljust(width)}  {column.type.value.ljust(7)}  {column.description}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
