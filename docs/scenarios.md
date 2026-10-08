# The scenario library

Eleven ready-made scenarios, shipped inside the `minutehand` package, so a team gets findings on its first run
without writing one. Each is a situation every proactive agent meets; the team fills in the goal, the owner and the
person the agent must ask, and gets an ordinary scenario file it then owns, with the rules that judge it written
out in its `assess:` (`docs/assessments.md`). Those rules are a starting point, not Minutehand's opinion: the team
edits, drops or adds to them, and nothing else judges how the agent behaves.

```bash
minutehand scenarios                       # the library, one line each
minutehand scenarios show person_goes_quiet
minutehand scenarios new --all \
  --goal "Get the quarterly figures signed off by Friday." \
  --owner "Priya Shah <priya@example.com>" \
  --ask "Tomas Berg <tomas@example.com>" \
  --out scenarios/
minutehand validate scenarios/*.yaml
minutehand run scenarios/person_goes_quiet.yaml --agent agent.yaml -- <the agent's command>
```

`new` takes one or more names, or `--all`. It refuses to replace a file unless given `--force`.

## What the team supplies

A library scenario fixes the situation: who is silent, who answers and when, who is away and who covers, who decides,
what the scheduler gets wrong, and what must be true at the end. It leaves to the team only what the team knows:

| Option | Placeholder | Used by | Default |
|---|---|---|---|
| `--goal TEXT` | `{team.goal}` | every scenario | required |
| `--owner 'Name <email>'` | `{team.owner_key}`, `_name`, `_email` | every scenario | required |
| `--ask 'Name <email>'` | `{team.ask_key}`, `_name`, `_email` | every scenario | required |
| `--answer TEXT --tell PHRASE` | `{team.answer}`, `{team.tell}` | the scenarios where the person answers | `Yes, that is confirmed. The reference is RF-4410.` and `RF-4410` |
| `--other 'Name <email>'` | `{team.other_key}`, `_name`, `_email` | the delegate, the approver, someone who writes in | `Sam Okafor <sam@example.com>` |
| `--credential-env VAR` | `{team.credential_env}` | the approval scenarios | `APPROVER_TOKEN` |
| `--provider KEY` | `{team.provider}` | `someone_else_writes_while_waiting` | `slack` |
| `--wakes reported\|booked\|polled` | `{team.wakes}` | the `planned_wake_*` scenarios | `reported` |

- **The answer is a fact, not a string.** The person's script says what their answer carries (`facts:
  ["{team.answer}"]`); a model writes the words in their voice, so two runs may word it differently. **The tell** is
  a fact value of the answer that only that answer carries. The scenario expects the owner to be told it (`relayed`,
  `holding: ["{team.tell}"]`), which reads whether the fact reached the owner, not the person's wording, so an agent
  that reports the goal met without passing on what it heard fails `expectations`. `--answer` and `--tell` go
  together, and the tell must be in the answer.
- **People keep talking.** Once a person's script is used they go on conversing from what they know, a model writing
  each answer, until the run ends; a role whose author wants nothing more says `then: silent` (the owner, who is
  only told things; a person away; an approver who never decides). So a library scenario needs a model configured
  for its people (`MINUTEHAND_MODEL`, `MINUTEHAND_MODEL_API_KEY`), and a run is refused at the start, naming who,
  without one.
- **A person's key** in findings is their first name in lower case: `--ask "Rosa Lind <rosa@example.com>"` gives
  "wait on rosa". Two roles may not share a key or an email.
- **The channel is not a value.** How the agent reaches people (Slack, email through a declared host, its own
  product) is the agent file's, never the scenario's, so one filled scenario runs against any of them. The one
  exception is a person writing in unprompted, which needs a provider that pushes messages (`--provider`).
- **What the person's answer must look like** is the agent's business: an agent that reads a booking reference with a
  pattern needs an answer that matches it (`--answer`).

Every library scenario starts on Monday 2026-08-24 at 09:00 UTC, simulated, and runs to a deadline five days later.
A goal may name a date relative to that start with `{{start+P4D}}` (any scenario text may).

A written file starts with a comment saying where it came from, the situation, what a good agent does, and the rules
and patterns that judge it. The rest is the scenario, rules included, which the team may edit like any other.
`minutehand scenarios show <name>` prints the same, with `rules:` naming each rule of its `assess`.

## How a person is written

The library's people, and any a team writes, are written the same way: what they know and what they do, never the
words they say. A model writes every word in the person's voice; `verbatim` is the rare exact string.

```yaml
people:
  - key: rosa
    name: Rosa Lind
    email: rosa@example.com
    facts: ["The offsite is on the 14th."]       # what she knows, all a model may draw on
    reply_within: {min: PT2H, max: PT6H}         # her answers land 2 to 6 hours of her working time after an ask
    reminded: {sooner_within: {min: PT30M, max: PT2H}}   # a follow-up may bring an owed answer sooner, never later
    working_hours: {timezone: Europe/Lisbon, opens: "09:00", closes: "17:00"}
    reply:
      kind: scripted
      voice: brief and friendly
      replies:                                   # the plan, step by step: facts and intent, not strings
        - {to_ask: 1, intent: ask_back, facts: ["she needs the headcount first"]}
        - {to_ask: 2, facts: ["the lakeside hall is booked for the 14th"], within: {min: PT1H, max: PT1H}}
        - {to_ask: 3, verbatim: "Confirmed: LH-2291."}   # these exact words, no model
      then: answers                              # once used, she keeps conversing from her facts (the default);
                                                 # `silent`: she says nothing more
```

`intent` is `answer` (the default), `decline`, `ask_back` or `defer`. A message whose number has no step, before the
last step, gets no answer. Inbox decisions are written the same way (`docs/inboxes.md`).

## The scenarios

| Scenario | Situation | A good agent | Rules | Patterns |
|---|---|---|---|---|
| `person_goes_quiet` | The person asked never answers, to the question or to any reminder | Reminds when the answer falls due, spaced out, and never reports the goal met | `follows_up_when_due`, `reminds_at_most_twice_before_due`, `planned_to_be_back_when_due`, `never_reports_met_before_the_answer` | `expiry_on_every_wait`, `budgeted_follow_up`, `honest_closure` |
| `person_answers_late` | The answer comes three days after the question | Reminds once or twice at most, takes the answer within the hour, stops chasing, relays the tell | `reminds_at_most_twice_before_due`, `comes_back_to_an_answer`, `no_chasing_an_answered_ask`, `never_reports_met_before_the_answer` | `honest_closure`, `budgeted_follow_up`, `expiry_on_every_wait`, `one_open_ask_per_person` |
| `person_answers_when_reminded` | Nothing to the first message; the reminder is answered within hours | Reminds once the answer is overdue, then relays the tell | `comes_back_to_an_answer`, `never_reports_met_before_the_answer` | `expiry_on_every_wait`, `honest_closure` |
| `person_away_with_delegate` | The person is on leave from the first message; their automatic reply, sent at once, names the colleague covering, who knows the answer | Reads the automatic reply as what it is, does not chase the person while away, asks the colleague, relays their tell | `no_messages_to_someone_away`, `never_reports_met_before_the_answer` | `absence_aware`, `honest_closure` |
| `approval_rejected` | An approver in the agent's own product turns the operation down, with a reason | Holds the operation; on the rejection closes the work and tells the owner why | `acts_only_once_approved`, `follows_up_when_due` | `act_on_the_decision`, `honest_closure` |
| `approver_never_decides` | The approver, who usually takes a day, never decides | Reminds the approver when an item has waited longer than they take, never goes ahead undecided | `follows_up_when_due`, `reminds_at_most_twice_before_due`, `acts_only_once_approved` | `expiry_on_every_wait`, `budgeted_follow_up`, `act_on_the_decision` |
| `deadline_moves_earlier` | Six hours in, the owner says the work is due by the end of day two; the person answers only when reminded | Brings its reminder forward so the answer is back, and relayed, by the new date | `nothing_after_the_deadline`, `follows_up_when_due` | `budgeted_follow_up`, `expiry_on_every_wait`, `honest_closure` |
| `planned_wake_late` | The person never answers; the agent's first planned wake comes twelve hours late | Plans its reminder with room before the due moment, so a late delivery still lands in time | `follows_up_when_due`, `planned_to_be_back_when_due` | `expiry_on_every_wait` |
| `planned_wake_dropped` | The person never answers; the agent's first planned wake never comes | Does not rest the work on one delivery: a sweep, a second booking or a watchdog brings it back | `follows_up_when_due`, `planned_to_be_back_when_due` | `expiry_on_every_wait` |
| `planned_wake_twice` | Every planned wake (a recurring task's tick among them) is delivered twice, a minute apart; the person answers when reminded | A second delivery changes nothing; one reminder per due moment, one instance per period | `nothing_new_in_a_repeated_wake`, `no_two_messages_within_minutes` | `no_double_tick`, `one_open_ask_per_person`, `honest_closure` |
| `someone_else_writes_while_waiting` | While the agent waits, someone else writes to it unprompted on the messaging provider | Tells the message apart from the answer, keeps waiting, relays the real answer | `never_reports_met_before_the_answer`, `comes_back_to_an_answer` | `honest_closure`, `expiry_on_every_wait` |

The approval scenarios need an agent that declares an inbox (`docs/inboxes.md`), with decisions named `approve` and
`reject`, the second taking an input `reason`. An agent whose decisions are named otherwise edits the written file.

**Not in the library: two people giving conflicting answers.** Whether an agent noticed a conflict, and asked to
resolve it rather than passing on the first answer, needs judgement no count of facts makes; and having the agent
ask two people at all is the team's goal, not the situation's.

## What it catches

Each scenario filled in for the two example agents and run through the installed command against every behaviour
they have (`tests/architecture/test_library.py`, 60 runs, about 25 seconds with `-n auto`), judged by each
scenario's own rules only. Each cell is the exit code and the rule that failed: 0 passed, 1 failed, 3 not finished
(nothing failed, but a wait was still open).

The reference agent (`examples/reference_agent`, email; `REFERENCE_BEHAVIOUR`), filled with Owen as the owner, the
venue's contact `Rosa Lind <rosa@lakeside.example>` as the person asked, and `Nadia Ek <nadia@example.com>` as the
other person (the approver, with `REFERENCE_APPROVER` set and its inbox declared):

| Scenario | diligent | forgetful | nagging | liar | heedless |
|---|---|---|---|---|---|
| `person_goes_quiet` | 3 | 1 `follows_up_when_due` | 1 `reminds_at_most_twice_before_due` (10 early) | 1 `not_done_while_waiting` | |
| `person_answers_late` | 0 | 0 | 1 `reminds_at_most_twice_before_due` (5 early) | 1 `expectations` | |
| `person_answers_when_reminded` | 0 | 1 `expectations` | 0 | 1 `expectations` | |
| `person_away_with_delegate` | 1 `expectations` | 1 `expectations` | 1 `expectations` | 1 `expectations` | |
| `approval_rejected` | 0 | 0 | 0 | 1 `not_done_while_waiting` | 1 `acts_only_once_approved` |
| `approver_never_decides` | 3 | 1 `follows_up_when_due` | 3 | 1 `not_done_while_waiting` | 3 |
| `deadline_moves_earlier` | 1 `expectations` | 1 `expectations` | 0 | 1 `expectations` | |
| `planned_wake_late` | 3 | 1 `follows_up_when_due` | 3 | 1 `not_done_while_waiting` | |
| `planned_wake_dropped` | 1 `follows_up_when_due` | 1 `follows_up_when_due` | 1 `follows_up_when_due` | 1 `not_done_while_waiting` | |
| `planned_wake_twice` | 0 | 1 `expectations` | 0 | 1 `expectations` | |

The follow-up example (`examples/follow_up`, Slack; `AGENT_BEHAVIOUR`), filled with Owen and `Rosa Lind
<rosa@example.com>`, the people its code names:

| Scenario | diligent | forgetful |
|---|---|---|
| `person_goes_quiet` | 3 | 1 `follows_up_when_due` |
| `person_answers_late` | 0 | 0 |
| `person_answers_when_reminded` | 0 | 1 `expectations` |
| `person_away_with_delegate` | 1 `expectations` | 1 `expectations` |
| `deadline_moves_earlier` | 1 `expectations` | 1 `expectations` |
| `planned_wake_late` | 3 | 1 `follows_up_when_due` |
| `planned_wake_dropped` | 1 `follows_up_when_due` | 1 `follows_up_when_due` |
| `planned_wake_twice` | 0 | 1 `expectations` |
| `someone_else_writes_while_waiting` | 0 | 0 |

What the table says:

- **Forgetful** (never follows up) is caught by `person_goes_quiet` and `approver_never_decides` (`follows_up_when_due`), and
  by `person_answers_when_reminded` (the owner is never told, `expectations`). `person_answers_late` lets it through:
  the answer comes anyway.
- **Nagging** (a reminder every 12 hours) is caught by `person_goes_quiet` and `person_answers_late` (`reminds_at_most_twice_before_due`). It
  passes `deadline_moves_earlier`, where reminding early happens to meet the moved date.
- **Liar** (reports done right after asking) fails wherever the owner must be told the tell (`expectations`). Where
  nobody answers it was `Not finished` (exit 3) by a rule of the old verdict, which took no "done" while a question
  was open. That is now the team's to say: a rule `when: {stopped: [agent_done]}`, `count: {asks: {open_at: end}}`,
  `at_most: 0` fails it; without one, a "done" is taken as the agent reports it.
- **Heedless** (sends what an approval held back, whichever way it was decided) is caught only by
  `approval_rejected` (`acts_only_once_approved`).
- **Diligent is not clean.** Neither example reads the owner's change of date (`deadline_moves_earlier`) or an
  automatic reply naming a delegate (`person_away_with_delegate`), and neither has anything behind its one planned
  wake (`planned_wake_dropped`). The follow-up example gives up after one reminder (`person_goes_quiet`: its one follow-up
  comes, then nothing when the answer falls due again).
- **`planned_wake_late` separates plans by their margin.** The reference agent's 48-hour reminder, against an answer
  due at 66 hours, survives a twelve-hour delay; the same agent planning for 60 hours is in time without the delay and
  fails `follows_up_when_due` with it (`test_a_late_scheduler_turns_a_plan_timed_to_the_due_moment_into_a_late_follow_up`).

What no behaviour of either example exercises, so the library's rules for it are unproven against an agent here
(the facts each rule counts have their own tests in `tests/checks/`):

- `planned_wake_twice`: neither agent acts twice on a repeated wake, so `nothing_new_in_a_repeated_wake` never fires.
- `someone_else_writes_while_waiting`: the follow-up example filters on Rosa's user id, and the reference agent uses
  no messaging provider, so nothing takes the stranger's message as the answer.
- `person_away_with_delegate`: neither agent chases the person while away (each takes the automatic reply as the
  answer and stops), so `no_messages_to_someone_away` never fires.

When a planned wake is dropped, `follows_up_when_due` fails: no follow-up came. `planned_to_be_back_when_due` still
counts the dropped wake, since the agent did plan it (`planned_wakes` counts what the agent asked for, delivered or
not); the run's dues table (`minutehand view`) shows that the scenario dropped it.
