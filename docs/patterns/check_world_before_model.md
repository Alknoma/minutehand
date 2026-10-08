# Look at the world before asking the model

**Found by:** a rule of the team's own (`docs/assessments.md`), such as `count: {wakes: {changed_world: false, changed_commitments: false}}, at_most: 0, severity: review`; the scorecard counts `idle_wakes` for every run.

## The failure

The agent wakes, hands the model its whole context, and learns that nothing changed: no reply, no ticket moved, nothing due. It costs a model call and a few seconds each time, and on a long run most wakes are of this kind.

## What the finding says, and what it does not

The facts state what was observed and nothing more: the wake changed nothing in the world and nothing the agent was waiting on, and, when the run received the agent's telemetry, how many model calls were received or recorded during it (placed by the real moment each span started). It does not know why the wake was idle; that is for you to read in the wake's trace. A wake that changed nothing and made no model call cost nothing worth fixing; whether to count it is the team's rule.

## Why a wake changes nothing, and what each cause needs

| What the trace shows | The cause | The design |
|---|---|---|
| Model calls that read the same waits and conclude "nothing yet" | The agent asks the model whether anything changed | On waking, answer "has anything I care about changed?" with plain code: new messages in the threads it waits on, tickets whose state differs from the last look, waits whose expected-by date has passed. Involve the model only when one is true, and then with only what changed. |
| The wake came at a moment nothing was due | The agent asked to be woken too early, or on a fixed rhythm | Book the next wake at the earliest expected-by date among its open waits (`expiry_on_every_wait`), not on a timer. |
| A reply or an event arrived and the agent did nothing with it | The agent dropped the news | That is not idleness: read the reply handling. A rule counting `touches` after `answer` fails it when an answer goes unacted on. |
| The agent decided, rightly, to keep waiting | Nothing; the wake was cheap | Nothing to change. A run where most wakes are idle is the failure, not one idle wake. |

A reference agent filters its waits to those actually stale before any model call, and runs a cheap preflight that returns early when none is.

## What to look for in your own agent

- Count the wakes that end with no write to the world and no change to what the agent is waiting on. The scorecard counts them (`idle_wakes`), and a rule over `wakes` with `changed_world: false` names each.
- Is there code that runs before the first model call on a wake, and can it end the wake?
- Does the agent's next wake come from its waits' dates, or from a fixed interval?
