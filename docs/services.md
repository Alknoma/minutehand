# Declared services: hosts people respond through

An agent files something with a service no provider fakes and later finds a person's response there: an approval,
a choice among options it offered, missing data supplied, a question back. The agent never sees the person act; it
sees only what the service shows it afterwards: the answers to its own calls, and the calls the service pushes to it.
So Minutehand holds the service's state and renders what the agent observes from it. Every move is a transition
(`docs/design-transitions.md`), and the service's people act through the same people engine as a Jira assignee or a
Calendar guest.

Minutehand judges the agent against the world the scenario declares, and nothing else: a move the machine refuses, an
order placed before the state the goal waits on, an item left where only the agent could move it on, a reaction
slower than the agent's declared rhythm, an invented figure in a resubmission (`docs/assessments.md`, "What every run
is assessed on"); a team's own rules add policy the world cannot imply. A person's words are a model's. Authentication is out of scope: any credential, or none, is let in.

## The declaration

A scenario declares each such service under `services:`. The minimum is a host, who responds, and when:

```yaml
services:
  - host: api.approvals.example
    responders: [nadia, marta]                 # scenario people; one is drawn per item and state, seeded
    within: {min: PT2H, max: P2D}              # of the responder's available time after the item waits on them
```

Everything else is optional:

```yaml
    name: approvals                            # what its records and transitions are kept under; from the host
    describe: >-                               # plain language, handed to the model with everything it writes
      Purchase approvals. An approver approves, rejects, or asks for the cost centre.
    bias: rejects about a third of the time    # plain language, shown to the model that picks a person's move
    odds: {approve: 0.7, reject: 0.3}          # by transition: drawn first (seeded), the model then writes its words
    openapi: approvals.openapi.yaml            # the service's own description: routes, shapes and errors pinned
    ids: {format: prefixed, prefix: req_}      # how the service names a new item (uuid by default)
    collections:                               # routes answered exactly as the `store` kind answers them
      - {path: /v1/attachments}
    machine: {...}                             # the item's states and transitions (below); else proposed once
    routes:                                    # pins, each copied from what a run showed
      - {route: "GET /v1/requests/{id}", means: read, item_at: id, state_at: status, shape: {...}}
      - {route: "POST /v1/requests/{id}/resubmit", means: transition, transition: resubmit}
```

A responder's move can be pinned as any person's is (`Person.takes`, the service's `name` as its provider):
`takes: [{provider: approvals, take: reject, after: PT3H, facts: ["the budget is spent"]}]`.

A host declared both here and under the agent file's `outbound` is refused, naming it. A run that declares a service
and has no model configured is refused before it starts, naming the service.

## Each item's state, held and enforced

```yaml
    machine:
      initial: placed
      states: [placed, approved, rejected, needs_info, shipping_requested, shipped, expired]
      transitions:
        - {name: approve,      from: [placed],             to: approved,           by: person}
        - {name: reject,       from: [placed],             to: rejected,           by: person}
        - {name: ask_back,     from: [placed],             to: needs_info,         by: person}
        - {name: resubmit,     from: [needs_info],         to: placed,             by: agent, requires: [cost_centre]}
        - {name: request_ship, from: [approved],           to: shipping_requested, by: agent}
        - {name: ship,         from: [shipping_requested], to: shipped, by: system, actor: warehouse, after: P2D}
        - {name: expire,       from: [placed, needs_info], to: expired, by: timer, after: P14D}
```

- **Where it comes from**: the declaration's `machine`; else proposed once by the model (`service-machine/1`) from
  the `openapi` document and `describe`, or from the agent's first call. It is kept in the run's log the first time
  (`EntityKind.SERVICE_RECORD`) and fixed for the run and every fork of it; copy it into the YAML to pin it.
- **Who moves an item.** `agent`: a call of the agent's that the route means as that transition. `person`: the
  responder drawn for the item, through the people engine. `timer`: `after` (or `within`) since the item entered the
  state. `system`: as a timer, done by the named `actor`, recorded as `Actor.SYSTEM` with its name as `who`.
- **Guards.** `requires` names fields the agent's call, or the person's response, must carry.
- **Legal only.** An agent call asking for a transition the state does not allow, or missing a guard's field, is
  refused in the service's error shape and changes nothing. A person is offered only the person transitions legal
  at that moment. A timer or system move fires only if the item is still in the state it was booked from.
- **Every move is one transition** in the log (`TransitionSnapshot`): the item (`EntityKind.SERVICE_ITEM`), its
  name (`create` for the move that files it), from, to, who, the moment, and its content (the agent's body, or what
  the person wrote).

## People's responses, through the engine

The service is a provider of the transitions port (`ServiceItems`): an item in a state from which a person may move
it waits on the responder drawn for it (from the responders the item names by email or key, else from all of them;
seeded by the run's seed, the service, the item and the state's entry). The engine books their moment within the
service's `within` of their available time, and at it picks one of the legal offers (the drawn one when the service
has `odds`, the pinned one when the scenario pins it) and writes its content: each field the transition `requires`,
and a `note` in their words. Multi-step follows from the machine: a person who asks back moves the item to
`needs_info`; the agent's `resubmit` moves it back, and it waits on its responder again.

**A responder who never decides** is declared so: list no responders, or declare the responder
`reply: {kind: silent}`. Either way nobody acts on the service's items, by the author's word, and the run's health
says so as coverage (`waits_by_declaration`), never as a fault of the simulation:

```yaml
people:
  - {key: nadia, name: Nadia Ek, email: nadia@example.com, reply: {kind: silent}}   # never decides
services:
  - {host: api.approvals.example, name: approvals, responders: [nadia], within: {min: PT3H, max: P1D}}
```

A responder whose script ends in silence (`reply: {kind: scripted, then: silent}`) with no `takes` pinning what they
do on the service is not that: a script is a person meant to speak, and once its steps are used they say nothing
more, so an item waits on them and they can never act on it. `minutehand validate` warns of every such responder,
and a run in which an item waited on one ends `simulation_incomplete` (exit 6), naming the item, the person and
since when (`responder_never_acts`, `docs/design.md` "The simulation's health"). Use `then: answers` for a responder
who decides once their script is used, or a take (`takes: [{provider: approvals, take: approve}]`) to pin what they
do.

## What the agent observes: answers rendered from the state and the log

- **A route's meaning** (`create`, a named transition, `update`, `read`, `subscribe`, `other`) comes from the
  declaration's `routes`, else the model reading it once (`service-route/1`), kept in the log.
- **Route templates.** A path segment that is an id the service gave an item is `{id}`; declared and OpenAPI
  templates are matched first.
- **State changes are Minutehand's.** A create makes an item in the machine's initial state with an id Minutehand
  assigns; a transition is applied if legal. Then the answer's body is rendered (`service-answer/1`). A route under
  `collections` is answered exactly as the `store` kind answers it.
- **One shape per route.** The first rendered answer to a route fixes its JSON shape for the run (and its forks);
  later answers must conform. An OpenAPI response schema, or a pinned `shape`, fixes it before any answer. The
  host's error shape is fixed the same way by its first refusal, or by the document's error responses.
- **No impossible state.** A rendered answer naming an item in a state other than its own (`item_at`, `state_at`),
  or not of the route's shape, is rendered again once; then the call is answered 502, the failure recorded.
- **Call-free polling.** Each rendered answer is kept with the state it was rendered in. The same call while the
  service's state is unchanged is answered from that record with no model call, in a run, a rerun or a fork.
- **Marked.** A call answered by the model is `answered_by: model`; one a collection answered is `declaration`.

## Pushes

After a move, the service pushes to each address the agent gave it: an http(s) URL anywhere in the item's create
body (a callback), or in a call the route means as `subscribe`. The body is rendered from the state and the log, its
shape fixed per address. Each delivery is recorded (`EntityKind.PUSH`) with how the agent answered; a refused or
unreachable delivery is recorded and the run goes on. A move with a push wakes the agent as a pushed reply does; one
without lands silently, and the agent finds it on its next read.

## A host nobody declared

Under `--capture-unknown model` (`docs/capture.md`), a write to an undeclared host, and every call to it after, is
answered as a service with no description and no responders: its machine proposed from the first write, its answers
rendered from what the agent did there.

## Forks, standing worlds, facts

- **Forks** keep every item's state and every move made before the checkpoint, every record written once, and every
  response owed with its drawn moment; after it they draw with the fork's seed.
- **Standing worlds** (`minutehand serve`) answer a service's calls as a run does, and make what is owed when the
  clock is advanced past it; `GET /v1/worlds/{id}/transitions` lists the waits and every move.
- **Facts.** Moves are counted with `count: {transitions: {provider: [approvals], to: [approved], by: [person]}}`
  and read once each with `each: transition` (`docs/assessments.md`); `by` takes `agent`, `person`, `system` and
  `timer`. The read model's `transitions` and `items` views hold them (`docs/querying.md`).

```yaml
assess:
  - id: orders_only_once_approved          # nothing ordered before the approval
    each: transition
    where: {provider: [approvals], to: [approved]}
    count: {stored: {host: api.orders.example, collection: orders}, until: transition-PT1S}
    at_most: 0
  - id: never_ships_unapproved             # no item shipped that was never approved
    count: {transitions: {provider: [orders], to: [shipping_requested], not_reached: [approved]}}
    at_most: 0
  - id: follows_up_expired_items           # within a day of an item expiring, the owner hears of it
    each: transition
    where: {provider: [orders], to: [expired], by: [timer]}
    count: {messages: {to: [owner]}, since: transition, until: transition+P1D}
    at_least: 1
```

## Prompt versions

`service-machine/1` (propose the machine), `service-route/1` (a route's meaning), `service-answer/1` (render an
answer, a refusal or a push), and the engine's `person-transition/1` for a person's response. The recipes' fake
model (`examples/recipes/fake_model.py`) answers all of them deterministically, so CI makes no real model call.
