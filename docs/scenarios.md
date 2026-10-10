# The scenario library

Eleven ready-made worlds, shipped inside the `minutehand` package, so a team gets findings on its first run without
writing one. Each is a situation in the world every proactive agent meets: a person who never answers, one who is
away with someone covering, an approver who never decides, a scheduler that drops a wake. The team fills in who its
people are and gets an ordinary scenario file it then owns.

A library scenario is a world and nothing more. The agent brings its own work (its prompt, and the state and items it
sets), so the file hands it none: no goal, no owner, no expectation of what it achieves, no rule about how it should
work. It says how long to watch the agent (`runs_for`), and every run is assessed, automatically, against the agent's
own instructions and the world the file declares (`docs/assessments.md`), with nothing more to write.

```bash
minutehand scenarios                       # the library, one line each
minutehand scenarios show person_goes_quiet
minutehand scenarios new --all \
  --person "Tomas Berg <tomas@example.com>" \
  --other "Lena Fox <lena@example.com>" \
  --out scenarios/
minutehand validate scenarios/*.yaml
minutehand run scenarios/person_goes_quiet.yaml --agent agent.yaml -- <the agent's command>
```

`new` takes one or more names, or `--all`. It refuses to replace a file unless given `--force`.

## What the team supplies

A library scenario fixes the situation. It leaves to the team who its people are:

| Option | Placeholder | Used by | Default |
|---|---|---|---|
| `--person 'Name <email>'` | `{team.person_key}`, `_name`, `_email` | the person the situation is about | required |
| `--other 'Name <email>'` | `{team.other_key}`, `_name`, `_email` | who covers, who decides, who writes in | `Sam Okafor <sam@example.com>` |
| `--knows TEXT` | `{team.knows}` | the fact the person holds, which their answer carries | `The reference is RF-4410.` |
| `--credential-env VAR` | `{team.credential_env}` | the approval scenarios | `APPROVER_TOKEN` |
| `--provider KEY` | `{team.provider}` | the scenarios where someone writes in unprompted | `slack` |
| `--wakes reported\|booked\|polled` | `{team.wakes}` | the `planned_wake_*` scenarios | `reported` |

- **People are described, then played.** Each person has a `profile` (who they are, how they work) and `facts` (what
  they know); a model writes every word they say from those, so two runs may word it differently. A person's script
  says when they answer and what the answer carries, never the words.
- **A person's key** in findings is their first name in lower case: `--person "Rosa Lind <rosa@example.com>"` gives
  "rosa". The two people may not share a key or an email.
- **The channel is not a value.** How the agent reaches people (Slack, email through a declared host, its own product)
  is the agent file's, never the scenario's, so one filled scenario runs against any of them. The exception is a
  person writing in unprompted, which needs a provider that pushes messages (`--provider`).
- **A model plays the people,** so a library scenario needs one configured (`MINUTEHAND_MODEL`,
  `MINUTEHAND_MODEL_API_KEY`); a run is refused at the start, naming who, without one.

Every library scenario starts on Monday 2026-08-24 at 09:00 UTC, simulated, and watches the agent for thirty days.
Any text may name a date relative to that start with `{{start+P4D}}`.

A written file starts with a comment saying where it came from, the situation, and that it is a world. The rest is the
scenario, which the team may edit like any other: add its people's profiles, more people, the starting state of its
services, a longer window.

## How a person is written

The library's people, and any a team writes, are written the same way: what they know and what they do, never the
words they say. A model writes every word in the person's voice; `verbatim` is the rare exact string.

```yaml
people:
  - key: rosa
    name: Rosa Lind
    email: rosa@example.com
    profile: Runs the venue's bookings; brief, and busy at weekends.   # who she is
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
last step, gets no answer.

Anything else a person does to an item, beyond answering in words, is a **take**: a button or a form on a message, a
decision in the agent's product, a ticket moved, commented on or deleted, an invitation answered. A take names the
move, and optionally the provider and the nth item there, when, and what it carries:

```yaml
    takes:
      - {take: Approve, nth: 2}                                 # the button on her second ask, wherever it went
      - {provider: approvals, take: reject, facts: ["the budget is spent"]}   # a model words the reason
      - {provider: jira, take: done, after: P3D}                # each Jira ticket handed to her, three days on
```

Takes that name the same item are its moves in order: `[{provider: approvals, take: ask_back}, {provider:
approvals, take: approve}]` asks back on the first decision and approves the resubmission; the last holds from
then on.

Without a take, a model picks among what the item offers when the scenario plays its provider (`transitions_on`,
and always for the agent's product); a delete, a comment or a note is never picked unprompted. `minutehand migrate
<file>` rewrites an older scenario's `ticket_fates`, `press`, `presses_every` and scripted `decisions` as takes
(`docs/design-transitions.md`, phase 4).

## The scenarios

| Scenario | Situation |
|---|---|
| `person_goes_quiet` | The person who holds what the agent needs never answers, to a question or to any reminder |
| `person_answers_late` | The person answers, but three days after they are asked |
| `person_answers_when_reminded` | Nothing to the first message; a reminder is answered within hours |
| `person_away_with_delegate` | The person is on leave from the first message; their automatic reply names who covers, and the cover holds what the agent needs |
| `someone_else_writes_while_waiting` | While the agent waits on the person, a second writes in unprompted, asking whether the thing is still happening |
| `date_moves_earlier` | Six hours in, a person writes to say the date the work hangs on moved two days earlier |
| `approval_rejected` | An approver in the agent's own product turns an item down, with a reason |
| `approver_never_decides` | An approver in the agent's own product never decides: every item waiting on them stays pending |
| `planned_wake_late` | The first wake the agent plans for itself arrives twelve hours late |
| `planned_wake_dropped` | The first wake the agent plans for itself is never delivered |
| `planned_wake_twice` | Every wake the agent plans for itself is delivered twice, a minute apart |

The approval scenarios need an agent that declares an inbox (`docs/inboxes.md`), with decisions named `approve` and
`reject`, the second taking an input `reason`. An agent whose decisions are named otherwise edits the written file.

## What it catches

Each scenario filled in for the two example agents and run through the installed command against every behaviour
they have (`tests/architecture/test_library.py`, 60 runs, about 25 seconds with `-n auto`). Each scenario is a world
with no rule of its own, so each cell is what the automatic assessment alone failed (exit code and check), with no
model configured for the reviewer: 0 passed, 1 failed.

The reference agent (`examples/reference_agent`, email through its own mail API; `REFERENCE_BEHAVIOUR`), filled with
the venue's contact `Rosa Lind <rosa@lakeside.example>` as the person and `Nadia Ek <nadia@example.com>` as the other
(the approver, with `REFERENCE_APPROVER` set and its inbox declared). Its own work is in its configuration
(`REFERENCE_JOB`):

| Scenario | diligent | forgetful | nagging | liar | heedless |
|---|---|---|---|---|---|
| `person_goes_quiet` | 1 `duplicate` | 0 | 1 `duplicate` | 0 |  |
| `person_answers_late` | 0 | 0 | 1 `duplicate` | 0 |  |
| `person_answers_when_reminded` | 0 | 0 | 0 | 0 |  |
| `person_away_with_delegate` | 0 | 0 | 0 | 0 |  |
| `approval_rejected` | 1 `duplicate` | 0 | 1 `duplicate` | 0 | 1 `duplicate` |
| `approver_never_decides` | 1 `duplicate` | 0 | 1 `duplicate` | 0 | 1 `duplicate` |
| `planned_wake_late` | 1 `duplicate` | 0 | 1 `duplicate` | 0 |  |
| `planned_wake_dropped` | 0 | 0 | 0 | 0 |  |
| `planned_wake_twice` | 0 | 0 | 0 | 0 |  |

The follow-up example (`examples/follow_up`, Slack; `AGENT_BEHAVIOUR`), filled with `Rosa Lind <rosa@example.com>`,
the person its code names:

| Scenario | diligent | forgetful |
|---|---|---|
| `person_goes_quiet` | 0 | 0 |
| `person_answers_late` | 0 | 0 |
| `person_answers_when_reminded` | 0 | 0 |
| `person_away_with_delegate` | 0 | 0 |
| `someone_else_writes_while_waiting` | 0 | 0 |
| `date_moves_earlier` | 0 | 0 |
| `planned_wake_late` | 0 | 0 |
| `planned_wake_dropped` | 0 | 0 |
| `planned_wake_twice` | 0 | 0 |

What the tables say:

- **Repeating itself with nothing new is caught wherever it happens.** The reference agent sends the same reminder
  every two days (diligent) or every twelve hours (nagging); over a thirty-day window, to someone who never answers
  or never decides, every repeat after the first is a `duplicate`, and each reminder before the person's declared
  reply window could pass is for review (`inside_reply_window`, not shown).
- **Forgetful passes, rightly.** Not following up is no fact about the world: an agent's own instructions would have
  to say it should, and the reviewer reads those.
- **Liar and heedless pass the deterministic checks.** The liar reports done and stops, which in a world is no
  violation on its own; the heedless agent goes ahead after a rejection in its own product, which only its
  instructions, or the reviewer reading them (`--judge`), establish. Neither example's model is told its work in a
  system prompt the run can read, so nothing deterministic has the words to hold either to.
- **The follow-up example sends each message once**, so nothing repeats; its one reminder before Rosa's window could
  pass is for review.

Messages an agent sends through its own declared API (an `acknowledge` host with a `message` reading) are assessed
like any provider's: as emails when the reading names a subject, as chat messages otherwise.
