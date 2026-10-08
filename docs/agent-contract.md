# The agent contract

This page lists every way Minutehand and an agent's own repository touch, with one term for each. Field names, docs,
command output and errors use these terms. Where a name in the code says otherwise, it is listed under "Known debt"
at the end and has not been renamed.

**Almost nothing is required.** An agent that takes its goal by message and books its own wakes implements none of
the endpoints below. An agent that remembers anything across wakes keeps it through one import, `minutehand.agent`
("The agent's memory and its next wake", below): that is its whole contact with Minutehand in its own code.

## The terms

| Term | Means | Never call it |
|---|---|---|
| **agent file** | The YAML or JSON file an agent's repository keeps: `AgentUnderTest` | config, manifest |
| **scenario**, **seed** | The situation a run plays (`Scenario`), or a standing world opens with (`Seed`) | fixture |
| **wake** | Minutehand telling the agent it is now `now`, and to go | tick (except a `Polled` one), trigger |
| **report** | The agent's answer: still working, done, when it next needs a wake | status call |
| **memory** | What the agent remembers across wakes, through `minutehand.agent.store`: the run's own under Minutehand | state, snapshot |
| **mark** | The agent saying when it next wants to be woken, through `minutehand.agent.wake` | schedule (that is its own scheduler's) |
| **deliver** | Minutehand handing the agent what a person did: a reply, a press, a happening | push (that is the provider's word) |
| **inbox** | Where work waits on a person in the agent's own product | queue, human action |
| **item** | One thing waiting in an inbox | task, request |
| **decide** | A person settling an item with a decision and its inputs | approve (that is one decision) |
| **declared host** | A host the agent calls that Minutehand captures: acknowledge, pass through, replay, forward | mock |
| **operation** | An `operationId` in an OpenAPI document that a declaration names instead of a template | endpoint |

## Touch points

| Touch point | Who calls whom, when | Request → response | Declared by | Required |
|---|---|---|---|---|
| **wake** | Minutehand → agent, each moment something is due | `WakeRequest` → any 2xx | `wakes[].wake_url` (`reported`, `marked`, `polled`); `command` (stdin) | Only to take the goal or wakes this way |
| **report** | Minutehand → agent, polled after a wake, and at the start of a fork to prove the agent is the checkpoint's | none → `AgentReport` | `wakes[].report_url` | Only for `reported` |
| **memory** | The agent → Minutehand's receiver, each time it reads or writes what it remembers | `minutehand.agent.store` (`POST $MINUTEHAND_AGENT_URL/store`) | Nothing: MINUTEHAND_ON and MINUTEHAND_AGENT_URL are handed out | Only for an agent that remembers across wakes and is forked |
| **mark** | The agent → Minutehand's receiver, when it knows its next wake | `minutehand.agent.wake` (`POST $MINUTEHAND_AGENT_URL/wake`) | `wakes[]` of kind `marked` takes nothing else | No: a report's `next_wake` does the same |
| **own database** | Minutehand → the agent's environment, before each run and fork | A fresh empty SQLite file, its path or `sqlite:///` URL | `own_databases[]` (`env`, `form`) | No |
| **deliver a reply** (provider) | Minutehand → agent, when a person's reply falls due | The provider's own event (Slack Events API, Bot Framework activity) → 2xx | `inbound[]` | Only for a provider that pushes |
| **socket** (Slack) | The agent → Slack's `apps.connections.open`, then a WebSocket it holds open; replies arrive on it | `events_api` envelopes → the agent's `{"envelope_id": …}` | `inbound[]` with `delivery: socket_mode` and no `url` | Instead of a request URL |
| **deliver a press** (provider) | Minutehand → agent, when a person uses a control | The provider's own interactivity payload → 2xx | `inbound[].interactivity_url` | Only for controls |
| **deliver a reply** (declared host) | Minutehand → agent, an answer to a captured send | `DeliveredReply` (the default shape, no `body`), or the declared `body` → 2xx | `outbound[].replies` | Only when people answer a send |
| **inbox list** | Minutehand → agent, as each person, after each wake or step and before the clock moves | Declared template or operation (default `listPending`, `PendingPage`) | `inboxes[].pending` | Only with an inbox |
| **decide** | Minutehand → agent, as the person, when a decision falls due | Declared template or operation (default `decide`, `DecisionMade`) → declared success | `inboxes[].decisions[]` | Only with an inbox |
| **declared hosts** | The agent → a host no provider claims | Captured as declared | `outbound[]` (`acknowledge`, `pass_through`, `replay`, `forward`) | No |
| **emulators** | Minutehand → a fake outside it, for `forward` hosts | The emulator's own | `emulators[]` | No |
| **base URLs** | The agent → the proxy's `/_host/…`, for a client without a proxy | As the real host | `base_urls[]` | No |
| **model hosts** | The agent → its model API, tunnelled or recorded | As the real host | `--model-host`, `CreateWorld.model_hosts` | No |
| **telemetry** | The agent → Minutehand's OTLP receiver | OTLP/HTTP or gRPC | Environment Minutehand hands out | No |
| **assessments** | Minutehand reads the team's rules over the facts of every run and fork; nothing else judges how the agent behaves | YAML rules (`docs/assessments.md`) → findings named by each rule's `id` | `assess[]` in the agent file; `assess[]` and `assess_off[]` in a scenario | No: a run with none is reported as facts, `Not assessed` |
| **own checks** | Minutehand runs the agent's checks after every run and fork: the escape hatch for what a rule cannot say | A class with `id`, `needs` and `run(view) -> CheckReport`, reading the facts `minutehand.checks.facts` gives (`asks`, `messages`, `writes`, `planned_wakes`, `reported`) | `checks[]`: Python files, a relative path read from the agent file's folder | No |

## The agent's memory and its next wake

```python
from minutehand.agent import store, wake

store.configure(store.SqliteBackend("agent.db"))  # once, at start: production's backend, ignored under Minutehand

store.put("asks/sam", {"status": "asked", "expected_by": "2026-09-03T09:00:00+00:00"})
store.get("asks/sam")  # None when there is none, or get(key, default)
store.list("asks/")  # [(key, value), ...] ordered by key
store.query("asks/", where={"status": "asked"})  # a field may be a dotted path: "venue.city"
store.delete("asks/sam")
with store.batch() as b:  # all or none; `async with` too
    b.put("asks/sam", {"status": "confirmed"})
    b.delete("asks/rosa")
store.collection("jobs").put("1", {...})  # a namespace of its own keys
await store.aget("asks/sam")  # aput, adelete, alist, aquery

wake.at(expected_by)  # the next wake, an aware moment; wake.clear() for none
```

**In production** (MINUTEHAND_ON unset) `store` passes every call to the backend `configure` named and records
nothing, and `wake` does nothing. **Under Minutehand** (`minutehand run` and `minutehand env` set MINUTEHAND_ON and
MINUTEHAND_AGENT_URL) both go to the run: every write is the agent's in the run's log, every read is answered from
the run, each run starts from the scenario's `memory:` and each fork from its parent's memory at the checkpoint, and
the configured backend is never called. If the run cannot be reached the call raises `MinutehandUnreachable`; it
never falls back to the agent's own database. The package imports nothing but the standard library.

**What is not part of a run.** Only state written through the store. Whatever the agent writes anywhere else (its
own database, files, a cache, a variable in a process that outlives a wake) is not simulated, not kept apart between
runs, and not rewound by a fork, so its later calls can depend on state from another moment or another run. What
Minutehand detects: the store's reads and writes per wake (`WakeRecord.memory_reads`, `memory_writes`); a database the
agent file names under `own_databases`, handed fresh and empty to every run and fork and noted as outside forks; and
a fork whose agent's report differs from the one recorded at its checkpoint, refused, naming state outside the store
as the likely cause.

### Writing an adapter

A production backend is three methods over JSON text (`minutehand.agent.store.Backend`); the value is already
serialised, so an adapter stores and returns the string unchanged:

```python
class FirestoreBackend:
    def get(self, collection: str, key: str) -> str | None: ...  # the value, or None
    def scan(self, collection: str, prefix: str) -> list[tuple[str, str]]: ...  # (key, value) by key, under prefix
    def write(self, writes: Sequence[store.Write]) -> None: ...  # store.Put / store.Delete, all or none


store.configure(FirestoreBackend(...))
```

`write` must apply the whole list or none of it (a transaction, or a batched commit). `SqliteBackend` and
`MemoryBackend` in `src/minutehand/agent/_store.py` are the two shipped and the pattern to copy.

### The wire, for an agent not written in Python

`POST $MINUTEHAND_AGENT_URL/store` with JSON (`domain/memory.py`), straight to the receiver (its host is in the
agent's `NO_PROXY`); `collection` defaults to `default`:

| Body | Answer |
|---|---|
| `{"op": "get", "collection": "default", "key": "asks/sam"}` | `{"found": true, "value": …}` or `{"found": false, "value": null}` |
| `{"op": "list", "collection": "default", "prefix": "asks/"}` | `{"items": [{"key": "asks/sam", "value": …}, …]}`, ordered by key |
| `{"op": "write", "writes": [{"op": "put", "key": "k", "value": …}, {"op": "delete", "key": "j"}]}` | `{"seq": n}`, the last entry's seq in the run's log |

`POST $MINUTEHAND_AGENT_URL/wake` with `{"at": "2026-09-03T09:00:00+00:00"}` or `{"at": null}` answers 204. A body
the receiver cannot read is answered 400 saying why. `examples/recipes/vercel_ai_sdk/minutehand-store.ts` is a client
in TypeScript.

## Judging a run

| Field | In | What |
|---|---|---|
| `assess` | agent file | The team's rules, for every scenario it runs (`docs/assessments.md`) |
| `assess` | scenario | Rules for this situation; one with the id of an agent file's rule replaces it |
| `assess_off` | scenario | Ids of the agent file's rules this scenario does not judge by; an id no rule has is refused |
| `expect` | scenario | What must be true of the world at the end |
| `expect_outcome` | scenario | The verdict the scenario is written to reach: `passed` (default), `failed`, `unfinished`, `not_judged`. Read by `minutehand run-all`, which exits 1 when a run's verdict differs |
| `checks` | agent file | Python checks of the team's own (above) |

`RunResult.assessed_by` records what judged a run: each rule's id, `expectations`, `near_miss_name` and each of the
agent's own checks. A rule may not take the id of a check.

## The wake limit

A run stops after its wake limit. The scenario's `max_wakes` sets it. Without one, a scenario with a deadline and an
agent with a rhythm of its own get every tick up to the deadline and 20 more, for the wakes replies and happenings
bring; the rhythm is a polled wake's `every`, or `tick` in the agent file for an agent that reports or books its own
next wake (`tick: PT1H`). Otherwise the limit is 20. A run that stops there says the limit and where it came from.

## Types the agent may generate

`schemas/agent-api.openapi.json` (`minutehand schema agent-api`) is an OpenAPI 3.1 document. It is generated from the
models that cross the endpoints and committed with a drift test (`tests/test_schemas.py`). It covers:

- `wake` and `report`;
- `deliverReply`, the default shape of a reply delivered to a declared host's webhook;
- `listPending` and `decide`, the default shape of an inbox.

An agent's team can generate types from it in any language. Every operation is optional, and the paths in it are
suggestions: the agent file names each URL.

**A press has no default shape.** Only a provider delivers presses, in its own wire format.

## Validating a file without running it

| Tool | What it does |
|---|---|
| `minutehand schema agent\|scenario\|seed` | Prints the JSON Schema (2020-12) of each kind of file, generated by Pydantic and committed under `schemas/`. |
| An editor header | A first line of `# yaml-language-server: $schema=https://raw.githubusercontent.com/Alknoma/minutehand/main/schemas/agent.schema.json` (or `scenario`, `seed`) gives completion and inline errors in any editor using the YAML language server. |
| `minutehand validate <file>…` | Loads each file with every load-time check, resolves each inbox operation in its document, and reads every rule of `assess` (an anchor its `each` lacks, no bound, a pattern there is not); given an agent file with scenarios, also the rules each scenario would be judged by. It prints each problem with its place (`inboxes[0].pending.id: …`) and exits 1 when there is any. |
| `version: 1` | An agent file may name the version it was written for. A file naming a later version is refused, saying so. Absent means the current version. |

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
  is the check `agent_contract_changed`.

It is built for inboxes only. How the other declarations would take it up:

| Declaration | How it would take `operation` |
|---|---|
| `outbound[].replies` | `request: {kind: operation, …}` in place of `url`, `method`, `headers` and `body`. Signing stays as it is. |
| `wakes[].wake_url`, `report_url` | `wake: {document, operation}` and `report: {document, operation}`. The report's answer would be checked against the document as well as against `AgentReport`. |
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
| `run` | `port`, `dir`: anywhere in the agent file and the agent's command under `minutehand run-all`, a free port and a folder of the scenario's own, so runs in parallel share neither; also `MINUTEHAND_RUN_PORT` and `MINUTEHAND_RUN_DIR` in the agent's environment |
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
| `{key}` | `Manifest.world_keys` (provider code, not files) |
| `{{start+P2D}}` `{{start+P2D:iso}}` `{{start+P2D:time}}` | Any text of a scenario (`DATED`) |
| Dotted paths with `[n]`, `[*]`, no `$` (`personalizations[*].to[*].email`) | `outbound[].message`, `.redact`, `.ignore_body`, `.replies.thread` (`adapters/proxy/capture.values_at`), and `emulators[].errors[].at` (its own `BodyPath`, read by `adapters/emulator/answers.py`). Each is a clean subset of JSONPath without the `$`. |

## Known debt

Each line is a name or shape that disagrees with this page. None is renamed in this change.

1. **`wakes[].kind: reported` is the term "report".** The source is named after the endpoint it adds, not after the
   wake.
2. **`inbound[]` is the provider's push, which this page calls "deliver".** `InboundTarget`, `inbound.py` and
   `PushesEvents.deliver` mix the two words.
3. **`outbound[].replies` is "deliver a reply" to a declared host.** `ReplyDelivery` and `TakesReplies` name it
   three ways.
4. **The older placeholders listed above** (`{reply_id}`, `{message_id}`, `{port}`, `{hex}`) should move to
   `{reply.id}`, `{message.id}`, `{emulator.port}` and `{signature.hex}`.
5. **The dotted path syntax and its two readers** (`capture.values_at`, the emulator's) should become
   `domain/jsonpath.py`, with paths written `$.personalizations[*].to[*].email`.
6. **`BodyPath` is declared twice**, in `domain/outbound.py` and `domain/emulator.py`.
7. **`Scripted.replies[].to_ask` counts messages, while `decisions[].to_item` counts items.** Both are "the nth
   ask".
8. **Only the agent file is versioned.** A scenario and a seed carry no version.
9. **`application/restore.py` and `restore.json` say "restore"** for a fork's start, where nothing is restored: the
   memory is read from the log, and the agent's report compared.
10. **`--telemetry-port` names the receiver's port,** which holds the agent's memory as well as its telemetry.
11. **`docs/design.md` still calls a wake source "how it comes back to work"**, and uses "monitor" for Minutehand.
