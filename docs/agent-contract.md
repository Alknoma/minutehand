# The agent contract

This page lists every way Minutehand and an agent's own repository touch, with one term for each. Field names, docs,
command output and errors use these terms. Where a name in the code says otherwise, it is listed under "Known debt"
at the end and has not been renamed.

**Nothing is asked of the agent.** Every agent runs unchanged, in a sandbox whose clock and network Minutehand owns
(`sandbox` in the agent file; `docs/design.md`, "A sandbox whose clock Minutehand owns"). Its calls reach the fakes
because the sandbox has no other way out; its next wake is a record, never a guess: a scheduler it books with
(Cloud Tasks, EventBridge Scheduler) delivers when the clock reaches the booking, and a due record it keeps in its
memory falls due at the instant the record holds when the run decides its next jump. A timer or sleep of its own
fires when the clock passes it, and moves nothing. The agent carries no endpoint, protocol or import that exists for
Minutehand, with one exception an agent opts into: an agent that remembers anything across wakes may keep it through
`minutehand.agent.store` ("The agent's memory", below), so a fork starts from the memory as it stood at the
checkpoint.

## The terms

| Term | Means | Never call it |
|---|---|---|
| **agent file** | The YAML or JSON file an agent's repository keeps: `AgentUnderTest` | config, manifest |
| **scenario**, **seed** | The situation a run plays (`Scenario`), or a standing world opens with (`Seed`) | fixture |
| **sandbox** | Where the agent runs: a gVisor sandbox whose clock Minutehand moves and whose only route out is the proxy | container (that is the outer one), VM |
| **wake** | One go of the agent's, as the run records it: from the moment something reaches it (a person's answer, a pushed event, a booking delivered, a due record's moment) until its sandbox is idle again | tick, trigger, turn |
| **timer** | A deadline a task in the sandbox waits for (a sleep, a scheduler loop's next check). It fires when the clock passes it for some other reason, and is never a wake: Minutehand does not read it | schedule (that is its own scheduler's) |
| **booking** | A wake the agent asked a faked scheduler for, by calling it: a record in the run's log, delivered at its moment, changed or cancelled through the scheduler's own API | cron |
| **due record** | A key of the agent's memory holding when its own scheduler next runs something (a job's run-at, a next-check field), at a place the agent file names (`due:`): read as it stands whenever the run decides its next jump | timer, reminder |
| **memory** | What the agent remembers across wakes, through `minutehand.agent.store`: the run's own under Minutehand. A write may carry a version, and lands only when none newer is held | state, snapshot |
| **deliver** | Minutehand handing the agent what a person did: a reply, a press, a happening | push (that is the provider's word) |
| **inbox** | Where work waits on a person in the agent's own product | queue, human action |
| **item** | One thing waiting in an inbox | task, request |
| **decide** | A person settling an item with a decision and its inputs | approve (that is one decision) |
| **use** | A person doing something in the agent's own product unprompted: opening a page, writing to it | event, interaction |
| **declared host** | A host the agent calls that Minutehand captures: acknowledge, pass through, replay, forward | mock |
| **operation** | An `operationId` in an OpenAPI document that a declaration names instead of a template | endpoint |
| **playback** | A fork bringing the agent back to a checkpoint by playing its recorded inputs into a fresh sandbox | restore (that is the memory's word), snapshot |

## Touch points

| Touch point | Who calls whom, when | Request → response | Declared by | Required |
|---|---|---|---|---|
| **sandbox** | Minutehand → the sandbox's control calls, at the end of each wake (whether it is idle, and its clock) and at each jump of the clock (its clock advanced); the run's start → the agent's command, as `MINUTEHAND_STARTS_AT_NS`, the moment its sandbox is born at; the transparent listener and the name answerer → the agent's command, as `MINUTEHAND_TRANSPARENT_PORT` and `MINUTEHAND_DNS_PORT`, what `sandbox/seal.sh` sends every connection and lookup from the sandbox to | `deadlines` → `{"idle", "now_ns", ...}` (anything else it prints is ignored); `advance` with `{nanoseconds}` → exit 0 | `sandbox.deadlines`, `sandbox.advance` | Yes |
| **goal** | An older scenario's owner → agent, as a message on a provider, at the start; directions the same way | The provider's own event | `goal.provider`, with an `inbound[]` target on it | Only for a scenario that hands the agent a `goal` |
| **memory** | The agent → Minutehand's receiver, each time it reads or writes what it remembers | `minutehand.agent.store` (`POST $MINUTEHAND_AGENT_URL/store`) | Nothing: MINUTEHAND_ON and MINUTEHAND_AGENT_URL are handed out | Only for an agent that remembers across wakes and is forked, or keeps due records |
| **own database** | Minutehand → the agent's environment, before each run and fork | A fresh empty SQLite file, its path or `sqlite:///` URL | `own_databases[]` (`env`, `form`) | No |
| **deliver a reply** (provider) | Minutehand → agent, when a person's reply falls due | The provider's own event (Slack Events API, Bot Framework activity) → 2xx | `inbound[]` | Only for a provider that pushes |
| **socket** (Slack) | The agent → Slack's `apps.connections.open`, then a WebSocket it holds open; replies arrive on it | `events_api` envelopes → the agent's `{"envelope_id": …}` | `inbound[]` with `delivery: socket_mode` and no `url` | Instead of a request URL |
| **deliver a press** (provider) | Minutehand → agent, when a person uses a control | The provider's own interactivity payload → 2xx | `inbound[].interactivity_url` | Only for controls |
| **deliver a reply** (declared host) | Minutehand → agent, an answer to a captured send | `DeliveredReply` (the default shape, no `body`), or the declared `body` → 2xx | `outbound[].replies` | Only when people answer a send |
| **deliver a booking** (scheduler) | The faked scheduler → agent, when the clock reaches a booking | The scheduler's own delivery (a Cloud Tasks HTTP request, a message in an SQS queue the agent polls) | Nothing: the agent's own call booked it | No |
| **due records** | Minutehand reads the agent's memory whenever it decides its next jump; nothing is called | The keys `due[]` names, as they stand; the earliest instant is a wake | `due[]` (`collection`, `prefix`, `where`, `field`, `poll`) | Only for an agent whose own scheduler polls its records |
| **inbox list** | Minutehand → agent, as each person, after each wake or step and before the clock moves | Declared template or operation (default `listPending`, `PendingPage`) | `inboxes[].pending` | Only with an inbox |
| **decide** | Minutehand → agent, as the person, when a decision falls due | Declared template or operation (default `decide`, `DecisionMade`) → declared success | `inboxes[].decisions[]` | Only with an inbox |
| **use** | Minutehand → agent, as the person, when a scenario's `uses` happening falls due | Declared template or operation → declared success | `inboxes[].uses[]` | Only to play people using the product unprompted |
| **declared hosts** | The agent → a host no provider claims | Captured as declared | `outbound[]` (`acknowledge`, `pass_through`, `replay`, `forward`) | No |
| **emulators** | Minutehand → a fake outside it, for `forward` hosts | The emulator's own | `emulators[]` | No |
| **base URLs** | The agent → the proxy's `/_host/…`, for a client without a proxy | As the real host | `base_urls[]` | No |
| **model hosts** | The agent → its model API, recorded or tunnelled | As the real host | `--model-host`, `CreateWorld.model_hosts` | No |
| **telemetry** | The agent → Minutehand's OTLP receiver | OTLP/HTTP or gRPC | Environment Minutehand hands out | No |
| **assessments** | Every run's effects on the world, by kind of item, against the world its files declare (goal, deadline, people's facts and windows, services and their machines, the agent's rhythm); the team's own rules over the facts on top | findings that name what they were measured against (`docs/assessments.md`); with `--judge`, the shared reviewer's, for review | nothing for the automatic assessment; `assess[]` in the agent file, `assess[]` and `assess_off[]` in a scenario for the team's own | No |
| **own checks** | Minutehand runs the agent's checks after every run and fork: the escape hatch for what a rule cannot say | A class with `id`, `needs` and `run(view) -> CheckReport`, reading the facts `minutehand.checks.facts` gives (`asks`, `messages`, `writes`, `planned_wakes`) | `checks[]`: Python files, a relative path read from the agent file's folder | No |

Everything the world sends the agent goes through one door (`adapters/agent/delivery.py`) and is recorded whole:
method, URL, every header and the body (`PushSnapshot`). With the answer to every call the agent made (`Exchange`:
status, headers, body) and its memory read back, the record holds everything the agent ever received.

## A wake, and how long Minutehand waits

A wake begins when something reaches the agent and ends when its sandbox is idle: every task in it blocked, on two
reads `sandbox.quiet` apart (default 50 ms), with no call of the agent's in flight. A sandbox still busy after
`sandbox.settle_limit` (default 30 s of real time) stops the run `agent_failed`, saying so. Nothing is printed while
it waits.

A run ends when nothing is due, the agent has booked nothing and no due record of its is left to wake for
(`nothing_pending`), at the end of the scenario's
window (`window_ended`, or an older scenario's `deadline_passed`), at the wake limit, or when the agent's sandbox
fails to fall idle. There is no `done`: whether the work is finished is read from the world (`docs/design.md`, "The
verdict"), never from the agent.

| Field | Default | What it bounds |
|---|---|---|
| `sandbox.settle_limit` | `PT30S` | Real time one wake may take to fall idle |
| `sandbox.quiet` | `PT0.05S` | Idle on two reads this far apart is idle |
| `start_limit` | `PT30S` | Real time the agent's command may take, from its launch, before the address the world pushes to accepts connections; nothing is pushed before then. An agent whose front door opens only once everything behind it is up declares how long that takes |
| `due[].poll` | `PT0S` | How long the agent's own poller may take to find a due record after it falls due: the wake is that long after the record's instant, the latest its poller would have acted |
| `take_limit` | `PT30S` | Real time the run waits for the agent to take a booking's delivery from its own queue, when the scheduler can tell (`ConfirmsDelivery`) |
| `inbound[].push_timeout` | `PT5M` | One push to `url` or `interactivity_url` (a Slack event, a press, a Bot Framework activity) before it counts as unanswered and is sent again, as the vendor retries it (Slack: up to three more times, `X-Slack-Retry-Num`, `X-Slack-Retry-Reason: http_timeout`); still unanswered after the last, the run stops `agent_failed` |

Slack itself waits three seconds for an event's 2xx and then retries: an app that handles an event inside the request
(a model's turn in the handler) is sent it again by the real service, and handles it more than once. The default
`push_timeout` leaves room for such a handler so a first run is not cut short; set `push_timeout: PT3S` to be held to
Slack's own window, which is how the duplicate handling a slow handler causes shows up in a run. A Socket Mode
envelope is always held to Slack's three seconds. A pushed request is signed with the simulated moment it is sent at
(`X-Slack-Request-Timestamp`), since the agent's verifier checks it against its own clock, which is the sandbox's.

## The sandbox's clock

The agent's clock is the sandbox's, and Minutehand moves it. The sandbox is born at the scenario's `starts_at`: the
agent's command is handed `MINUTEHAND_STARTS_AT_NS` and gives it to `runsc --sim-clock-at`, so the agent's first
instruction reads simulated time; from there the clock moves forward with every jump of the run's, and never back,
so a sleep, a backoff, a scheduler loop's next check and a date the agent reads all follow simulated time with no
change to the agent. The real time the agent spends in a wake passes on its clock, and the run's clock catches up to
it when the wake ends. Between wakes Minutehand reads only whether the sandbox is idle and what its clock says.

No deadline a task in the sandbox waits for is a wake. A timer the agent armed fires when the clock passes it for
something else (a reply, a pushed event, a booking), and a runtime's own (an HTTP server's poll, a JVM's keepalive, an
emulator's event loop) never moves the clock. Work the agent means to come back to by itself is a booking with a
scheduler it calls (Cloud Tasks, EventBridge Scheduler, Slack's `chat.scheduleMessage`): a record in the run's log,
delivered at its moment (`planned_wakes`, `dispatch.source = booked`), moved or cancelled through the scheduler's API
with each change kept, and carried into a fork from any checkpoint. Through 0.3.4 the earliest deadline in the sandbox longer than `sandbox.housekeeping` was the
agent's next wake; it could not tell the agent's timer from a runtime's, it saw one deadline and never the schedule
behind it, and nothing recorded a change to it, so it is gone. An agent file that still names
`sandbox.housekeeping` is refused, saying so.

### Due records

An agent whose own scheduler polls its records for what has come due (a job table's run-at column, a `next_check`
field) books nothing. It keeps a projection of those records in its memory, and the agent file says where:

```yaml
due:
  - collection: due              # the store collection (default `default`)
    prefix: "jobs/"              # the keys under it
    where: {state: open}         # equality filters, a field or a dotted path, as `store.query`
    field: run_at                # an ISO 8601 instant with an offset; absent or null is not due
    poll: PT30S                  # how long its poller may take to find it (default PT0S)
```

- **Read at every decision.** Whenever the run decides its next jump, beside people's moves, replies and bookings, it
  reads every declared record from the memory as it stands then (in a fork, the parent's up to the checkpoint and
  the fork's own after), never from an earlier read. A record moved earlier, moved later, cleared or closed since is
  followed as it now stands. The earliest record's instant, and `poll` after it, is next due: `planned_wakes`,
  `dispatch.source = recorded`, its `ref` naming each key and instant. The wake is the clock reaching it; Minutehand
  calls nothing, and the agent's own poller finds the work when its own clock passes it.
- **Once for one instant.** A record wakes the agent at most once while it holds one instant. Woken and still holding
  that instant when the wake ends, it is not woken again, and the check `due_records` says the agent left it due
  (`review`): in production a poller whose wait and whose query disagree wakes for it forever with nothing to do. A
  new instant is new. What has woken is read from the run's log, so a fork knows what its parent was woken for.
- **A field that is not an instant** with an offset (a date alone, a time without one, a number) is not due, and
  `due_records` names the key and what it held.
- **The price.** The agent's scheduler reads its own database; the projection is a second copy it writes for the run
  to read, and in production it goes to the backend `store` was configured with. Several processes mirroring one
  record after their own commits can send their writes out of order, so a due record is written with a version
  (`store.put(key, value, version=n)`, below): the newer stays, whichever arrives last.

The agent checks the proxy's certificates against its own clock, so the
proxy's CA and host certificates are valid at every instant (`sandbox/README.md` for the sandbox itself).

## The sandbox's network

The proxy is the sandbox's only way out (`sandbox/seal.sh`): every connection it opens lands on the proxy's
transparent listener and every name it looks up resolves to the proxy, whatever its clients do with `HTTPS_PROXY`;
nothing is forwarded. What goes out for real is what the proxy tunnels: the model hosts (`--model-host`; the public
providers by default), the designated endpoints a run reaches. Everything else is answered by a fake or refused, and
recorded either way.

## The agent's memory

```python
from minutehand.agent import store

store.configure(store.SqliteBackend("agent.db"))  # once, at start: production's backend, ignored under Minutehand

store.put("asks/sam", {"status": "asked", "expected_by": "2026-09-03T09:00:00+00:00"})
store.get("asks/sam")  # None when there is none, or get(key, default)
store.list("asks/")  # [(key, value), ...] ordered by key
store.query("asks/", where={"status": "asked"})  # a field may be a dotted path: "venue.city"
store.delete("asks/sam")
store.put("jobs/7", {"run_at": "2026-09-03T09:00:00+00:00"}, version=41)  # True when it landed
with store.batch() as b:  # all or none; `async with` too
    b.put("asks/sam", {"status": "confirmed"})
    b.delete("asks/rosa")
store.collection("jobs").put("1", {...})  # a namespace of its own keys
await store.aget("asks/sam")  # aput, adelete, alist, aquery
```

**In production** (MINUTEHAND_ON unset) `store` passes every call to the backend `configure` named and records
nothing. **Under Minutehand** (`minutehand run` and `minutehand env` set MINUTEHAND_ON and MINUTEHAND_AGENT_URL) it
goes to the run: every write is the agent's in the run's log, every read is answered from the run, each run starts
from the scenario's `memory:` and each fork from its parent's memory at the checkpoint, and the configured backend
is never called. If the run cannot be reached the call raises `MinutehandUnreachable`; it never falls back to the
agent's own database. The package imports nothing but the standard library.

**A versioned write** (`put`, `delete`, a batch's `put` and `delete`, each with `version=`) lands only when no version
the key holds is newer; an equal one lands. A version is a JSON integer (a row's version, or its update time in
microseconds); an ISO 8601 instant or any other text is refused, since two writes in one millisecond would tie. A
delete keeps its version, so an older put arriving later does not bring the key back. `put` and `delete` answer
whether they landed, and a batch's `applied` says it write by write. A write with no version always lands, and leaves
the key's version as it was.

**What a fork brings back.** The world as of the checkpoint, from the log it shares with its parent; the agent's
memory, the same way; and the agent's sandbox, by playback: a fresh sandbox is started at the run's start, and the
agent's recorded inputs are played into it wake by wake up to the checkpoint, its calls answered from the record and
nothing written. At the checkpoint the agent stands where it stood, its own files, processes and timers included. A
fork is refused where the agent does not do what it did before (a call the record does not hold next, naming it): a
changed program, or something the agent draws from outside the record (a random number, a thread's timing). What
Minutehand reports besides: the store's reads and writes per wake (`WakeRecord.memory_reads`, `memory_writes`), and a
database the agent file names under `own_databases`, handed fresh and empty to every run and fork.

### Writing an adapter

A production backend is three methods over JSON text (`minutehand.agent.store.Backend`); the value is already
serialised, so an adapter stores and returns the string unchanged:

```python
class FirestoreBackend:
    def get(self, collection: str, key: str) -> str | None: ...  # the value, or None
    def scan(self, collection: str, prefix: str) -> list[tuple[str, str]]: ...  # (key, value) by key, under prefix
    def write(self, writes: Sequence[store.Write]) -> list[bool]: ...  # store.Put / store.Delete, all or none


store.configure(FirestoreBackend(...))
```

`write` must apply the whole list or none of it (a transaction, or a batched commit), and answer, write by write,
whether it landed. A write carrying a `version` lands only when no version the key holds is newer
(`store.lands(held, version)`), decided in the same transaction, and a delete keeps its version, so the adapter keeps
each key's version beside its value. An adapter that keeps no versions refuses a write that carries one rather than
land it unchecked. This changed in 0.3.6: an adapter whose `write` answers None is refused by name. `SqliteBackend`
and `MemoryBackend` in `src/minutehand/agent/_store.py` are the two shipped and the pattern to copy.

### The wire, for an agent not written in Python

`POST $MINUTEHAND_AGENT_URL/store` with JSON (`domain/memory.py`), straight to the receiver (its host is in the
agent's `NO_PROXY`); `collection` defaults to `default`:

| Body | Answer |
|---|---|
| `{"op": "get", "collection": "default", "key": "asks/sam"}` | `{"found": true, "value": …}` or `{"found": false, "value": null}` |
| `{"op": "list", "collection": "default", "prefix": "asks/"}` | `{"items": [{"key": "asks/sam", "value": …}, …]}`, ordered by key |
| `{"op": "write", "writes": [{"op": "put", "key": "k", "value": …, "version": 8}, {"op": "delete", "key": "j"}]}` | `{"seq": n, "applied": [true, false]}`: the last entry's seq in the run's log (its head when none landed), and for each write whether it landed. `version`, on a put or a delete, is optional and a JSON integer |

A body the receiver cannot read is answered 400 saying why.

## Judging a run

| Field | In | What |
|---|---|---|
| `assess` | agent file | The team's rules, for every scenario it runs (`docs/assessments.md`) |
| `assess` | scenario | Rules for this situation; one with the id of an agent file's rule replaces it |
| `assess_off` | scenario | Ids of the agent file's rules this scenario does not judge by; an id no rule has is refused |
| `fail_on_integrity` | agent file, scenario | Integrity facts that fail the run when found: `around_proxy`, `agent_contract_changed`, `unmatched_call` (the scenario's are added to the agent file's). Unnamed, each is stated as `review` and never changes the verdict |
| `expect` | scenario | What must be true of the world at the end |
| `expect_outcome` | scenario | The verdict the scenario is written to reach: `passed` (default), `failed`, `unfinished`, `not_judged`, `simulation_incomplete`. Read by `minutehand run-all`, which exits 1 when a run's verdict differs |
| `checks` | agent file | Python checks of the team's own (above) |
| `instructions` | agent file | The files that state the agent's work (`- file: prompt.md`; `${NAME}` or `${NAME:-default}` reads Minutehand's environment), read when the run starts and kept with it. The reviewers judge every effect against them; unnamed, against the system prompts the agent gave its model. Name them when the agent's model does one part of its work only (`docs/assessments.md`) |

`RunResult.assessed_by` records what judged a run: each rule's id, `expectations`, `near_miss_name` and each of the
agent's own checks. A rule may not take the id of a check.

## The wake limit

A run stops after its wake limit. The scenario's `max_wakes` sets it. Without one, a scenario with a window or a
deadline and an agent with a rhythm of its own (`tick` in the agent file: how often its bookings or due records bring it
back at most, `tick: PT1H`) get every tick across it and 20 more, for the wakes replies and happenings bring; a window
with no rhythm declared gets one wake a simulated minute across it, a guard against an agent waking itself without
end. Otherwise the limit is 20. A run that stops there says the limit and where it came from.

## Types the agent may generate

`schemas/agent-api.openapi.json` (`minutehand schema agent-api`) is an OpenAPI 3.1 document. It is generated from the
models that cross the endpoints and committed with a drift test (`tests/test_schemas.py`). It covers:

- `deliverReply`, the default shape of a reply delivered to a declared host's webhook;
- `listPending` and `decide`, the default shape of an inbox.

None of these exists for Minutehand: each is a place the agent's own product already has, which the agent file
points at. An agent's team can generate types from it in any language. Every operation is optional, and the paths in
it are suggestions: the agent file names each URL.

**A press has no default shape.** Only a provider delivers presses, in its own wire format.

## Validating a file without running it

| Tool | What it does |
|---|---|
| `minutehand schema agent\|scenario\|seed` | Prints the JSON Schema (2020-12) of each kind of file, generated by Pydantic and committed under `schemas/`. |
| An editor header | A first line of `# yaml-language-server: $schema=https://raw.githubusercontent.com/Alknoma/minutehand/main/schemas/agent.schema.json` (or `scenario`, `seed`) gives completion and inline errors in any editor using the YAML language server. |
| `minutehand validate <file>…` | Loads each file with every load-time check, resolves each inbox operation in its document, and reads every rule of `assess` (an anchor its `each` lacks, no bound, a pattern there is not); given an agent file with scenarios, also the rules each scenario would be judged by. It prints each problem with its place (`inboxes[0].pending.id: …`) and exits 1 when there is any. It warns, without failing, of a declared service's responder who never acts on its items (declared silent, or a script that ends in silence with nothing pinned). |
| `version: 1` | An agent file may name the version it was written for. A file naming a later version is refused, saying so. Absent means the current version. |

An agent file written for the kinds of wake that are gone (`wakes:` with `reported`, `marked`, `polled`, `command`
or `contained`) is refused by name: every agent runs in a sandbox, and nothing is asked of it.

## Using the agent's own API description

A declared call **on** the agent can name an operation of an OpenAPI document instead of writing a request out:

```yaml
request: {kind: operation, document: openapi.yaml, operation: listApprovals,
          parameters: {approver: "{person.email}"}}
```

What Minutehand does with it:

- **Before a run** it reads the document and finds the operation. It refuses, naming each, an operation that is
  not there, a parameter the operation does not have, a required parameter not given, and a body for an operation
  that takes none.
- **On each call** it takes the method, path and parameter locations from the document.
- **On each answer** it checks the answer against the schema the document gives that status. A mismatch is the
  agent's contract having changed, named by field: "the agent's contract changed: listApprovals (openapi.yaml)
  answered 200 with what its API description does not allow: $.items[0].summary: 21 is not of type 'string'". That
  is the check `agent_contract_changed`, stated for review, and a failure only when the agent file or the scenario
  names it in `fail_on_integrity`.

It is built for inboxes only. How the other declarations would take it up:

| Declaration | How it would take `operation` |
|---|---|
| `outbound[].replies` | `request: {kind: operation, …}` in place of `url`, `method`, `headers` and `body`. Signing stays as it is. |
| `inbound[]` | Not applicable. Those are the provider's wire formats, not the agent's. |

## One template syntax, one path syntax

New declarations (inboxes) use the following.

**Placeholders**, `{namespace.name}` (`domain/templates.py`):

| Namespace | Names |
|---|---|
| `person` | `key`, `email`, `name`, `credential` |
| `item` | `id` |
| `input` | the decision's inputs, by name |
| `clock` | `now` |
| `page` | `cursor` |
| `run` | `port`, `dir`: anywhere in the agent file and the agent's command under `minutehand run-all` (a free port and a folder of the scenario's own, so runs in parallel share neither) and `minutehand run` with a command (filled once); also `MINUTEHAND_RUN_PORT` and `MINUTEHAND_RUN_DIR` in the agent's environment. Refused by name where no agent is started (`env`, `doctor`, `run` with no command) |
| `case` | reserved |
| `person`, `ask`, `rule` | in an assessment's `message` and `holding`: `{person.key}`, `{person.name}`, `{ask.at}`, `{ask.answer}`, `{rule.id}`, `{rule.count}`, `{rule.moment}`, and in `holding` `{ask.facts}` (`docs/assessments.md`) |
| `team` | a team's values in a library scenario: `goal`, `owner_key`, `owner_name`, `owner_email`, the same for `ask` and `other`, `answer`, `tell`, `credential_env`, `provider`, `wakes` (`docs/scenarios.md`) |

**Paths** are JSONPath (RFC 9535), in the subset `domain/jsonpath.py` reads: names, indexes, wildcards, several
selectors in one bracket, and descendants. Filters and slices are refused at load.

What existed before inboxes, and stays as it is for now:

| Syntax | Where |
|---|---|
| `{reply_id}` `{from}` `{from_name}` `{to}` `{subject}` `{text}` `{in_reply_to}` `{sent_at}` | `outbound[].replies.body` |
| `{message_id}` | `outbound[].answer` (acknowledged answers) |
| `{hex}` `{base64}` `{timestamp}` | `outbound[].replies.signing.format` |
| `{port}` | `emulators[].upstream.url`, `.command`, `.env` |
| `{nanoseconds}` | `sandbox.advance` |
| `{key}` | `Manifest.world_keys` (provider code, not files) |
| `{{start+P2D}}` `{{start+P2D:iso}}` `{{start+P2D:time}}` | Any text of a scenario (`DATED`) |
| Dotted paths with `[n]`, `[*]`, no `$` (`personalizations[*].to[*].email`) | `outbound[].message`, `.redact`, `.ignore_body`, `.replies.thread` (`adapters/proxy/capture.values_at`), and `emulators[].errors[].at` (its own `BodyPath`, read by `adapters/emulator/answers.py`). Each is a clean subset of JSONPath without the `$`. |

## Known debt

Each line is a name or shape that disagrees with this page. None is renamed in this change.

1. **`inbound[]` is the provider's push, which this page calls "deliver".** `InboundTarget`, `inbound.py` and
   `PushesEvents.deliver` mix the two words.
2. **`outbound[].replies` is "deliver a reply" to a declared host.** `ReplyDelivery` and `TakesReplies` name it
   three ways.
3. **The older placeholders listed above** (`{reply_id}`, `{message_id}`, `{port}`, `{hex}`) should move to
   `{reply.id}`, `{message.id}`, `{emulator.port}` and `{signature.hex}`.
4. **The dotted path syntax and its two readers** (`capture.values_at`, the emulator's) should become
   `domain/jsonpath.py`, with paths written `$.personalizations[*].to[*].email`.
5. **`BodyPath` is declared twice**, in `domain/outbound.py` and `domain/emulator.py`.
6. **`Scripted.replies[].to_ask` counts asks, while `Take.nth` counts items in a provider, or asks when it names
   none.** Both are "the nth ask".
7. **`PushSnapshot` is named for the provider's word.** Every send through the door is a delivery, and the entity
   kind is still `push`.
