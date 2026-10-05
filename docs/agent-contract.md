# The agent contract

This page lists every way Minutehand and an agent's own repository touch, with one term for each. Field names, docs,
command output and errors use these terms. Where a name in the code says otherwise, it is listed under "Known debt"
at the end and has not been renamed.

**Almost nothing is required.** An agent that takes its goal by message and books its own wakes implements none of
the endpoints below.

## The terms

| Term | Means | Never call it |
|---|---|---|
| **agent file** | The YAML or JSON file an agent's repository keeps: `AgentUnderTest` | config, manifest |
| **scenario**, **seed** | The situation a run plays (`Scenario`), or a standing world opens with (`Seed`) | fixture |
| **wake** | Minutehand telling the agent it is now `now`, and to go | tick (except a `Polled` one), trigger |
| **report** | The agent's answer: still working, done, when it next needs a wake | status call |
| **deliver** | Minutehand handing the agent what a person did: a reply, a press, a happening | push (that is the provider's word) |
| **inbox** | Where work waits on a person in the agent's own product | queue, human action |
| **item** | One thing waiting in an inbox | task, request |
| **decide** | A person settling an item with a decision and its inputs | approve (that is one decision) |
| **declared host** | A host the agent calls that Minutehand captures: acknowledge, pass through, replay, forward | mock |
| **operation** | An `operationId` in an OpenAPI document that a declaration names instead of a template | endpoint |

## Touch points

| Touch point | Who calls whom, when | Request → response | Declared by | Required |
|---|---|---|---|---|
| **wake** | Minutehand → agent, each moment something is due | `WakeRequest` → any 2xx | `wakes[].wake_url` (`reported`, `polled`); `command` (stdin) | Only to take the goal or wakes this way |
| **report** | Minutehand → agent, polled after a wake and while settling | none → `AgentReport` | `wakes[].report_url` | Only for `reported` |
| **deliver a reply** (provider) | Minutehand → agent, when a person's reply falls due | The provider's own event (Slack Events API, Bot Framework activity) → 2xx | `inbound[]` | Only for a provider that pushes |
| **deliver a press** (provider) | Minutehand → agent, when a person uses a control | The provider's own interactivity payload → 2xx | `inbound[].interactivity_url` | Only for controls |
| **deliver a reply** (declared host) | Minutehand → agent, an answer to a captured send | `DeliveredReply` (the default shape, no `body`), or the declared `body` → 2xx | `outbound[].replies` | Only when people answer a send |
| **inbox list** | Minutehand → agent, as each person, after each wake or step and before the clock moves | Declared template or operation (default `listPending`, `PendingPage`) | `inboxes[].pending` | Only with an inbox |
| **decide** | Minutehand → agent, as the person, when a decision falls due | Declared template or operation (default `decide`, `DecisionMade`) → declared success | `inboxes[].decisions[]` | Only with an inbox |
| **busy** | Minutehand runs the agent's command while settling | Exit 0 busy, 1 idle | `state.busy` | No |
| **fingerprint** | Minutehand runs the agent's command at each checkpoint and after a restore | Last line of output is the digest | `state.fingerprint` | No |
| **snapshot**, **restore** | Minutehand runs the agent's commands at each checkpoint and before a fork | `MINUTEHAND_SNAPSHOT_DIR` | `state.snapshot`, `state.restore` (`stop`, `start`) | Only to fork |
| **declared hosts** | The agent → a host no provider claims | Captured as declared | `outbound[]` (`acknowledge`, `pass_through`, `replay`, `forward`) | No |
| **emulators** | Minutehand → a fake outside it, for `forward` hosts | The emulator's own | `emulators[]` | No |
| **base URLs** | The agent → the proxy's `/_host/…`, for a client without a proxy | As the real host | `base_urls[]` | No |
| **model hosts** | The agent → its model API, tunnelled or recorded | As the real host | `--model-host`, `CreateWorld.model_hosts` | No |
| **telemetry** | The agent → Minutehand's OTLP receiver | OTLP/HTTP or gRPC | Environment Minutehand hands out | No |

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
| An editor header | A first line of `# yaml-language-server: $schema=https://raw.githubusercontent.com/Alknoma/minutehand/integration-main/schemas/agent.schema.json` (or `scenario`, `seed`) gives completion and inline errors in any editor using the YAML language server. |
| `minutehand validate <file>…` | Loads each file with every load-time check, and resolves each inbox operation in its document. It prints each problem with its place (`inboxes[0].pending.id: …`) and exits 1 when there is any. |
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
| `run`, `case` | reserved |

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
8. **`StateHooks` is "state"**, while its commands are snapshot, restore, busy and fingerprint.
9. **Only the agent file is versioned.** A scenario and a seed carry no version.
10. **`docs/design.md` still calls a wake source "how it comes back to work"**, and uses "monitor" for Minutehand.
