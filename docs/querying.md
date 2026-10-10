# Reading a run

A run is kept in one SQLite file, but its tables are Minutehand's own: a body of 512 bytes or more is a
compressed row of a shared table, a fork sees its parent's rows only up to its checkpoint, and whether a message
asked, followed up or answered is worked out by the ledger of waits, not stored. The read model is what a reader
queries instead: a fixed set of views over one run (or one fork), every body decoded, every time in one format, each
view and column documented here and listed by `minutehand query --schema`. A coding agent debugging its proactive
agent reads it to answer "what did the agent do, when, and why": every message to a person, what it read before
acting, its memory over time, its model calls and what they cost, and the chain from a wake to a person's reply.

## Three ways in

```bash
minutehand query <run> "SELECT seq, at, text FROM messages WHERE from_actor = 'agent'"   # --format table|json|csv
minutehand query --schema                         # every view and column below
minutehand trace <run> --person sofia             # the agent's acts in order: --provider --kind --from --to --wake --json
minutehand explain <run> <seq>                    # one event: what led to it and what followed (--json)
minutehand query <run> --export run.sqlite        # the read model as a file of its own, for any SQLite client
```

`<run>` is a run's id, a fork's id, or the start of exactly one. Over MCP (`minutehand mcp`) the same are the tools
`schema`, `query_run` (a page of at most 1,000 rows, with `offset` and `next_offset`), `trace` and `explain`.
`trace` is a filter over `actions`, and `explain` a handful of queries over the views below, so what they say is
what your own SQL over the same views finds.

Read-only: the run's world file is opened in SQLite's read-only mode, so a run still being written is read as of its
last commit, and the read model refuses every statement but a SELECT (or `WITH ... SELECT`): no write, no `CREATE`,
no `PRAGMA`, no `ATTACH`, one statement at a time. A statement running longer than 30 seconds is stopped.

## How it is built, and why

The read model is built on demand, when a run is first read in a process, into an SQLite database of its own in
memory, from the same readers the viewer, the checks and the MCP tools use: each view below is a table of it. It is
kept for the next query while the run's files are unchanged.

It is not a set of SQL views over the world file, because most of what a reader asks is not in a column there:

- **Bodies.** An entity's body, an event's snapshot, a call's request and answer and a span's string attributes are
  kept in their own row under 512 bytes and as a SHA-256 into `content` at 512 or more, zstd-compressed when that is
  smaller (`docs/design.md`, "Bytes kept once"). SQLite cannot decompress zstd; the readers can, and the read model
  holds every body as the text that crossed the wire (`calls.request_body`), a snapshot as JSON (`events.snapshot`).
  A body that is not text is not shown; `calls.request_binary` says so and `request_size` gives its length.
- **Forks.** A fork reads its parent's events up to its checkpoint, the calls its parent had recorded by then and
  the spans of the wakes before it, by the store's own rules. Every view of a fork holds exactly that and its own
  rows after; `events.run_id` says which run wrote a row.
- **Facts the ledger works out.** Whether a message asked (`messages.is_ask`), followed up (`is_follow_up`,
  `ask_seq`) or answered (`is_reply`, `answers_seq`) is the obligations ledger's (`checks/ledger.py`), and which model
  call wrote a message is the join of `application/model_calls.py` (`messages.joined_by`: by trace, by content, or
  the nearest call in the wake, which is no proven cause).
- **Files beside the log.** Findings are in the run's `result.json` and the wakes in its `record.json`.

Restating each of those in SQL over the raw tables would drift from the code that decides them. `--export` writes
the built read model as a plain SQLite file, which any client reads with no Minutehand code at all.

## Conventions

- **Time.** Every simulated and real moment is UTC text, `2026-08-24T10:00:00.000Z`: milliseconds always, so text
  order is time order. Compare as text, or with `julianday()`: `(julianday(b) - julianday(a)) * 24` is hours.
  `recipients.local_time` is the one exception: the person's own clock, with its offset.
- **Seq.** `seq` is an event's place in the world's log, the same number `findings`, `show_evidence` and the viewer
  use. A fork shares its parent's seqs up to `run.forked_at_seq`.
- **JSON.** Columns holding JSON (`to_people`, `evidence`, `facts`, `attributes`, `snapshot`, bodies) are read with
  SQLite's `json_each()` and `json_extract()`.
- **Flags** are 1 or 0; NULL is "not known", never "no".
- **Who.** A person is their `people.key` everywhere.
- **Cost** is only ever from prices you declare: Minutehand knows no model's price. A prices file, given with
  `--prices FILE` (or `prices` on the MCP tools):

  ```yaml
  prices:
    - {model: gpt-4o-mini, input_per_million: 0.15, output_per_million: 0.6, currency: USD}
    - {model: claude-haiku-4-5, input_per_million: 1.0, output_per_million: 5.0,
       cache_read_per_million: 0.1, cache_creation_per_million: 1.25}
  ```

  A call to a model it does not name, or with a token count unknown, has no cost. Input tokens are priced by kind:
  `uncached_input_tokens` at `input_per_million`, `cache_read_tokens` at `cache_read_per_million` and
  `cache_creation_tokens` at `cache_creation_per_million`; a call with cached tokens of a kind the file prices not
  has no cost, rather than one at a guessed rate.
- **Tokens.** `model_calls.input_tokens` is every input token, cached ones included, as OpenTelemetry's GenAI
  conventions count `gen_ai.usage.input_tokens`. Vendors count differently: Anthropic's `usage.input_tokens` leaves
  out `cache_read_input_tokens` and `cache_creation_input_tokens`, so a call recorded on the wire sums the three;
  OpenAI's `prompt_tokens` already holds the `cached_tokens` it details. A span the agent exported is read as the
  conventions say (`gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_creation.input_tokens` beside an
  `input_tokens` that includes them): an exporter that counts input without its cached tokens shows too few.

## The contract

The views are a contract, versioned: `minutehand_schema.version` and `run.read_model_version` give the version.
Within one version no view is removed and no column is removed, renamed, retyped, reordered or made to hold something
else; a new view, or a new column at the end of a view, may come in any version. Anything else is a new version.
`tests/query/test_schema.py` holds the views as they stand (`tests/query/schema_golden.json`) and fails when they
change without the version changing, and fails when this page and `minutehand query --schema` disagree.

## The views

### `minutehand_schema`

Every view of the read model and its columns, with the read model's version: this list. Kept in the order `view_name, position`.

| Column | Type | What it holds |
|---|---|---|
| `version` | INTEGER | The read model's version; a column removed, renamed or changed is a new version |
| `view_name` | TEXT | The view's name |
| `position` | INTEGER | The column's place in the view, from 1 |
| `column_name` | TEXT | The column's name |
| `type` | TEXT | TEXT, INTEGER or REAL |
| `description` | TEXT | What the column holds |

### `run`

The run being read, one row: a fork reads its parent's log up to the checkpoint it was taken at and its own after it, and every other view holds exactly what the fork can see. Kept in the order `run_id`.

| Column | Type | What it holds |
|---|---|---|
| `run_id` | TEXT | The run read |
| `root_run` | TEXT | The run whose world file holds it: itself, or the run its line of forks began with |
| `parent_run` | TEXT | The run it was forked from; NULL for a run played from the beginning |
| `forked_at_seq` | INTEGER | The last seq it shares with its parent; NULL when not a fork |
| `scenario` | TEXT | The scenario's name |
| `goal` | TEXT | The goal handed to the agent |
| `seed` | INTEGER | The run's seed; NULL while it runs |
| `starts_at` | TEXT | When the scenario starts, simulated; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `ended_at` | TEXT | When the run ended, simulated; NULL while it runs |
| `stop` | TEXT | How it stopped (agent_done, wake_limit, deadline_passed, ...); NULL while it runs |
| `verdict` | TEXT | passed, failed, unfinished, not_judged, tool_failed, environment_failed or simulation_incomplete; NULL while it runs |
| `verdict_words` | TEXT | The verdict in one sentence, as every surface states it |
| `finished` | INTEGER | 1 once the run has finished and been judged, else 0 |
| `read_model_version` | INTEGER | The read model's version |

### `people`

The scenario's people: who the agent can reach, and the hours they work. Kept in the order `key`.

| Column | Type | What it holds |
|---|---|---|
| `key` | TEXT | Person.key, as every other view names a person |
| `name` | TEXT | Their name |
| `email` | TEXT | Their email, which a message's recipients are matched by |
| `is_owner` | INTEGER | 1 for the scenario's owner |
| `reply` | TEXT | How they answer: scripted, answers (a model writes from their facts) or silent |
| `timezone` | TEXT | Their working hours' timezone; NULL when they declare no hours |
| `opens` | TEXT | When their working day opens, local HH:MM:SS; NULL when they declare no hours |
| `closes` | TEXT | When it closes, local HH:MM:SS; NULL when they declare no hours |
| `weekdays_only` | INTEGER | 1 when they work Monday to Friday only; NULL when they declare no hours |

### `events`

The world's log, every row of it in order: each change and read by the agent, a person or the scenario, the run loop's own checkpoints and table of what is due included. Kept in the order `seq`.

| Column | Type | What it holds |
|---|---|---|
| `seq` | INTEGER | The event's place in the log; every other view's seq is one of these |
| `run_id` | TEXT | The run that wrote it: for a fork, its parent's id on the rows it shares |
| `wake` | INTEGER | The wake it happened in; 0 is setup |
| `at` | TEXT | Simulated time; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `wall_time` | TEXT | Real time it was written |
| `actor` | TEXT | agent, person, scenario, or a declared service's system or timer |
| `operation` | TEXT | create, update, delete, read or search |
| `provider` | TEXT | The provider (slack, asana, ...), memory, minutehand (the run loop) or a declared host |
| `entity_kind` | TEXT | message, ticket, document, memory, stored, due, record, ... |
| `entity_id` | TEXT | The entity's id within its provider and kind |
| `snapshot` | TEXT | What the entity read after the change, as JSON; NULL when none was kept |
| `call_id` | INTEGER | The HTTP call that wrote it (calls.call_id); NULL when no call did |

### `actions`

Every act of the agent under test, in the order it acted: messages, other writes, reads, its memory, items it stored, the next wake it marked, calls that wrote nothing, and its model calls. Each row names the view holding the whole of it. Kept in the order `position`.

| Column | Type | What it holds |
|---|---|---|
| `position` | INTEGER | Its place in the agent's acts, from 1 |
| `seq` | INTEGER | The event it is (events.seq); NULL for a call that wrote nothing or a model call |
| `call_id` | INTEGER | The HTTP call it was made by, or is (calls.call_id); NULL when none |
| `span_id` | TEXT | The model call it is (model_calls.span_id); NULL otherwise |
| `at` | TEXT | Simulated time; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `wake` | INTEGER | The wake it happened in |
| `kind` | TEXT | message, write, read, memory, stored, next_wake, call (an HTTP call that wrote and read no event) or model_call |
| `operation` | TEXT | create, update, delete, read or search; for a call its HTTP method; for a model call chat |
| `provider` | TEXT | The provider, memory, a declared host, or the model's system for a model call |
| `target` | TEXT | What it acted on: a channel, an entity id, a memory key, host and path, or the model |
| `person` | TEXT | people.key of the person a message went to (the first, when several); else NULL |
| `summary` | TEXT | One line: what it said, wrote, read or asked, cut at 200 characters |
| `view` | TEXT | The view that holds the whole of it: messages, memory, stored, events, calls or model_calls |

### `messages`

Every message in the world, the agent's and people's, sent, rewritten or deleted, with what the ledger of waits knows of it: whether it asked, followed up or answered. Kept in the order `seq`.

| Column | Type | What it holds |
|---|---|---|
| `seq` | INTEGER | The event (events.seq) |
| `run_id` | TEXT | The run that wrote it |
| `at` | TEXT | Simulated time; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `wake` | INTEGER | The wake it happened in |
| `change` | TEXT | sent, edited or deleted |
| `provider` | TEXT | slack, microsoft, google_workspace, ... or a declared host for a captured send |
| `channel` | TEXT | The conversation: a channel, a chat, a mailbox thread, as the provider names it |
| `thread_of` | TEXT | The message it is a reply under, when it is in a thread |
| `message_id` | TEXT | The message's own id |
| `from_actor` | TEXT | agent, person or scenario |
| `from_person` | TEXT | people.key of a person's message, when a reply of theirs landed as it; else NULL |
| `to_people` | TEXT | JSON array of the people.key it was addressed to |
| `to_emails` | TEXT | JSON array of the addresses it was addressed to, as the provider recorded them |
| `text` | TEXT | What it said, decoded |
| `text_before` | TEXT | For an edit: what it said before |
| `is_ask` | INTEGER | 1 when it opened a wait on a person (the ledger's ask); agent messages only |
| `is_follow_up` | INTEGER | 1 when it was a follow-up on a wait still open; agent messages only |
| `ask_seq` | INTEGER | The seq of the ask it opened or followed up; NULL when neither |
| `is_reply` | INTEGER | 1 when it is a person's reply, as decided and landed |
| `answers_seq` | INTEGER | For a person's reply: the seq of the agent's message it answers |
| `to_away` | INTEGER | 1 when it reached a person away while a delegate covered |
| `model_call_span_id` | TEXT | The agent's model call that wrote it (model_calls.span_id); NULL when unknown |
| `joined_by` | TEXT | How that model call was found: trace, content or wake (the nearest, not a proven cause) |
| `call_id` | INTEGER | The HTTP call that sent it |

### `recipients`

Each person each message was addressed to, with the moment in their own day. Kept in the order `seq, person`.

| Column | Type | What it holds |
|---|---|---|
| `seq` | INTEGER | The message (messages.seq) |
| `person` | TEXT | people.key |
| `email` | TEXT | The address it reached them at |
| `local_time` | TEXT | When it reached them, in their working hours' timezone (ISO 8601 with offset); NULL with no hours |
| `in_working_hours` | INTEGER | 1 inside their working hours, 0 outside, NULL when they declare none |
| `away` | INTEGER | 1 when they were away while a delegate covered |

### `calls`

Every HTTP, gRPC and WebSocket exchange the proxy saw, bodies decoded: the provider fakes', the declared outbound hosts', the refused ones, tunnels to model APIs, and Minutehand's own calls as a person. Kept in the order `call_id`.

| Column | Type | What it holds |
|---|---|---|
| `call_id` | INTEGER | Its place among the calls this run sees, from 1 |
| `at` | TEXT | Simulated time it began; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `wake` | INTEGER | The wake it began in |
| `provider` | TEXT | The provider that answered; NULL when no provider claims the host |
| `host` | TEXT | The host called |
| `method` | TEXT | GET, POST, ...; CONNECT for a tunnel |
| `path` | TEXT | With its query string, credentials redacted; a gRPC call's method |
| `status` | INTEGER | The HTTP status answered |
| `outcome` | TEXT | answered, refused, not_implemented, internal_error, injected_fault or unavailable; NULL unclassified |
| `answered_by` | TEXT | provider, declaration, recording, pass_through (the real host), emulator, model, tunnel (relayed unopened), refused (nobody: 502) or minutehand (Minutehand acting as a person) |
| `capture_mode` | TEXT | For a host no provider claims: acknowledge, pass_through, replay, store, forward, discovered or service |
| `declared_as` | TEXT | The declaration's host pattern that captured it |
| `is_agent` | INTEGER | 1 for the agent's own call; 0 for one Minutehand made as a person |
| `request_body` | TEXT | The request body as text, decoded; NULL when none or not text |
| `response_body` | TEXT | The answer's body as text, decoded; NULL when none or not text |
| `request_binary` | INTEGER | 1 when the request body is bytes, not text |
| `response_binary` | INTEGER | 1 when the answer's body is bytes, not text |
| `request_size` | INTEGER | Bytes of the request body kept; NULL when none |
| `response_size` | INTEGER | Bytes of the answer's body kept; NULL when none |
| `first_seq` | INTEGER | The first event it wrote; NULL when it wrote none |
| `last_seq` | INTEGER | The last event it wrote; NULL when it wrote none |
| `trace_id` | TEXT | The trace its traceparent named |
| `caller_span_id` | TEXT | The agent's span that made it, as its traceparent named it |
| `started` | TEXT | Real time it began, when the proxy kept it (captured and tunnelled calls) |
| `ended` | TEXT | Real time it ended, when kept |
| `duration_ms` | REAL | ended less started, in milliseconds; NULL when not kept |
| `grpc_status` | TEXT | A gRPC call's status (OK, NOT_FOUND, ...) |
| `frame_sender` | TEXT | A WebSocket message's sender: agent or service |
| `failure` | TEXT | Why Minutehand answered in the fake's place: not implemented, or its own error |

### `wakes`

Every wake of the agent, with why it began, what the agent reported as it ended, and what it did. Kept in the order `wake`.

| Column | Type | What it holds |
|---|---|---|
| `wake` | INTEGER | The wake, from 1 |
| `at` | TEXT | Simulated time it ran at; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `reason` | TEXT | start, due, person_replied, direction or tick; NULL for a run recorded before reasons were kept |
| `woken_by` | TEXT | Comma-separated sources of what fell due and fired at that moment (dispatch.source) |
| `world_changes` | INTEGER | The agent's changes to the world in it |
| `memory_reads` | INTEGER | Gets and listings of its memory |
| `memory_writes` | INTEGER | Keys of its memory written or deleted |
| `commitments_changed` | INTEGER | 1 when its reported commitments changed |
| `checkpoint_seq` | INTEGER | The seq of the checkpoint written as it ended; NULL when none |
| `reported_status` | TEXT | working, idle or done, as the agent reported at that checkpoint; NULL when it reported nothing |
| `reported_next_wake` | TEXT | The next wake it reported then |
| `actions` | INTEGER | The agent's acts in it (actions rows) |
| `model_calls` | INTEGER | The agent's model calls placed in it |

### `dispatch`

The run loop's table of what is due next, every entry as it last stood: what entered, when it was due, and how it left (fired, replaced, cancelled, delayed or dropped by the scenario's dispatch rules). Kept in the order `due_id`.

| Column | Type | What it holds |
|---|---|---|
| `due_id` | INTEGER | The entry, numbered in the order it entered |
| `kind` | TEXT | agent_wake, person_reply, direction, happening, machine, transition or service |
| `ref` | TEXT | What it refers to, in the run loop's words |
| `source` | TEXT | reported, booked, polled, reply, happening, direction, machine, timer, transition, service, or call (a call of the agent's held until the world could answer it) |
| `due_at` | TEXT | When it was due; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `entered_at` | TEXT | When it entered the table |
| `entered_wake` | INTEGER | The wake in progress then; 0 is setup |
| `closed` | TEXT | fired, replaced, cancelled, delayed or dropped; NULL while still in the table |
| `closed_at` | TEXT | When it left |
| `closed_wake` | INTEGER | The wake in progress when it left |
| `fault` | TEXT | The dispatch rule's fault that applied: late, twice or dropped |
| `asked_for` | TEXT | On a late or second delivery: the moment the agent asked for |
| `drawn_from` | TEXT | For a person's reply: delay, window, reminded, pinned or automatic |
| `drawn_offset_seconds` | REAL | The span drawn after the ask, in seconds |

### `memory`

The agent's memory (`minutehand.agent.store`) over time: every write and delete, and each get or listing that was the first of its key in its wake or found something other than the last one kept (`wakes.memory_reads` counts every one). Kept in the order `seq`.

| Column | Type | What it holds |
|---|---|---|
| `seq` | INTEGER | The event |
| `at` | TEXT | Simulated time; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `wake` | INTEGER | The wake |
| `actor` | TEXT | agent, or scenario for the scenario's seeded memory and a fork's memory edit |
| `op` | TEXT | get, list, put or delete |
| `collection` | TEXT | The collection |
| `key` | TEXT | The key; for a listing, the prefix listed |
| `value` | TEXT | JSON: what a put wrote, or what a get found then; NULL for a delete, a listing or a key not held |

### `stored`

Items the agent wrote to outbound hosts its agent file declares `store`, as each was stored. Kept in the order `seq`.

| Column | Type | What it holds |
|---|---|---|
| `seq` | INTEGER | The event |
| `at` | TEXT | Simulated time; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `wake` | INTEGER | The wake |
| `actor` | TEXT | agent, or scenario |
| `op` | TEXT | create, update or delete |
| `host` | TEXT | The declaration's host pattern |
| `collection` | TEXT | The collection's name in the declaration |
| `path` | TEXT | The collection's path the item is under, as called |
| `item_id` | TEXT | The item's id |
| `item` | TEXT | The item as stored, JSON; NULL for a delete |

### `replies`

What people said and decided, each reply as it landed: who, when, how its words were written, the facts it carried, the agent's message it answers, and the model call that wrote it. Kept in the order `reply_id`.

| Column | Type | What it holds |
|---|---|---|
| `reply_id` | INTEGER | The reply, numbered from 1 in the order decided |
| `person` | TEXT | people.key |
| `at` | TEXT | When it lands, simulated; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `kind` | TEXT | message, press (a control used), decision (on an item in the agent's product) or automatic (away) |
| `writing` | TEXT | script, verbatim, conversing, automatic or by_hand: where its words came from |
| `written_by` | TEXT | model (a model wrote the words), verbatim (the scenario's exact words, or a control pressed), automatic (an away message) or by_hand (whoever drives a standing world) |
| `model` | TEXT | The model that wrote it; NULL when none did |
| `prompt_version` | TEXT | The prompt it was written under |
| `text` | TEXT | What they said |
| `facts` | TEXT | JSON array of the facts a script step gave it to carry |
| `decision` | TEXT | The decision made, for a decision |
| `answers_seq` | INTEGER | The seq of the agent's message or item it answers |
| `in_reply_to` | TEXT | The entity it answers: provider/kind/id |
| `seq` | INTEGER | The event it landed as, matched by its moment and provider; NULL when none was found |
| `drawn_from` | TEXT | How its moment was drawn: delay, window, reminded, pinned or automatic |
| `person_call_id` | INTEGER | The model call that wrote it (model_calls.person_call_id); NULL when none |

### `transitions`

Every move of an item's state, by anyone: the agent through a provider's API, a person through the people engine, a person's own act (docs/design-transitions.md). A provider's recorded transition is given in its own words; every other write to an item in the world is the move it made, named create, update or delete, from the state its last write left (NULL: it did not exist) to `exists`, the ticket's state, or `deleted`. The world as the scenario set it up, the agent's memory and the run's own tables are no moves. Kept in the order `seq`.

| Column | Type | What it holds |
|---|---|---|
| `seq` | INTEGER | The event it is (events.seq) |
| `at` | TEXT | Simulated time; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `wake` | INTEGER | The wake it happened in |
| `provider` | TEXT | The provider whose item it moved |
| `item_kind` | TEXT | The item's kind: ticket, message (an invitation), ... |
| `item_id` | TEXT | The item's id within its provider and kind |
| `name` | TEXT | The provider's own name for it: 'Start work', 'accepted' |
| `from_state` | TEXT | The state it left; NULL when it created the item |
| `to_state` | TEXT | The state it reached |
| `actor` | TEXT | agent, person, scenario, or a declared service's system or timer |
| `who` | TEXT | people.key of the person who made it; NULL for the agent |
| `content` | TEXT | What it carried, as a JSON object: a comment, the reasons |
| `call_id` | INTEGER | The HTTP call that made it (calls.call_id); NULL when no call did |

### `reactions`

Each change someone else made in the world (a reply, a decision, an ask-back), when the agent could first know it, and when it acted on it: facts, not verdicts (`domain.reactions`). It could know a change from a wake that carried it (a reply wakes it) or from its first read of the item answered 2xx after it; a read that failed is no read. NULL where it never saw the change, or never acted after seeing it. Kept in the order `change_seq`.

| Column | Type | What it holds |
|---|---|---|
| `change_seq` | INTEGER | The change (transitions.seq) |
| `at` | TEXT | When it was made; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `provider` | TEXT | The provider whose item it changed |
| `item_kind` | TEXT | The item's kind |
| `item_id` | TEXT | The item's id within its provider and kind |
| `change` | TEXT | The move's name: reply, approve, ask_back, Start work |
| `to_state` | TEXT | The state it reached |
| `actor` | TEXT | person, or a declared service's system or timer |
| `who` | TEXT | people.key of the person who made it |
| `seen_at` | TEXT | When the agent could first know it; NULL: never |
| `seen_by` | TEXT | wake (a reply woke it) or read (its own read showed it) |
| `seen_call_id` | INTEGER | The read that showed it (calls.call_id), when a read did |
| `unseen_seconds` | REAL | How long it sat unseen |
| `acted_seq` | INTEGER | The agent's first move in the world after it saw it (transitions.seq) |
| `acted_at` | TEXT | When |
| `acted` | TEXT | Which: its provider and the move's name |
| `to_act_seconds` | REAL | How long after seeing it the agent moved |

### `items`

Every item the people engine held pending on a person (docs/design-transitions.md): when it began to wait on them, when they act, and how it ended. A person acts once per turn: after their move it is pending on them again only once someone else moves it, as a row of its own. Kept in the order `pending_id`.

| Column | Type | What it holds |
|---|---|---|
| `pending_id` | TEXT | The engine's record of it (events.entity_id of its pending rows) |
| `person` | TEXT | people.key it waits on |
| `nth` | INTEGER | Which item pending on them in its provider it is, from 1 |
| `provider` | TEXT | The provider it is in |
| `item_kind` | TEXT | The item's kind |
| `item_id` | TEXT | The item's id within its provider and kind (transitions.item_id) |
| `state` | TEXT | Its state when it began to wait on them |
| `turn` | INTEGER | The seq of the last move anyone else made on it then; 0: none |
| `since` | TEXT | When it began to wait on them, simulated; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `status` | TEXT | pending, acted (they moved it) or gone (it stopped waiting on them first) |
| `due_at` | TEXT | When they act; NULL: never (silent, or nothing pinned); UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `take` | TEXT | The transition the scenario pinned (`takes`); NULL: a model picks |
| `drawn_from` | TEXT | How `due_at` was drawn: delay, window or pinned |
| `transition_seq` | INTEGER | The transition they took (transitions.seq); NULL until they act |
| `closed_at` | TEXT | When it stopped waiting on them, simulated; NULL while it waits; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `failure` | TEXT | Why their last try did not land, or why it was dropped |

### `model_calls`

Model calls: the agent's, from the telemetry it exported or the wire (recorded by default), and the ones Minutehand made: to write what people say and declared services answer, and to judge the run. Cost only from prices the user declares. Kept in the order `at, span_id, person_call_id`.

| Column | Type | What it holds |
|---|---|---|
| `side` | TEXT | agent; person (a person's words or move); service (a declared service's); judge (a judged check, or a person's reply checked against what they know); assessor (the reviewer of the agent's effects) |
| `span_id` | TEXT | The agent's model call span; NULL for a person's |
| `person_call_id` | INTEGER | A person's model call, numbered from 1; NULL for the agent's |
| `source` | TEXT | received (the agent exported it), wire (recorded on the wire) or person (Minutehand's own) |
| `person` | TEXT | people.key, for a person's |
| `wake` | INTEGER | The wake it is placed in |
| `at` | TEXT | Simulated time: when it arrived (agent) or was made (person); UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `started` | TEXT | Real time it began (agent) |
| `ended` | TEXT | Real time it ended (agent) |
| `duration_ms` | REAL | In milliseconds (agent) |
| `model` | TEXT | The model |
| `prompt_version` | TEXT | The prompt's version (person) |
| `input_tokens` | INTEGER | Every input token, as reported, cached ones included (an Anthropic call's input_tokens, cache_read_input_tokens and cache_creation_input_tokens summed) |
| `output_tokens` | INTEGER | Tokens out, as reported |
| `cost` | REAL | From the prices the user declared for this model (--prices); NULL when none was declared |
| `currency` | TEXT | The declared price's currency |
| `trace_id` | TEXT | The agent's trace |
| `wrote` | TEXT | For a person's: reply, transition or summary; for a declared service's: service_machine, service_route or service_answer; for a judge's: judgement or fact_check; for the assessor's: review |
| `wrote_seqs` | TEXT | JSON array of the agent messages it is joined to (agent) or of the message it answered (person) |
| `replayed` | INTEGER | 1 for a person's call answered from the record: no model was called |
| `failure` | TEXT | Why a person's call failed |
| `cache_read_tokens` | INTEGER | Of input_tokens, those read from the prompt cache; NULL when not reported |
| `cache_creation_tokens` | INTEGER | Of input_tokens, those written to the prompt cache (Anthropic); NULL when not reported |
| `uncached_input_tokens` | INTEGER | Of input_tokens, those billed at the base input rate: input_tokens less both cached counts |

### `findings`

Every finding the run's checks raised: the assessment of the agent's effects against the declared world, the team's rules, the scenario's expectations and protected names, the agent's own checks, and the run's integrity checks. Kept in the order `finding_id`.

| Column | Type | What it holds |
|---|---|---|
| `finding_id` | INTEGER | Numbered from 1, as list_findings numbers them |
| `check_id` | TEXT | The check, or the team's rule by its id |
| `kind` | TEXT | fail, review or informational |
| `severity` | TEXT | error, warning or information |
| `message` | TEXT | What it says |
| `at` | TEXT | When, simulated; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `wake` | INTEGER | The wake it is about |
| `pattern` | TEXT | The pattern that fixes it |
| `pattern_title` | TEXT | Its title |
| `evidence` | TEXT | JSON array of the seqs it cites |
| `calls` | TEXT | JSON array of the agent's calls it cites (calls.call_id) |
| `assessed_kind` | TEXT | For the assessment of the agent's effects (`items`, `review`): violation, wrong_action or wrong_timing; NULL for any other finding |
| `item_kind` | TEXT | The kind of item the effect was on: chat_message, email, ticket, ...; NULL: none |
| `against` | TEXT | The declaration the effect was measured against, named or quoted |
| `judged_by` | TEXT | The model that judged it, for a judged finding; NULL for a deterministic one |

### `evidence`

Each seq a finding cites, one row each, to join findings to events, messages and actions. Kept in the order `finding_id, seq`.

| Column | Type | What it holds |
|---|---|---|
| `finding_id` | INTEGER | findings.finding_id |
| `seq` | INTEGER | events.seq |

### `simulation_health`

The simulated world's health, kept apart from the agent's findings: what did not play as the files declare (incomplete: the run is simulation_incomplete) and how much of what they declare the run reached (coverage). Kept in the order `health_id`.

| Column | Type | What it holds |
|---|---|---|
| `health_id` | INTEGER | Numbered from 1, in the order the report lists them |
| `kind` | TEXT | responder_never_acts, waits_on_nobody, owed_unbooked, model_failed, service_failed, push_failed, waits_by_declaration, never_exercised or step_never_fired |
| `incomplete` | INTEGER | 1 when it makes the run simulation_incomplete; 0 for coverage |
| `words` | TEXT | What happened, in one sentence |
| `person` | TEXT | people.key it is about; NULL when none |
| `provider` | TEXT | The provider or declared service of what it is about; NULL when nothing |
| `entity_kind` | TEXT | The kind of what it is about; NULL when nothing |
| `entity_id` | TEXT | What it is about, within its provider and kind; NULL when nothing |
| `since` | TEXT | When it began, simulated; NULL when it has no moment; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `evidence` | TEXT | JSON array of the seqs it cites |

### `pushes`

Every send of an event the world pushed to the agent (a Slack event, a declared service's push), each retry a row of its own, with how the agent's address answered: duplicates and timeouts are counted here. Kept in the order `seq`.

| Column | Type | What it holds |
|---|---|---|
| `seq` | INTEGER | events.seq of the send |
| `at` | TEXT | Simulated time; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `wake` | INTEGER | The wake in progress |
| `provider` | TEXT | The provider or declared service that pushed it |
| `item` | TEXT | What it was about: a Slack event's event_id, a declared service's item id |
| `url` | TEXT | The agent's address it was sent to |
| `attempt` | INTEGER | 0 for the first send, then each retry's number |
| `retry_reason` | TEXT | Why it was sent again, in the service's words; NULL for a first send |
| `status` | INTEGER | The address's HTTP status; NULL when none came |
| `failure` | TEXT | Why it was not delivered; NULL when it was |
| `seconds` | REAL | How long the address took to answer, real time; NULL when not measured |
| `delivered` | INTEGER | 1 when the address answered 2xx |

### `spans`

The agent's own telemetry, every span it exported (and the model calls recorded on the wire), joined to the HTTP call it made and so to the world events that call wrote. Kept in the order `started, span_id`.

| Column | Type | What it holds |
|---|---|---|
| `span_id` | TEXT | The span |
| `trace_id` | TEXT | Its trace |
| `parent_span_id` | TEXT | Its parent |
| `name` | TEXT | Its name |
| `service` | TEXT | The resource's service.name |
| `source` | TEXT | received, wire or log |
| `wake` | INTEGER | The wake it is placed in |
| `placed_by` | TEXT | window (its start fell in the wake) or arrival (the wake it arrived in) |
| `arrived_wake` | INTEGER | The wake in progress when it arrived |
| `after_seq` | INTEGER | The head of the log when it arrived |
| `at` | TEXT | Simulated time it arrived; UTC, ISO 8601 with milliseconds and a Z (`2026-08-24T10:00:00.000Z`): compares as text, and julianday() reads it |
| `started` | TEXT | Real time, as the agent's SDK stamped it |
| `ended` | TEXT | Real time |
| `duration_ms` | REAL | In milliseconds |
| `status` | TEXT | unset, ok or error |
| `status_message` | TEXT | The status message |
| `attributes` | TEXT | JSON object of every attribute, values decoded |
| `is_model_call` | INTEGER | 1 when the GenAI conventions mark it a model call |
| `call_id` | INTEGER | The HTTP call whose traceparent names it (calls.call_id); its events are first_seq..last_seq |

## Queries an agent asks

Each runs as written against a run: `tests/query/test_docs_queries.py` runs every one on a run written by hand and expects rows, and `tests/architecture/test_read_model_on_real_runs.py` runs every one on real runs of the two examples, expecting rows wherever the run holds the data. Change a
person's key, a memory key or a host to your own run's.

### Follow-ups per person, and the gap before each

```sql
SELECT r.person, m.seq, m.at, m.is_ask, m.is_follow_up,
       ROUND((julianday(m.at) - julianday(LAG(m.at) OVER (PARTITION BY r.person ORDER BY m.seq))) * 24, 1)
         AS hours_since_previous
FROM messages m JOIN recipients r ON r.seq = m.seq
WHERE m.from_actor = 'agent' AND m.change = 'sent' AND r.person IS NOT NULL
ORDER BY r.person, m.seq;
```

### Time from each ask to its answer

```sql
SELECT a.seq AS ask, p.person, a.at AS asked_at, p.at AS answered_at,
       ROUND((julianday(p.at) - julianday(a.at)) * 24, 1) AS hours
FROM replies p JOIN messages a ON a.seq = p.answers_seq
ORDER BY a.seq;
```

### Asks never answered

```sql
SELECT m.seq, m.at, m.to_people, m.text
FROM messages m
WHERE m.is_ask = 1
  AND NOT EXISTS (SELECT 1 FROM replies p WHERE p.answers_seq = m.seq)
ORDER BY m.seq;
```

### What the agent read before each message to a person

```sql
SELECT m.seq AS message, m.person, r.position, r.kind, r.target, r.summary
FROM actions m
JOIN actions r ON r.wake = m.wake AND r.position < m.position
WHERE m.kind = 'message' AND m.person IS NOT NULL
  AND (r.kind = 'read'
       OR (r.kind = 'memory' AND r.operation IN ('read', 'search'))
       OR (r.kind = 'call' AND r.operation = 'GET'))
ORDER BY m.position, r.position;
```

### One memory key over time

```sql
SELECT seq, at, wake, actor, op, value
FROM memory
WHERE collection = 'default' AND key = 'asks/sofia'
ORDER BY seq;
```

### Memory reads that found nothing

```sql
SELECT seq, at, wake, collection, key
FROM memory
WHERE op = 'get' AND value IS NULL
ORDER BY seq;
```

### Model tokens, and cost, per wake

```sql
SELECT wake, COUNT(*) AS calls, SUM(input_tokens) AS tokens_in, SUM(output_tokens) AS tokens_out,
       SUM(cost) AS cost, MAX(currency) AS currency
FROM model_calls
WHERE side = 'agent'
GROUP BY wake
ORDER BY wake;
```

### The model call behind each message

```sql
SELECT m.seq, m.text, m.joined_by, c.model, c.input_tokens, c.output_tokens
FROM messages m JOIN model_calls c ON c.span_id = m.model_call_span_id
ORDER BY m.seq;
```

### Calls a declaration answered, and calls refused

```sql
SELECT call_id, at, method, host, path, status, answered_by, declared_as
FROM calls
WHERE answered_by IN ('declaration', 'refused')
ORDER BY call_id;
```

### What the agent sent, read out of the request bodies

```sql
SELECT call_id, at, json_extract(request_body, '$.channel') AS channel, json_extract(request_body, '$.text') AS text,
       status
FROM calls
WHERE host = 'slack.com' AND path LIKE '/api/chat.postMessage%'
ORDER BY call_id;
```

### Actions after the agent reported it was done

```sql
SELECT a.position, a.wake, a.seq, a.kind, a.target, a.summary
FROM actions a
WHERE a.seq > (SELECT MIN(checkpoint_seq) FROM wakes WHERE reported_status = 'done')
   OR a.wake > (SELECT MIN(wake) FROM wakes WHERE reported_status = 'done')
ORDER BY a.position;
```

### Messages sent outside a person's working hours

```sql
SELECT m.seq, r.person, m.at, r.local_time, m.text
FROM messages m JOIN recipients r ON r.seq = m.seq
WHERE m.from_actor = 'agent' AND r.in_working_hours = 0
ORDER BY m.seq;
```

### Why each wake began, and what it did

```sql
SELECT wake, at, reason, woken_by, reported_status, world_changes, actions, model_calls
FROM wakes
ORDER BY wake;
```

### People's replies, and how their words were written

```sql
SELECT p.reply_id, p.person, p.at, p.written_by, p.model, p.prompt_version, p.facts, p.text,
       c.input_tokens, c.output_tokens
FROM replies p LEFT JOIN model_calls c ON c.person_call_id = p.person_call_id
ORDER BY p.reply_id;
```

### The chain behind each failed finding

```sql
SELECT f.finding_id, f.check_id, e.seq, ev.actor, ev.entity_kind, m.text,
       c.method || ' ' || c.host || c.path AS call, m.model_call_span_id, w.reason AS wake_reason
FROM findings f
JOIN evidence e ON e.finding_id = f.finding_id
JOIN events ev ON ev.seq = e.seq
LEFT JOIN messages m ON m.seq = e.seq
LEFT JOIN calls c ON c.call_id = ev.call_id
LEFT JOIN wakes w ON w.wake = ev.wake
WHERE f.kind = 'fail'
ORDER BY f.finding_id, e.seq;
```

### The agent's own spans behind each call it made

```sql
SELECT s.name, s.trace_id, s.span_id, c.call_id, c.method, c.host, c.path, c.first_seq, c.last_seq
FROM spans s JOIN calls c ON c.call_id = s.call_id
ORDER BY c.call_id;
```
