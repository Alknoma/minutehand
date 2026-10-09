# Inboxes: work waiting on a person in the agent's own product

Some of what a person does never touches a SaaS fake: approving an operation in the agent's own web app, answering
a question it raised on its own page. Minutehand reaches these through an **inbox** the agent file declares. It
reads what waits on each person, as that person, and records each new item as the agent asking them. The people
engine decides for the person, after their usual delay, as a take pins or a model picks. Minutehand then makes the
decision as that person, by the declared call, records it as the person's transition, and the ledger and the checks
score the result the way they score a chat question.

Built and tested:

| Where | What |
|---|---|
| Declaration | `src/minutehand/domain/inboxes.py` |
| Calls to the agent | `adapters/agent/inboxes.py`, `adapters/agent/openapi.py` |
| Logic | `application/inboxes.py`, the run loop, `application/standing.py` |
| Facts and check | `checks/facts.py` (each decision a transition a team's rule reads), `checks/agent_contract_changed.py` |
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
- **It goes beyond it in two places:**
  - `reads`, how the record words a decision ("approved").
  - `settles: false` on a decision: a note the person leaves on the item that decides nothing (a comment). It is
    where an away person's automatic reply goes, and a take can pin it.
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
      paging: {next: "$.next", param: cursor}
    decisions:
      - name: approve
        reads: approved
        request:
          kind: template
          method: POST
          url: "http://127.0.0.1:8790/approvals/{item.id}/decision"
          body: {decision: approve}
      - name: reject
        reads: rejected
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
| `pending.category`, `.decisions` | Optional. A category, and the decisions allowed on this item. |
| `pending.paging` | `next` is where the next page's cursor is. It is sent as `param`, or wherever the request names `{page.cursor}`. At most `most` pages are read (50). |
| `decisions[]` | `name`, `description` (a model-written person reads it), `reads`, `settles` (false: a note, offered on every item, that leaves it waiting), `inputs`, `request`, and `succeeds`. `succeeds` is the statuses that mean the decision was taken (any 2xx by default), optionally with a JSONPath `at` that must equal `equals`. |

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
    reply: {kind: scripted, delay: {shortest: PT2H, longest: P1D}, replies: [], then: silent}
    takes:
      - {take: approve}                                              # every item
      - {provider: approvals, nth: 2, take: reject, facts: [this is the second request this week]}  # a model words it
      - {provider: approvals, nth: 3, take: reject, fields: {reason: Twice is once too many}}       # these exact words
```

**What a person does with an item is a take, or a model's pick.** The people engine books each item at the person's
usual delay, and at that moment:

| The person has | They |
|---|---|
| A take that pins the item | Make that decision. A take naming the inbox and an `nth` (the nth item waiting on them there) wins over one for every item there, which wins over one for every item anywhere (`provider` and `nth` both absent). What the decision takes (a reason) is `fields`, word for word, or written by a model from the take's `facts`, in the person's voice. `after` or `within` sets the moment. |
| No take for it, and a model | Are shown the summary, each decision with its description and inputs, and what they know, and pick one through the model port. A note (`settles: false`) is never picked unprompted. |
| No take, and they are `Silent`, or `Scripted` with `then: silent` | Leave it pending. |

A take whose decision the item does not offer, or that gives an input the decision does not take, is refused before
the run starts. A person whose decisions a model writes (a reason it words, or deciding at all) needs a model: with
none configured the run, or the standing world, is refused at the start, naming them. `minutehand migrate <file>`
rewrites the scripted `decisions:` of an older scenario as takes.

**Around the item:**

- **A reminder brings it forward.** A message from the agent to a person who owes a decision is a follow-up on it:
  with `reminded`, their decision moves sooner, as it would for a message.
- **An away person's automatic reply.** When the inbox declares a note, a person away while someone covers leaves
  their automatic reply on the item, at once, naming who covers; the item still waits on them.
- **A fork's `reply_at`** pins an item's moment when it names the inbox as `provider`: `to_ask` is then the nth item
  waiting on them there.
- **What they know.** A decision a model words is worded when the item is first seen, and again when it falls due if
  their facts changed between.

## What Minutehand does with it

### `minutehand run`

**When it reads.** Minutehand reads every inbox as each person it can act as at the end of every wake, and again
before the clock moves. Each read is Minutehand's own call, recorded with `Exchange.inbox_call`. It is never one of
the agent's calls, never an outbound call, and never unmatched.

**What it records:**

| Event | Recorded as |
|---|---|
| An item first seen | The agent asking that person: `InboxItemSnapshot`, `PENDING`, actor AGENT, carrying the summary, the inbox, the item id and the decisions available. The people engine holds what the person will do (`PendingSnapshot`), kept with the run, so a fork replays it. |
| A decision falling due | The person's transition (`TransitionSnapshot`: the decision's name, `pending` to `decided`, by PERSON, its inputs as `content`), recorded before the declared call goes out, so whatever the product writes handling it comes after it. Then the call, made as the person. It wakes the agent as a reply does (`PERSON_REPLIED`). |
| The product takes it | `DECIDED`, by actor PERSON, with the decision, its inputs and how it reads. |
| The product refuses it | Still `PENDING`, by actor PERSON, with the product's answer in `refused`, and a SYSTEM `refuse` transition back to `pending`. The run goes on, and the wait stays open. |
| A note | A PERSON transition from `pending` to `pending`; the item is unchanged. |
| A pending item gone from the list, undecided | Withdrawn: `WITHDRAWN`, and the agent's `withdraw` transition, by AGENT, so a rule tells it from a decision by `by`. A decision still on its way is never made: the people engine's record of it is `GONE`. |

A list the product did not answer (refused, unreachable, not JSON) concludes nothing, and nothing is withdrawn on
its account.

**How it is scored.** Each item is a wait, `ANSWER_FROM_PERSON`. It falls due after the person's longest delay, a
message to them or a change to the item follows it up, and it settles when the decision is taken or the item is
withdrawn. Its answer, what `{ask.answer}` and `{ask.facts}` read, is what the decision carries: its inputs as
given, each a fact of its own, or the decision's name when it takes none. So every rule that counts `follow_ups` or `touches` on an ask reads these waits unchanged
(`docs/assessments.md`). The scorecard line about messages adds "decisions asked of people: n,
decided: n, left pending: n".

**The fork.** A `PersonChange` asks the changed person again about every item still pending at the fork, under
their new behaviour: a decision planned before the fork that had not landed is planned again.

### What a run prints

Whether going ahead without an approval fails a run is the team's rule, written in the scenario or the agent file
over what the run recorded: each ask, and each decision as a transition. The reference agent's
(`examples/reference_agent/agent.yaml`):

```yaml
  - id: acts_only_once_approved      # the booking reference reaches Owen only once whoever it asked has answered
    each: ask
    where: {person_not: [owner]}
    count: {messages: {to: [owner], holding: [LH-2291]}, until: closed-PT1S}
    at_most: 0
    message: "went ahead with the booking before it was approved"
    pattern: act_on_the_decision
  - id: never_books_after_a_rejection # and never once it is turned down
    each: transition
    where: {provider: [approvals], name: [reject], by: [person]}
    count: {messages: {to: [owner], holding: [LH-2291]}, since: transition}
    at_most: 0
    message: "went ahead with the booking after {transition.who} turned it down"
    pattern: act_on_the_decision
```

The rejected scenario, with an agent that sends the booking anyway (`REFERENCE_BEHAVIOUR=heedless`), fails the
second rule with the message as its evidence, and the report names the pattern: hold each operation until a decision
that allows it, and on a rejection close the work and say so instead.

Other shapes, each over transitions:

- **One operation, two approvers.** `each: transition` with `where: {name: [approve], first: true}` reads only the
  first approval, from either; with none, the run never moved and the rule reads the whole run.
- **A request taken back** is the agent's `withdraw`, `by: [agent]`, never a decision.
- **What the agent tells the requester.** `holding: ["{ask.facts}"]` needs each input of the decision word for word;
  `conveys: ["{ask.facts}"]` needs each conveyed in any words, as a judge model reads it. A rule with `conveys` is
  read only with a judge model (the judged check `conveys`), and its findings are for review.

### `minutehand serve`: driven from outside

`CreateWorld.inboxes` takes the same declaration. In a standing world:

- A person's `credential` is read from the server's own environment. A generated credential is refused, since no
  command is started to hand it to.
- The inboxes are read at a step's end, on `advance`, and on `checks`.
- With `scripted_people: true`, the people's decisions are owed like replies, and `advance` past one makes it, as the
  person's transition.

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
- **An operation is seen only by what the agent says or writes.** A rule finds the operation going ahead in a message
  or a write the run recorded; one that lives only in the agent's own database is invisible to it.
- **A standing world decides as a run does**, with the server's own model (`docs/serve.md`, "People and time"):
  a decision a model makes is written when the world's clock passes its moment (`advance`, `inboxes/due`), not
  before, so `inboxes/read` shows its moment and not yet the decision.
- **MCP inboxes are designed, not built.**
