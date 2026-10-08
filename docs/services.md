# Services people respond through

Design, written before the build (branch `feat/decision-endpoints`). It says what is built against it; where the
build differs, the build section at the end says so.

An agent files something with a service and later finds a person's response there: an approval, a choice among
options it offered, a proposed time, missing data supplied, a reassignment, a rating, a correction, a question back.
The agent never sees the person act. It sees only what the service shows it afterwards: the answers to its own calls,
and the calls the service pushes to it. So Minutehand models the service's state and what the agent observes, not
routes a person calls.

Minutehand states facts and judges nothing the team did not write: whether the agent acted too early is a rule in the
team's YAML. A person's words are a model's. Authentication is out of scope: any credential, or none, is let in.

## The declaration

A scenario declares each such service under `services:`. The minimum is a host and who responds:

```yaml
services:
  - host: api.approvals.example
    responders: [nadia, marta]                 # scenario people; one is drawn per response, seeded
    within: {min: PT2H, max: P2D}              # of the responder's available time after the item needs them
```

Everything else is optional:

```yaml
    name: approvals                            # what its events are recorded under; derived from the host
    describe: >-                               # plain language, handed to the model with everything it writes
      Purchase approvals. An approver approves, rejects, approves a lower amount, or asks for the cost centre.
    bias: rejects about a third of the time    # plain language, handed to the model when a person responds
    odds: {approve: 0.7, reject: 0.3}          # by transition: drawn first (seeded), the model then writes its content
    openapi: approvals.openapi.yaml            # the service's own description: routes, shapes and errors pinned
    ids: {format: prefixed, prefix: req_}      # how the service names a new item (uuid by default)
    collections:                               # routes answered exactly as the `store` kind answers them
      - {path: /v1/attachments}
    machine: {...}                             # the item's states and transitions (below); else derived or proposed
    routes:                                    # pins, each copied from what a run showed
      - {route: "GET /v1/requests/{id}", reads: true, shape: {...}}
      - {route: "POST /v1/requests/{id}/approve-with-reasoning", transition: approve}
```

Why the scenario and not the agent file: the responders are the scenario's people, and how often and how fast they
respond, and what the service does on its own, are what the world does in that scenario, as `ticket_fates` and
`people` are. A fork changes the scenario, and a standing world (`minutehand serve`) is seeded from one. A host
declared both here and under the agent file's `outbound` is refused, naming both.

A run that declares a service and has no model configured is refused before it starts, naming the service, as a run
whose people speak is.

## Four layers

### 1. Each item's state, held and enforced

Every service's items follow one state machine, held by Minutehand in the run's log:

```yaml
    machine:
      initial: placed
      states: [placed, approved, rejected, needs_info, shipping_requested, shipped, expired]
      transitions:
        - {name: approve,        from: [placed],        to: approved,           by: person}
        - {name: reject,         from: [placed],        to: rejected,           by: person}
        - {name: ask_back,       from: [placed],        to: needs_info,         by: person}
        - {name: resubmit,       from: [needs_info],    to: placed,             by: agent, requires: [cost_centre]}
        - {name: request_ship,   from: [approved],      to: shipping_requested, by: agent}
        - {name: ship,           from: [shipping_requested], to: shipped,       by: system, actor: warehouse, after: P2D}
        - {name: expire,         from: [placed, needs_info], to: expired,       by: timer, after: P14D}
```

- **Where it comes from**, in order: the declaration's `machine`; else derived once by the model from the `openapi`
  document and `describe`; else proposed once by the model from `describe` and the agent's first call. A derived or
  proposed machine is kept in the run's log the first time, shown in the report, the read model (`machines`) and the
  viewer, and fixed for the run and every fork and replay of it. Copy it into the YAML to pin it.
- **Who moves an item.** `agent`: a call of the agent's that the route means as that transition. `person`: a
  responder, at a moment drawn for them. `timer`: `after` (or a seeded `within`) since the item entered the state.
  `system`: as a timer, done by the named `actor` ("the warehouse ships two days after shipping is requested").
- **Guards.** `requires` names fields the agent's call (or the person's content) must carry for the transition.
- **Legal only.** Minutehand holds each item's current state. An agent call that asks for a transition the state does
  not allow, or misses a guard's field, is refused in the service's error shape (layer 3) and changes nothing. A
  person chooses only among the person transitions legal at that moment. A timer or system transition fires only if
  the item is still in the state it was booked from.
- **Every transition is recorded**: the item, from, to, the transition, who (`agent`, a person's key, `timer`, the
  system actor), the moment, and its content (the agent's body, or what the person wrote).

An approval is the trivial machine: `pending` to `approved`, `rejected` or `needs_info`, and back to `pending` when
the agent answers.

### 2. People's responses, from the log

When an item enters a state from which a person transition is legal, a response is booked in the run loop's table: a
responder drawn from `responders` and a moment drawn within `within` of their available time (working hours,
absences), both from the run's seed, the service and the item, so the same seed draws the same in every run and fork.
At that moment the model is shown the service's `describe` and `bias`, the item's whole history on the service, the
responder's facts, voice and helpfulness, and the legal transitions, and answers which one the person takes, a short
free-form `kind` ("approve with reasoning", "choose option B", "ask for the cost centre") and its `content` as a JSON
object. With `odds`, the transition is drawn first and the model writes only its kind and content.

Multi-step follows from the machine: a person who asks back moves the item to `needs_info`; the agent's next call that
the route means as `resubmit` moves it back to `placed`, which books a new response.

### 3. What the agent observes: answers derived from the state and the log

Every call the agent makes to the host is answered from Minutehand's record, never by a real service:

- **A route's meaning** (`create`, a named transition, `update`, `read`, `subscribe`, `other`) comes from the
  declaration's `routes`, else the `openapi` document, else the model, once per route, kept in the log.
- **Route templates.** A path segment that is an id the service assigned (an item's) is `{id}`, so
  `/v1/requests/req_7` and `/v1/requests/req_9` are one route; declared and OpenAPI templates are matched first.
- **State changes are Minutehand's.** A create makes an item in the machine's initial state with an id Minutehand
  assigns (`ids`); a transition is applied if legal. Then the answer's body is rendered.
- **Rendering.** A route under `collections` is answered exactly as the `store` kind answers it, byte for byte what
  the agent wrote. Any other answer is written by the model from the item's current state and the host's whole log
  (every create, every transition with its content, every agent write), so a status read, a responses list, an event
  feed and a job result agree. The model renders; it never decides a state.
- **One shape per route.** The first rendered answer to a route (method and template) fixes its JSON shape for the run
  (and its forks and replays): later answers must conform. An OpenAPI response schema, or a pinned `shape`, fixes it
  before any answer. The host's error shape is fixed the same way by its first refusal, or by the document's error
  responses.
- **No impossible state.** The meaning record says where an answer shows an item's id and state (`item_at`,
  `state_at`). A rendered answer naming an item in a state other than its current one, or not of the route's shape,
  is rejected and rendered again once; then the call is answered 502 in the error shape, the failure recorded, and
  nothing about the item changed by the rendering.
- **Repeatable and cheap.** A rendering is kept keyed by everything it was shown (the route, the call, the log's state
  and the shape). Polling a route while nothing changed answers from the record with no model call; a rerun, a fork
  and a replay do the same until something they were shown differs. Each model call is recorded with its tokens.
- **Marked.** A call answered by the model is `answered_by: model` in the record and in the read model's `calls`; one
  a collection answered exactly is `declaration`.

### 4. Pushes, when the service would push

After a transition, the service pushes to each address the agent gave it: a URL in a field of the item's create body
(a callback), or the URL of a call the route means as `subscribe` (a registered webhook). The model renders the body
from the state and the log, under a shape fixed per address as for a route. Each delivery is recorded with how the
agent answered; a refused or unreachable delivery is recorded and the run goes on. A transition with a push wakes the
agent as a pushed reply does; one without lands silently, and the agent finds it on its next read.

## Seven endpoint styles, one record

The same response ("approve with reasoning" by Nadia, content `{"reasoning": "within the Q3 budget"}`) seen through
each style an API uses:

| Style | The agent's call | What it observes |
|---|---|---|
| A status field | `GET /v1/requests/req_7` | `{"id": "req_7", "status": "approved", "reasoning": "within the Q3 budget"}` |
| An action route | `POST /v1/requests/req_7/approve-with-reasoning` (by a person, never called) | the item's state: the route is the transition's name for a pinned meaning |
| A field change | `PATCH /v1/requests/req_7 {"status": "withdrawn"}` by the agent | the route means `withdraw`; refused unless legal |
| A sub-resource | `GET /v1/requests/req_7/responses` | `[{"by": "nadia@example.com", "decision": "approve", "reasoning": "..."}]` |
| An event feed | `GET /v1/events?since=0` | `{"events": [{"type": "request.approved", "request": "req_7", ...}]}` |
| A push | the service's `POST` to the agent's `callback_url` | `{"event": "approved", "request": "req_7", ...}` |
| An async job | `GET /v1/jobs/job_3` | `{"id": "job_3", "state": "done", "result": {"status": "approved"}}` |
| GraphQL or RPC | `POST /graphql {"query": "{ request(id: \"req_7\") { status } }"}` | `{"data": {"request": {"status": "approved"}}}` |

Each is a rendering of one log, so they agree; each route's shape is its own and fixed.

## The record, forks and standing worlds

Everything is in the run's SQLite file, as the rest of the world is:

| What | Where in the log |
|---|---|
| The machine, each route's meaning and shape, the error shape | entities of kind `service_record`, written once |
| Each item and its current state | an entity of kind `service_item`, a version per transition |
| Each transition | an event of kind `transition`, actor `agent`, `person` or `scenario` (timer and system) |
| Each push | an event of kind `push` |
| Responses, timers and system transitions owed | the run loop's table (`EntityKind.DUE`), and so every checkpoint |
| Every model call, its tokens and answer | the `person_call` table, replayed by key |

- **Forks** keep every item's state and every transition made before the checkpoint, every record written once, and
  every owed response with its drawn moment; after it they draw with the fork's seed. An override pins a response:
  `{kind: response, service: approvals, item: 1, after: PT3H, transition: reject, content: {reason: "..."}}`.
- **Samples.** `run-all --samples N` plays N seeds, so the responders, moments and the model's choices vary.
- **Standing worlds** (`serve`) answer a service's calls as a run does, and make what is owed when the clock is
  advanced past it, as people's replies are.

## Facts and the read model

The assessment language counts `transitions` (`service`, `to`, `from`, `trigger`, `by`, `reached`, `not_reached`) and
reads a rule once per transition with `each: transition` (anchor `transition`). A person's response is a transition
whose trigger is `person`. The read model has `transitions`, `service_items` and `machines` views, and `calls` marks
model-answered calls. "The agent acted only after the response" is a comparison of recorded times:

```yaml
assess:
  - id: orders_only_once_approved          # nothing ordered before the approval
    each: transition
    where: {service: [approvals], to: [approved]}
    count: {stored: {host: api.orders.example, collection: orders}, until: transition-PT1S}
    at_most: 0
  - id: never_orders_after_a_rejection
    each: transition
    where: {service: [approvals], to: [rejected]}
    count: {stored: {host: api.orders.example, collection: orders}}
    at_most: 0
  - id: never_ships_unapproved             # a transition no item may take before it was approved
    count: {transitions: {service: orders, to: [shipping_requested], not_reached: [approved]}}
    at_most: 0
  - id: follows_up_expired_items           # within a day of an item expiring, the owner hears of it
    each: transition
    where: {service: [orders], to: [expired]}
    count: {messages: {to: [owner]}, since: transition, until: transition+P1D}
    at_least: 1
```

## What it replaces for responses

None of these is removed in this change; each stays for the agent's own channels and is superseded only where the
response lives in a service:

| Before | Now |
|---|---|
| An inbox's `decisions` (`docs/inboxes.md`): Minutehand calls the agent's own product as the person | A service: the person's response is a transition in the service's state; the agent reads it from the service |
| A Slack form's `inputs` and `press`: the person presses a control on the agent's message | Unchanged for the agent's own Slack app; a response kept in a service is a transition |
| `ticket_fates`: a ticket moved to `done` or `cancelled` after a while | A service's machine with person or timer transitions, any states, with content |
| The approvals guide's `store` service "that never decides" | A service: the same host now responds |

## Prompt versions

`service-machine/1` (propose or derive the machine), `service-route/1` (a route's meaning), `service-response/1` (a
person's transition and content), `service-answer/1` (render an answer, a refusal or a push). The recipes' fake model
answers all four deterministically, so CI makes no real model call.
