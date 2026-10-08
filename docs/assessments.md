# Assessments: the team's own rules

Minutehand holds no opinion of how an agent should behave. When to follow up, how often, whether to acknowledge an
answer, when to escalate and to whom, what counts as nagging: each is a team's policy, and two teams with good agents
answer differently. A run records what happened. It is judged only by the rules the team writes, in YAML, in its own
files:

- `assess:` in the agent file: the team's policy, for every scenario;
- `assess:` in a scenario: rules for that situation; one with the id of an agent file's rule replaces it;
- `assess_off:` in a scenario: ids of the agent file's rules this scenario does not judge by.

`expect:` (what must be true of the world at the end) and `protected_names:` are the scenario author's words too, and
judge the run beside the rules. A team that needs more than the language says writes a check in Python
(`checks:` in the agent file), reading the same facts (`minutehand.checks.facts`).

A run whose files declare none of these is not judged: it reports the facts (the scorecard, every wait, every message)
and its verdict says so: `Not assessed: nothing judged this run, since neither the scenario nor the agent file
declares an assessment`. Its exit code is 5.

## A rule

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

What is an ask is the ledger's, read from the world alone (`checks/ledger.py`): a message the person has a reply to,
or any message to a `Silent` person. A message to the same person in the same conversation while an ask is open is a
follow-up on it, not an ask of its own.

`where: {person: [...], person_not: [...]}` picks among asks, hand-offs or people by who they are of.

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
| `due` | when the scenario says the person would have answered by: the longest delay of their `reply`, or a ticket's fate |
| `moment` | each of the rule's own `at` |
| `all_answered` | when the last of the run's asks was answered |

A rule is not read for a thing when a moment it names is not there (an answer never given, a scenario without a
deadline, `all_answered` while an ask is open) or comes after the run's end, and when it counts what the run did not
record: `planned_wakes` of a run that kept no table (a captured run, a standing world), or `writes: {gated: ...}` when
items were asked of people and none says what it holds back. The run's notes say how many times each
rule went unread, so a rule never passes by being skipped unseen.

### `when`: whether to read it at all

| Condition | Holds when |
|---|---|
| `open_at: <moment>` | the ask or hand-off was made and still unanswered at that moment |
| `answered: true` / `false` | it was answered, or the work finished, by the end |
| `stopped: [agent_done, wake_limit, deadline_passed, nothing_pending, agent_failed, closed, environment_failed]` | the run stopped one of these ways |

### `count`: which facts, between which moments

`since` and `until` are moments, both inclusive; absent, the start and the end. Exactly one kind of fact is counted:

| Fact | Each one is | Filters |
|---|---|---|
| `follow_ups` | a write of the agent's the person could see on the ask while it was open: a message to them or their delegate, a change to the ask's thread or ticket (an `ask` or `handoff` rule only) | none |
| `touches` | any write of the agent's on the ask's person, thread or ticket, answered or not: after `answer`, the agent coming back to it (`ask` or `handoff` only) | none |
| `messages` | a message the agent sent | `to`, `to_not` (person keys, `owner`, or `person`: the one the rule is read for), `in_thread` (under the ask's own message), `holding` (phrases, any case; `{ask.facts}`, a phrase of its own, is each fact the answer's script step carried, which a model put in the person's words, so a relay is read by the fact and never the wording (else the answer itself); `{ask.answer}` is the answer as the person worded it, `{person.key}` and `{person.name}` the person), `to_away` (to someone away then while a delegate covered; the message an absence starts with, which their automatic reply answers, is not) |
| `writes` | any change the agent made to the world | `things`, `things_not` (`message`, `ticket`, `comment`, `document`, `record`, `inbox_item`, `file`, `tool_call`, `stored`), `operations` (`create`, `update`, `delete`), `repeats_open_ticket` (a ticket filed with the title of one still open in the project), `in_repeated_wake` (in the second delivery of one wake), `gated` (going ahead with an operation an item in the agent's product held back, while pending, turned down or taken back) |
| `wakes` | a wake of the agent's | `changed_world`, `changed_commitments` |
| `planned_wakes` | a wake the agent asked for itself (reported, booked, its rhythm, its timer), at the moment it was due | none |
| `commitments` | a commitment the agent reported as a wake ended | `status` (`open`, `met`, `dropped`), `waiting_on` (people) |
| `asks` | an ask of the run, at the moment it was made | `of` (people), `open_at` (a moment) |
| `stored` | an item a host declared `store` holds at `until` (the run's end without it), counted at the moment its version there was written (`docs/capture.md`) | `host` (the declaration's host pattern), `collection` (its name), `values` (each field of the item, a dotted path, equal to the one given) |
| `replies` | a person's reply or decision that landed, at the moment it landed | `by` (people), `written` (`script`: a model, from a step of their script; `verbatim`: the step's exact words or a control; `conversing`: a model, from their own facts, with no script or once it was used; `automatic`: their automatic reply while away; `by_hand`: a harness speaking for them) |
| `memory` | a key of the agent's memory (`minutehand.agent.store`) holding a value at `until` (the run's end without it), counted at the moment that value was written; so `since` keeps only keys written from then on | `key` (exactly this key) or `prefix` (keys starting with it), either may hold `{person.key}`; `collection` (default `default`); `values` (each field of the value, a dotted path, equal to the one given: `{status: confirmed}`) |

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
| `acted_without_approval` | `count: {writes: {gated: true}}`, `at_most: 0` |
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

Every run's result says what judged it (`RunResult.assessed_by`: each rule's id, `expectations`, `near_miss_name`,
each of the agent's own checks), so a reader of a run, or of its JSON, sees what was and was not asked of it.
