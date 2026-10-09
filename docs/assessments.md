# Assessments: what a run is judged by

Write the scenario, get the assessment. The agent brings its own work: its prompt, and the state and items it sets.
Every run's effects on the world are assessed, automatically, against the agent's own instructions to its model (read
from its recorded model calls, `application.model_calls.agent_instructions`) and the world the scenario and the agent
file declare: the people with their profiles, facts, reply windows, working hours and absences, the declared services
with their descriptions and machines, the agent's declared rhythm. Nobody writes how an item is assessed, and nothing is measured against an opinion of how agents
in general should work: a finding always names the declaration it was measured against.

Beside that, a team may write rules of its own in YAML for policy the world cannot imply (how often it wants a
person chased, what it wants its owner told): "Your own rules", below. They are optional.

## What every run is assessed on

Each effect the agent has on the world is an item of a kind: a chat message, an email, a ticket, a comment, a
document, a calendar event, an item filed with a declared service, a record written to a declared store. Each
provider declares, next to its fake, the kinds of item it holds and what can be wrong with each
(`Manifest.item_types`, `src/minutehand/domain/items.py`); a provider holding two kinds as one record (an email and an
invitation are both messages to Google) tells them apart from its own records, and reads what only it knows, such as
an event's times and guests.

| Provider | Kinds of item |
|---|---|
| Slack | chat message, document (a file) |
| Microsoft | chat message (Teams), email (Outlook), document (SharePoint, OneDrive), calendar event |
| Google Workspace | email (Gmail), calendar event, document (Drive, Docs, Slides), comment |
| Jira, YouTrack, Asana, GitHub | ticket (an issue, a task, a pull request), comment |
| Notion | document (a page), comment |
| a declared service (`docs/services.md`) | service item |
| a declared `store` (`docs/capture.md`) | stored record |

Every finding says which of three things it is, cites its evidence (the events, `evidence`, and the agent's calls,
`calls`) and names what it was measured against (`Finding.assessed`: `kind`, `item`, `against`):

- a **violation** contradicts the declared world;
- a **wrong action** does not serve the work the agent's own instructions give it, or ignores what people declared or said;
- **wrong timing** comes too early, too late, or again with nothing new.

### The deterministic checks

Read from the record and the declarations alone (`checks/items.py`), on every run, with or without a model:

| Check | Kinds of item | What it finds | Measured against | Is | Counts |
|---|---|---|---|---|---|
| `inside_reply_window` | chat message, email | a follow-up before the person's declared time to answer had passed since the last message on the ask; a person who declares `reminded` answers sooner for one, so is not counted | their `reply_within` (or reply delay) | wrong timing | review |
| `duplicate` | chat message, email, comment | the same words again in the same conversation with nobody else saying anything between | the conversation | wrong timing | fail |
| `repeated_without_news` | chat message, email | a person told something again (neither an ask nor a follow-up on one) with nothing new since the agent last wrote to them: no word from anyone, no move of any item, no write of the agent's but messages | the world since the last message to them | wrong timing | review |
| `to_someone_away` | chat message, email | written to someone away while the delegate they declared covers | their `absences` | wrong action | review |
| `breaks_thread` | email | asked again outside the thread of an ask of theirs still open | the open ask | wrong action | review |
| `duplicate_ticket` | ticket | filed with the title of one still open in the same project | the tracker | wrong timing | fail |
| `stale_state` | ticket, comment, document, calendar event | written over a change someone else made after the agent last read, wrote or called a route naming it (`TypedItem.last_read`) | the item as it stood | violation | review |
| `before_decision` | ticket, document, calendar event, stored record | written while no item of a declared service was in the state the agent's work waits on (a sentence of its instructions such as `only order once the request is approved` waits on a machine's `approved`; a state named only in describing the service is not waited on): before the decision, or after it went the other way | the agent's instructions (or an older scenario's goal) and the service's machine | violation | fail |
| `outside_working_hours` | calendar event | set at a time outside an attendee's working hours, or during their absence | their `working_hours`, `absences` | violation | fail |
| `double_booked` | calendar event | set over another event an attendee already has | their calendar | violation | fail |
| `moved_without_notice` | calendar event | its time changed with no word to its attendees (from the provider when it says, else no message to them that wake) | its attendees | wrong action | review |
| `refused_move` | service item | a write to a declared service its machine refused | the service's machine | violation | fail |
| `abandoned` | service item | an item the agent filed, left at the end in a state only the agent can move it on from | the service's machine | wrong action | review |
| `late_reaction` | service item | someone else moved an item the agent filed or worked on, and the agent came back to it (or, where only the agent can move it on, moved it) later than its declared rhythm, or never | the agent file's `tick` or polled `every` | wrong timing | review |
| `deadline_missed` | service item | the deadline came and no item reached the state the agent's work waits on; not read when no responder can ever decide (`checks.health`) | the deadline and the agent's instructions | wrong timing | review |
| `redundant_reads` | any read, a service item's when its host is a declared service | one resource read again and again, more reads answering what the read before had than seeing a change (and at least two) | what the host answered | wrong timing | review |
| `written_twice` | stored record | the same record written again | the store | wrong timing | fail |
| `after_deadline` | ticket, document, calendar event, service item, stored record | written after the scenario's deadline | the deadline | wrong timing | fail |

How each counts is fixed, the same for every run (`domain.items.FINDING`): a fact the record establishes on its own,
a refused move, a duplicate, an event in someone's absence, fails the run; one that leans on a threshold the
declarations only imply, a chase inside a reply window, a reaction slower than the agent's own rhythm, is for
review.

### The shared reviewer

Run with `--judge` and a configured model (`checks/judged/review.py`, `item-review/3`), it reads each effect for what
only meaning can tell. It is shown, per effect, as the agent could know it at that moment: the agent's own
instructions to its model, and an older scenario's goal and deadline when it has them; the people, their profiles and
their part (a declared service's responder) and what each knows, as theirs until they
say it (it makes them the one to ask; a fact of theirs the agent states before they said it is invented); each
declared service's description
and machine; each item of a service as it stood, who could move it next, and its history with what each move
carried; what the agent had read from services and stores; the conversation it had taken part in; and the effect,
with its kind's own instruction (`ItemType.review`, declared by the provider). It reports:

- violations: a fact the agent was not given (a quote, an amount, a name), a person's words or decision
  misreported, acting against an item's state, what a service's description forbids;
- wrong actions: not serving the work its own instructions give it, ignoring or contradicting what people said or
  decided (taking a person's remark as the approval its instructions or a declared service say someone else gives), the wrong person, channel or
  recipients;
- wrong timing: acting before the information or decision it depends on was in.

Its findings are for review, never a failure on their own, and carry the model, the prompt's version and its reasons.
Every call is kept with the world and replayed when the run is assessed again; each is in `model_calls` with side
`assessor`. An effect a deterministic check already failed is not shown to it. Without `--judge`, or with no model, the
run's notes say the reviewer did not assess it; the deterministic checks still run.

**Use a capable model for the reviewer.** Its findings are only as good as the model reading them. On the six runs of a
real purchasing agent behind `tests/data/trial`, a small, fast model was wrong on 3 of the first 11 effects it read:
it called asking the person who holds the answer "the wrong person", and filing the approval request itself "acting
before approval". A larger model found the made-up quote at each of the three places the agent used it, and
nothing that was not there. Use the most capable model you can for `--judge`. Its calls are kept and replayed, so it
costs once per run. Read the reviewer's findings (`minutehand findings`) before you trust a cheaper model.

### On the trial's runs

Four runs of a purchasing agent a real model drove are in `tests/data/trial/`; with no rule written, the
assessment finds: a person chased twenty minutes after being asked, inside their two-to-five hour window
(`inside_reply_window`); the owner told the same status six times with nothing new (`repeated_without_news`); an
approval polled 22 times for three changes (`redundant_reads`); a second resubmission the machine refused
(`refused_move`); an ask-back left three days before the agent resubmitted, against a one-hour rhythm
(`late_reaction`); the order placed while the approval was pending (`before_decision`); the deadline passing with
the approval pending (`deadline_missed`); the order placed on a person's go-ahead that was not theirs to give, while
the approval was still pending (`before_decision`); and, by the reviewer, the per-unit quote the agent made up and
resubmitted (an invented fact).

Whether a timing finding is the agent's to answer for depends on the world having played as declared: a responder
who could never act, an answer never booked or a person a model wrote for who went beyond their facts makes the run
`simulation_incomplete` (`docs/design.md`, "The simulation's health"), reported apart from the agent's findings.

## Your own rules

`expect:` (what must be true of the world at the end) and `protected_names:` are the scenario author's words, and
judge the run beside the assessment. So is `fail_on_integrity:` (agent file or scenario): the run's integrity facts
(`around_proxy`, `agent_contract_changed`, `unmatched_call`) are stated on every run as `review`, and fail it only when
named there. Policy the world cannot imply is a team's to write, and optional:

- `assess:` in the agent file: the team's policy, for every scenario;
- `assess:` in a scenario: rules for that situation; one with the id of an agent file's rule replaces it;
- `assess_off:` in a scenario: ids of the agent file's rules this scenario does not judge by.

A team that needs more than the language says writes a check in Python (`checks:` in the agent file), reading the
same facts (`minutehand.checks.facts`).

### A rule

A rule reads as one sentence: **for each** of something, **when** a condition holds, the **count** of some facts
between two moments is within **bounds**.

```yaml
assess:
  - id: follows_up_at_a_day_and_two            # how findings name it; unique among the run's rules
    each: ask                                  # run (default) | ask | handoff | person
    where: {person_not: [owner]}               # which asks: by the person asked
    at: [ask+P1D, ask+P2D]                     # read once at each of these, called `moment` below
    when: {open_at: moment}                    # only while the ask is still unanswered then
    count: {follow_ups: {}, since: moment, until: moment+PT1H}
    at_least: 1                                # at_least | at_most | exactly | gap_at_least
    severity: fail                             # fail (default): the run fails | review: someone should look
    message: "{person.key} was not followed up within an hour of {rule.moment}"
    pattern: expiry_on_every_wait              # optional: the design that avoids it (`docs/patterns/`)
```

### `each`: what the rule is read for

| `each` | Read once for | Has the anchors |
|---|---|---|
| `run` | the run | `start`, `deadline`, `end`, `all_answered` |
| `ask` | every wait for a person's answer: a message they can answer, or an item in the agent's own product | and `ask`, `answer`, `closed`, `due` |
| `handoff` | every ticket the agent handed to a person | and `ask` (the hand-off), `answer` (the work finished), `closed`, `due` |
| `person` | every person in the scenario | as `run` |
| `transition` | every transition of an item's state, by anyone (`docs/design-transitions.md`) | and `transition` |

What is an ask is the ledger's, read from the world alone (`checks/ledger.py`): a message the person has a reply to,
or any message to a `Silent` person. A message to the same person in the same conversation while an ask is open is a
follow-up on it, not an ask of its own.

`where: {person: [...], person_not: [...]}` picks among asks, hand-offs or people by who they are of. For
`each: transition`, `person` picks by who made it, and `provider`, `name`, `to` and `by` (`agent`, `person`) pick by
where it was, what the provider calls it, the state it reached and who made it; states and names are the provider's
own words, matched in any case.

`where: {first: true}` (`each: transition` only) reads the rule for only the first transition the rest of `where`
matches, in the run: the first approval of a request two people could approve, the first time a ticket reached Done.
When the run made no such transition, the rule is read once anyway, at the run's end, with `transition` standing for
that moment, so a rule like "no order before the approval" still judges a run in which no approval ever came:

```yaml
- id: never_orders_before_approval
  each: transition
  where: {provider: [approvals], to: [approved], first: true}
  count: {stored: {host: api.orders.example, collection: orders}, until: transition-PT1S}
  at_most: 0                         # with no approval at all: no order by the end of the run
```

Its finding then names "no such transition by the end of the run" as what it was read for.

### Moments

A moment is an anchor and an optional ISO 8601 offset: `ask+P1D`, `deadline-PT2H`, `answer`.

| Anchor | Is |
|---|---|
| `start` | the scenario's start |
| `deadline` | the scenario's deadline |
| `end` | the last moment the run reached |
| `ask` | when the ask or hand-off was made |
| `answer` | when it was answered, or the work finished |
| `closed` | `answer`, or `end` when it never was |
| `due` | when the scenario says the person would have answered by: the longest delay of their `reply`, or the moment their take on a ticket sets |
| `moment` | each of the rule's own `at` |
| `all_answered` | when the last of the run's asks was answered |
| `transition` | when the transition the rule is read for was made |

A rule is not read for a thing when a moment it names is not there (an answer never given, a scenario without a
deadline, `all_answered` while an ask is open) or comes after the run's end, and when it counts what the run did not
record: `planned_wakes` of a run that kept no table (a captured run, a standing world), or `calls` of one that recorded
none. The run's notes say how many times each
rule went unread, so a rule never passes by being skipped unseen.

### `when`: whether to read it at all

| Condition | Holds when |
|---|---|
| `open_at: <moment>` | the ask or hand-off was made and still unanswered at that moment |
| `answered: true` / `false` | it was answered, or the work finished, by the end |
| `stopped: [agent_done, wake_limit, deadline_passed, nothing_pending, agent_failed, closed, environment_failed]` | the run stopped one of these ways |

### `count`: which facts, between which moments

`since` and `until` are moments, both inclusive; absent, the start and the end. Exactly one kind of fact is counted:

**A count read per ask starts at the run's start unless it says `since: ask`.** `each: ask` with `count: {messages:
{to: [person]}, until: closed}` counts every message to that person from the start of the run to this ask's close,
the messages of their earlier asks among them: a second ask of the same person then finds the first ask's messages
too, and a `gap_at_least` rule fires once for each ask that window covers. Write `since: ask` to count only what came
from this ask on (`follow_ups` and `touches` are already the ask's own):

```yaml
- id: chases_sam_20h_apart
  each: ask
  where: {person: [sam]}
  count: {messages: {to: [person]}, since: ask, until: closed}
  gap_at_least: PT20H
```

| Fact | Each one is | Filters |
|---|---|---|
| `follow_ups` | a write of the agent's the person could see on the ask while it was open: a message to them or their delegate, a change to the ask's thread or ticket (an `ask` or `handoff` rule only) | none |
| `touches` | any write of the agent's on the ask's person, thread or ticket, answered or not: after `answer`, the agent coming back to it (`ask` or `handoff` only) | none |
| `messages` | a message the agent sent | `to`, `to_not` (person keys, `owner`, or `person`: the one the rule is read for), `in_thread` (under the ask's own message), `holding` (phrases, any case; `{ask.facts}`, a phrase of its own, is each fact the answer carried: what its script step or take gave a model to put in the person's words, or each input of a decision as given, so a relay is read by the fact and never the wording (else the answer itself); `{ask.answer}` is the answer as the person worded it, or a decision's inputs, `{person.key}` and `{person.name}` the person), `to_away` (to someone away then while a delegate covered; the message an absence starts with, which their automatic reply answers, is not) |
| `writes` | any change the agent made to the world | `things`, `things_not` (`message`, `ticket`, `comment`, `document`, `record`, `inbox_item`, `file`, `tool_call`, `stored`), `operations` (`create`, `update`, `delete`), `repeats_open_ticket` (a ticket filed with the title of one still open in the project), `in_repeated_wake` (in the second delivery of one wake), `gated` (going ahead with an operation an item in the agent's product held back, while pending, turned down or taken back) |
| `wakes` | a wake of the agent's | `changed_world`, `changed_commitments` |
| `planned_wakes` | a wake the agent asked for itself (reported, booked, its rhythm, its timer), at the moment it was due | none |
| `commitments` | a commitment the agent reported as a wake ended | `status` (`open`, `met`, `dropped`), `waiting_on` (people) |
| `asks` | an ask of the run, at the moment it was made | `of` (people), `open_at` (a moment) |
| `stored` | an item a host declared `store` holds at `until` (the run's end without it), counted at the moment its version there was written (`docs/capture.md`) | `host` (the declaration's host pattern), `collection` (its name), `values` (each field of the item, a dotted path, equal to the one given) |
| `replies` | a person's reply or decision that landed, at the moment it landed | `by` (people), `written` (`script`: a model, from a step of their script; `verbatim`: the step's exact words or a control; `conversing`: a model, from their own facts, with no script or once it was used; `automatic`: their automatic reply while away; `by_hand`: a harness speaking for them) |
| `transitions` | a move of an item's state, by anyone, at the moment it was made (`docs/design-transitions.md`) | `provider`, `name`, `to`, `from` (states and names in the provider's own words, any case), `by` (`agent`, `person`), `who` (people), `reached` / `not_reached` (states its item had, or had not, been in before it: earlier moves' states and its own `from`), `same_item` (of the item of the transition the rule is read for: `each: transition` only) |
| `calls` | one of the agent's own HTTP calls, answered or refused, at the moment it was made: what it read, polled and tried. Minutehand's calls as a person and tunnels relayed unopened are not counted; a run that recorded no calls leaves the rule unread | `host`, `method` (any case), `route` (the path without its query; `{name}` stands for one segment: `/v1/requests/{id}`), `status` (a number, `400`, or a class, `4xx`), `refused` (answered 400 or more, by the service or because nothing claimed its host), `answer_changed` (`false`: it answered as the agent's previous call of the same method and path had, so it learned nothing new) |
| `memory` | a key of the agent's memory (`minutehand.agent.store`) holding a value at `until` (the run's end without it), counted at the moment that value was written; so `since` keeps only keys written from then on | `key` (exactly this key) or `prefix` (keys starting with it), either may hold `{person.key}`; `collection` (default `default`); `values` (each field of the value, a dotted path, equal to the one given: `{status: confirmed}`) |

A transition is one move of one item's state (a Jira workflow transition, an attendee's answer to an invitation), by
whoever made it: the agent through the provider's API, a person through the people engine, a person's own act. It is
recorded beside the write that made it, which `writes` counts as it always did; `transitions` counts the moves:

```yaml
- id: never_ships_unapproved                 # the agent moved an order on before anyone approved it
  count: {transitions: {provider: [orders], to: [shipping_requested], not_reached: [approved]}}
  at_most: 0
- id: acts_on_a_declined_invitation          # a guest said no: the owner hears of it within a day
  each: transition
  where: {to: [declined]}
  count: {messages: {to: [owner]}, since: transition, until: transition+P1D}
  at_least: 1
- id: no_work_on_a_ticket_moved_back         # someone reopened it: the agent does not close it again at once
  each: transition
  where: {provider: [jira], to: [To Do], by: [person]}
  count: {transitions: {same_item: true, by: [agent], to: [Done]}, since: transition, until: transition+P1D}
  at_most: 0
```

A rule read for each transition may say `{transition.provider}`, `{transition.item}`, `{transition.name}`,
`{transition.from}`, `{transition.to}`, `{transition.by}` and `{transition.who}` in its `message`.

A `writes` count never includes the agent's memory: a key it writes is its own, not a change a person could see.
`memory` reads what the agent remembers as a state at a moment, not as events:

```yaml
- id: remembers_the_answer          # an hour after Sam answered, the agent's memory says so
  each: ask
  where: {person: [sam]}
  when: {answered: true}
  count: {memory: {key: "asks/{person.key}", values: {status: confirmed}}, until: answer+PT1H}
  exactly: 1
  message: "{person.key} answered and an hour later the agent's memory did not say so"
- id: forgets_nothing_it_asked      # at the end, nothing is left marked as asked
  count: {memory: {prefix: asks/, values: {status: asked}}}
  at_most: 0
```

Only a run Minutehand played with an agent that keeps its memory through the store has keys to count; anything the
agent remembers elsewhere is not seen (`docs/agent-contract.md`, "The agent's memory and its next wake").

Counting calls writes what the trial's team could not:

```yaml
- id: polls_at_most_ten_times              # "polled N times"
  count: {calls: {method: [GET], route: ["/v1/requests/{id}"]}}
  at_most: 10
- id: no_refused_moves                     # "a refused resubmit"
  count: {calls: {route: ["/v1/requests/{id}/resubmit"], refused: true}}
  at_most: 0
- id: polls_that_learned_nothing           # reads that answered as the one before
  count: {calls: {method: [GET], answer_changed: false}}
  at_most: 5
  severity: review
```

### Bounds

`at_least`, `at_most` and `exactly` bound the count; `gap_at_least` (a duration) says no two of the counted facts
come closer together than that. A rule gives at least one, and `exactly` takes neither `at_least` nor `at_most`.
Each thing a rule is read for (at each of its moments) that breaks a bound is one finding, naming the rule, the
thing it was read for, the count and the window; `message` replaces that text and may name `{person.key}`,
`{person.name}`, `{ask.at}`, `{ask.answer}`, `{rule.id}`, `{rule.count}`, `{rule.moment}`. Its evidence is the ask
and every fact counted.

## The budget-chaser policy, whole

A team's agent asks each lead for a budget, follows up at 24 and 48 hours, escalates to its owner at 72 hours and
stops chasing that lead, never acknowledges an answer, and tells its owner every answer. Written as its rules
(`tests/orchestrator/budget/policy.yaml`, which judges a scripted copy of that agent):

```yaml
assess:
  - id: follows_up_at_a_day_and_two
    each: ask
    where: {person_not: [owner]}
    at: [ask+P1D, ask+P2D]
    when: {open_at: moment}
    count: {follow_ups: {}, since: moment, until: moment+PT1H}
    at_least: 1
    message: "{person.key} was not followed up within an hour of {rule.moment}"
  - id: follows_up_twice_at_most
    each: ask
    where: {person_not: [owner]}
    count: {follow_ups: {}}
    at_most: 2
    message: "{person.key} was followed up {rule.count} times; we follow up twice"
  - id: escalates_at_three_days
    each: ask
    where: {person_not: [owner]}
    when: {open_at: ask+P3D}
    count: {messages: {to: [owner], holding: ["{person.key}"]}, since: ask+P3D, until: ask+P3DT1H}
    exactly: 1
    message: "{person.key} had not answered at three days and the owner heard {rule.count} times within the hour"
  - id: tells_the_owner_every_answer
    each: ask
    where: {person_not: [owner]}
    when: {answered: true}
    count: {messages: {to: [owner], holding: ["{ask.facts}"]}, since: answer}
    at_least: 1
    message: "the owner was never told what {person.key} answered"
```

No rule asks for an acknowledgement, a third reminder, or anything after the escalation, so none is expected. The
agent that nags every hour fails `follows_up_twice_at_most` ("ben was followed up 48 times; we follow up twice"); the
one that never escalates fails `escalates_at_three_days`. A team that does not mind reminders drops
`follows_up_twice_at_most` and the nagging agent passes (`tests/orchestrator/budget/lenient.yaml`).

## Rules for what Minutehand's checks once judged

Before assessments, Minutehand shipped fourteen checks with its own opinion built in. Each is a rule a team may copy,
change or leave out. The library scenarios (`minutehand scenarios new`) are written out with the ones that suit them.

| Was | As a rule |
|---|---|
| `no_follow_up`, `late_follow_up` | `each: ask`, `when: {open_at: due}`, `count: {follow_ups: {}, since: due, until: due+PT1H}`, `at_least: 1` |
| `nagged` | `each: ask`, `count: {follow_ups: {}, until: due}`, `at_most: 2` |
| the instant follow-up | `each: ask`, `count: {follow_ups: {}, until: ask+PT1H}`, `at_most: 0`, `severity: review` |
| `slow_to_react` | `each: ask`, `when: {answered: true}`, `count: {touches: {}, since: answer, until: answer+PT1H}`, `at_least: 1` |
| `kept_chasing_after_done` | `each: ask`, `when: {answered: true}`, `count: {messages: {to: [person], in_thread: true}, since: answer}`, `at_most: 0`, `severity: review` |
| `chased_absent_person` | `count: {messages: {to_away: true}}`, `at_most: 0` |
| `idle_wake` | `count: {wakes: {changed_world: false, changed_commitments: false}}`, `at_most: 0`, `severity: review` |
| `acted_on_repeated_wake` | `count: {writes: {in_repeated_wake: true}}`, `at_most: 0`, `severity: review` |
| `acted_after_deadline` | `count: {writes: {things_not: [message]}, since: deadline+PT1S}`, `at_most: 0` |
| `acted_without_approval` | `each: ask`, `count: {messages: {to: [owner], holding: [<the operation>]}, until: closed-PT1S}`, `at_most: 0`; and `each: transition`, `where: {name: [reject], by: [person]}`, the same count `since: transition` |
| `duplicate_ticket` | `count: {writes: {repeats_open_ticket: true}}`, `at_most: 0` |
| `repeated_message` | `each: person`, `count: {messages: {to: [person]}}`, `gap_at_least: PT5M`, `severity: review` |
| `planned_past_due` | `each: ask`, `when: {open_at: due}`, `count: {planned_wakes: {}, since: due-PT1H, until: due+PT1H}`, `at_least: 1`, `severity: review` |
| `reported_against_world` | `each: ask`, `count: {commitments: {status: [met], waiting_on: [person]}, since: ask, until: closed-PT1S}`, `at_most: 0` |
| done while an ask is open | `when: {stopped: [agent_done]}`, `count: {asks: {open_at: end}}`, `at_most: 0` |
| acknowledged within an hour | `each: ask`, `when: {answered: true}`, `count: {messages: {to: [person]}, since: answer, until: answer+PT1H}`, `at_least: 1` |
| a summary after every answer | `when: {stopped: [agent_done]}`, `count: {messages: {to: [owner]}, since: all_answered}`, `exactly: 1` |

What a rule cannot say, it says less of than the check did: `repeated_message` also weighed the wording two messages
shared, and a rule only their spacing; `reported_against_world` reviewed a commitment still reported open after the
answer, which a rule writes as its own count. Whether a message meant something (a thank-you or a chase) is a
judgement no count makes; a rule that might be wrong about it is `severity: review`.

## Checking a file without running it

`minutehand validate agent.yaml scenario.yaml` reads every rule as a run would: an unknown field, an anchor a rule's
`each` does not have (`answer` in a rule read once), `moment` without `at`, a fact counted where it cannot be
(`follow_ups` for `each: run`), a rule with no bound, two rules with one id, a pattern there is not, and a person no
scenario has, each named
with its place. Given an agent file and scenarios together, it also reads the rules each scenario would be judged by,
refusing an `assess_off` that names no rule. A run refuses the same before it starts, and refuses a rule whose id is
the id of a check (`expectations`, `near_miss_name`, an agent's own).

## What is recorded

Every run's result says what judged it (`RunResult.assessed_by`: `items`, the assessment of the agent's effects, on
every run; `review` when the reviewer read them; then each rule's id, `expectations`, `near_miss_name`, each of the
agent's own checks), so a reader of a run, or of its JSON, sees what was and was not asked of it. Each finding of the
assessment is in the read model's `findings` with `assessed_kind`, `item_kind`, `against`, `calls` and `judged_by`
(`docs/querying.md`).
