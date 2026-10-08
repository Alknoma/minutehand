# Inboxes: work waiting on a person in the agent's own product

Some of what a person does never touches a SaaS fake: approving an operation in the agent's own web app, answering
a question it raised on its own page. Minutehand reaches these through an **inbox** the agent file declares. It
reads what waits on each person, as that person, and records each new item as the agent asking them. The person's
replier decides, after their usual delay. Minutehand then makes the decision as that person, by the declared call,
and the ledger and the checks score the result the way they score a chat question.

Built and tested:

| Where | What |
|---|---|
| Declaration | `src/minutehand/domain/inboxes.py` |
| Calls to the agent | `adapters/agent/inboxes.py`, `adapters/agent/openapi.py` |
| Logic | `application/inboxes.py`, the run loop, `application/standing.py` |
| Facts and check | `checks/facts.py` (`writes` with `gated`, read by a team's rule), `checks/agent_contract_changed.py` |
| Tests | `tests/inboxes/`, `tests/architecture/test_approvals.py`, `tests/architecture/test_driven_approvals.py`, `tests/web/test_viewer_decisions.py` |

## The shape, and where it comes from

Frameworks that hold work for a human agree on two operations:

- **List** what is pending for a person. Each item has an id, who it waits on, a summary in words, the decisions
  allowed and what each one takes.
- **Decide** one item, by its id, as that person, with a choice and optional input.

How each framework does it, as of 2026-10-05:

- **MCP elicitation.** The server sends `message` and a flat `requestedSchema`. The answer is `accept`, `decline` or
  `cancel`, with `content`. It is pushed to a connected client, so it cannot be listed later.
  <https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation>
- **LangGraph.** An interrupt has an `id` and a free-form `value`. Threads with `status: interrupted` are listed by
  `POST /threads/search`. The run is resumed with `Command(resume={interrupt_id: value})`. The HITL middleware types
  this as `action_requests` and `review_configs.allowed_decisions` (approve, edit, reject, respond).
  <https://docs.langchain.com/oss/python/langgraph/interrupts>,
  <https://docs.langchain.com/oss/python/langchain/human-in-the-loop>,
  <https://docs.langchain.com/langsmith/agent-server-api/threads/search-threads>,
  <https://github.com/langchain-ai/agent-inbox>
- **OpenAI Agents SDK.** A run stops with `interruptions`, each a `ToolApprovalItem` (tool name and arguments). It
  resumes after `state.approve(item)` or `state.reject(item)`. This exists in the SDK only, with no listing.
  <https://openai.github.io/openai-agents-python/human_in_the_loop/>
- **Camunda 8 user tasks.** Tasks are found with `POST /v2/user-tasks/search`, filtered by `assignee` and `state`. A
  task is completed with `POST /v2/user-tasks/{key}/completion` and `{variables, action}`. This is the only one of
  the five with an assignee.
  <https://docs.camunda.io/docs/apis-tools/camunda-api-rest/specifications/complete-user-task>
- **A2A.** A task in state `input-required` carries its question in `status.message`. It resumes when a message is
  sent with the same `taskId`.
  <https://a2a-protocol.org/latest/specification/>

What follows from that:

- **The declaration matches that vocabulary.** It names `pending`, an item `id`, `waits_on`, `summary`, `decisions`,
  their `inputs`, and a decide request made by id as the person.
- **It goes beyond it in three places:**
  - `gates`, the id of the operation the item holds back. This lets a rule see the operation go ahead
    (`writes: {gated: true}`).
  - `permits` on a decision: true for approve, false for reject.
  - `reads`, how the record words a decision ("approved").
- **Two transports recur: HTTP and JSON, and JSON-RPC (MCP).** Only HTTP is built (`kind: http`). An MCP inbox would
  be a second `kind` beside `http`, whose list and decide are tool calls. Nothing that reads an item would change:
  the people, the ledger, the checks and the viewer read only what is seen.

## The declaration

The reference agent's own declaration (`examples/reference_agent/agent.yaml`):

```yaml
inboxes:
  - name: approvals
    kind: http
    as_person:
      headers: {Authorization: "Bearer {person.credential}"}
    pending:
      request: {kind: template, url: "http://127.0.0.1:8790/approvals?approver={person.email}"}
      items: "$.items[*]"
      id: "$.id"
      summary: "$.summary"
      decisions: "$.actions"
      gates: "$.operation"                    # the tell it holds back, which the email naming it carries
      paging: {next: "$.next", param: cursor}
    decisions:
      - name: approve
        reads: approved
        permits: true
        request:
          kind: template
          method: POST
          url: "http://127.0.0.1:8790/approvals/{item.id}/decision"
          body: {decision: approve}
      - name: reject
        reads: rejected
        permits: false
        description: Turn the booking down, saying why
        request:
          kind: template
          method: POST
          url: "http://127.0.0.1:8790/approvals/{item.id}/decision"
          body: {decision: reject, reason: "{input.reason}"}
        inputs: [{name: reason, description: Why the booking is turned down}]
```

### The parts

| Part | Says |
|---|---|
| `name` | What its items are recorded under. It may not also be a provider's key or a captured host's name. |
| `as_person.headers` | Sent on every call, filled for the person. When a header names `{person.credential}`, Minutehand can act only as people who hold a `Person.credential`. Without `as_person`, it sends no credential. |
| `pending.request` | How to list one person's items (see the request kinds below). |
| `pending.items` | Where the items are in each page. |
| `pending.id`, `.summary` | Where each item's id and summary are, relative to the item. |
| `pending.waits_on` | Makes the list everyone's. Each item names whom it waits on, by email or `Person.key`, and only that person's reading asks them. |
| `pending.category`, `.decisions`, `.gates` | Optional. A category, the decisions allowed on this item, and the id of the operation it holds back. |
| `pending.paging` | `next` is where the next page's cursor is. It is sent as `param`, or wherever the request names `{page.cursor}`. At most `most` pages are read (50). |
| `decisions[]` | `name`, `description` (a model-written person reads it), `reads`, `permits`, `inputs`, `request`, and `succeeds`. `succeeds` is the statuses that mean the decision was taken (any 2xx by default), optionally with a JSONPath `at` that must equal `equals`. |

### Request kinds

| Kind | What it is |
|---|---|
| `kind: template` | Method, URL, headers and body, written out. The body is written as structure. A form body needs `form: true`. |
| `kind: operation` | `document` (a file, an http(s) URL, or `minutehand`), `operation` (its `operationId`), and optionally `server`, `parameters` (by name), `headers` and `body`. The method, the path, where each parameter goes, and the answer's schema all come from the document. |

### Templates and paths

- **Placeholders** are `{namespace.name}`:
  - in every template: `{person.key}`, `{person.email}`, `{person.name}`, `{person.credential}`;
  - in a decision only: `{item.id}`, `{clock.now}`, `{input.<name>}`;
  - in a list that pages in place: `{page.cursor}`.
- **Paths** are JSONPath, RFC 9535 (see `docs/agent-contract.md`).
- **Refused when the file loads**, naming it:
  - any other placeholder;
  - a path that is not JSONPath, or that uses filters or slices;
  - a decision declared twice;
  - an input named but not declared;
  - a GET with a body.
- `minutehand validate agent.yaml` reports these without running anything. It also resolves each operation in its
  document.

### On the scenario's side

```yaml
  - key: nadia
    name: Nadia Ek
    email: nadia@example.com
    credential: {kind: generated, env: REFERENCE_APPROVER_TOKEN}   # or {kind: from_env, env: ...}; never stored
    reply:
      kind: scripted
      delay: {shortest: PT2H, longest: P1D}
      replies: []
      decisions:
        - {decision: approve}                                       # every item
        - {to_item: 2, decision: reject, inputs: {reason: Twice is once too many}}
```

**Each person who can receive items must say what they do with them.** That is any member or guest Minutehand can
act as, and the person's options are:

| The person is | They |
|---|---|
| `Scripted`, with `decisions` | Make the scripted decision. `to_item: n` (the nth item waiting on them, in `inbox` when it names one) wins over a decision for every item. |
| `Scripted`, with `decisions: []` | Leave every item pending. |
| `Silent` | Never decide. |
| `Answers` | Are shown the summary, each decision with its description and inputs, and pick one through the model port (`person-decision/1`). |

There is no default decision. A run is refused before anything starts when such a person is `Scripted` and says
nothing (`decisions` absent). A scripted decision that no inbox offers, or that lacks an input it requires, is
refused the same way.

## What Minutehand does with it

### `minutehand run`

**When it reads.** Minutehand reads every inbox as each person it can act as at the end of every wake, and again
before the clock moves. Each read is Minutehand's own call, recorded with `Exchange.inbox_call`. It is never one of
the agent's calls, never an outbound call, and never unmatched.

**What it records:**

| Event | Recorded as |
|---|---|
| An item first seen | The agent asking that person: `InboxItemSnapshot`, `PENDING`, actor AGENT, carrying the summary, the inbox, the item id, the decisions available and `gates`. The person's replier decides it as for a message (`PersonReply.decides`), and the decision is kept with the run, so a fork replays it. |
| A decision falling due | The declared call, made as the person. It wakes the agent as a reply does (`PERSON_REPLIED`). |
| The product takes it | `DECIDED`, by actor PERSON, with the decision, its inputs, `permits` and how it reads. |
| The product refuses it | Still `PENDING`, by actor PERSON, with the product's answer in `refused`. The run goes on, and the wait stays open. |
| A pending item gone from the list, undecided | Withdrawn by the agent (`WITHDRAWN`). A decision still on its way is withdrawn too and never made. |

A list the product did not answer (refused, unreachable, not JSON) concludes nothing, and nothing is withdrawn on
its account.

**How it is scored.** Each item is a wait, `ANSWER_FROM_PERSON`. It falls due after the person's longest delay, a
message to them or a change to the item follows it up, and it settles when the decision is taken or the item is
withdrawn. When the item names what it `gates`, the agent's first call carrying that id after the item settles is
its reaction. So every rule that counts `follow_ups` or `touches` on an ask reads these waits unchanged
(`docs/assessments.md`). The scorecard line about messages adds "decisions asked of people: n,
decided: n, left pending: n".

**The fork.** A `PersonChange` asks the changed person again about every item still pending at the fork, under
their new behaviour. A decision made before the fork that had not landed is withdrawn first.

### What a run prints

Whether going ahead without an approval fails a run is the team's rule, written in the scenario or the agent file:

```yaml
assess:
  - id: acts_only_once_approved
    count: {writes: {gated: true}}
    at_most: 0
    pattern: act_on_the_decision
```

The rejected scenario, with an agent that sends the booking anyway (`REFERENCE_BEHAVIOUR=heedless`), fails that rule
with the write as its evidence, and the report names the pattern: hold each gated operation until a decision that
permits it, and on a rejection close the work and say so instead.

A write is `gated` when its call carries an item's `gates` id and the item was, just before that write:

- still pending, including a write in the same wake before the item was first seen;
- decided by a decision with `permits: false`;
- withdrawn.

Only the agent's first such write per item is counted. With no item naming what it gates, no write is `gated`, and a
rule over them counts none.

### `minutehand serve`: driven from outside

`CreateWorld.inboxes` takes the same declaration. In a standing world:

- A person's `credential` is read from the server's own environment. A generated credential is refused, since no
  command is started to hand it to.
- The inboxes are read at a step's end, on `advance`, and on `checks`.
- With `scripted_people: true`, the people's decisions are owed like replies, and `advance` past one makes it.

A harness with its own clock uses three calls. On the plugin's `minutehand_world` (`OpenWorld`) or an `OpenCase`:

```python
view = world.inboxes()  # POST /v1/worlds/{id}/inboxes/read   -> InboxesView: pending items, decisions due (when)
done = world.perform_due()  # POST /v1/worlds/{id}/inboxes/due    -> the decisions due by the world's, case's or
#                                         latest step's moment, each made as its person
made = world.decide("nadia", item, "reject", {"reason": "..."})  # POST /v1/worlds/{id}/inboxes/decide -> DecisionView
```

`MinutehandClient` and `AsyncMinutehandClient` have `read_inboxes`, `perform_due_decisions` and `decide`. Each act
is recorded exactly as in a run. `tests/architecture/test_driven_approvals.py` shows the loop: mark a step at the
earliest `due`, `perform_due()`, then wake the agent.

### Where it shows

| Where | What |
|---|---|
| `minutehand run`, `findings` | The scorecard line, and the findings. |
| The viewer | A "Decisions in the agent's product" row in the timeline. In "What was said": "asked Nadia Ek to approve or reject: …", "Nadia Ek approved: …", the product's refusal, and a withdrawal. |
| MCP | Results carry the same scorecard. |

## What it cannot do

- **A decision with no API behind it** is out of reach. A person who must click a page that sends nothing an agent
  file can declare cannot be stood in for.
- **An item raised and withdrawn between two readings is never seen.** In a run, readings are at the end of each
  wake and before each clock jump. In a standing world, they are at a step's end, on an advance, and on the three
  calls. So an item raised and taken back within one wake or step is missed.
- **Identity across readings is the item's id alone.** A product that reissues an item under a new id after a
  change asks the person again.
- **The gate check reads only the agent's recorded calls.** An operation whose id never appears in a call's path or
  request body (it lives only in the agent's database) is invisible to it.
- **Silent and model-written people decide only in a run, not in a standing world**, which has no model. In a
  standing world a person decides by script, or when the harness calls `decide`.
- **MCP inboxes are designed, not built.**
