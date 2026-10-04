# Look at the world before asking the model

**Found by:** `idle_wake`.

## The failure

The agent wakes, hands the model its whole context, and learns that nothing changed: no reply, no ticket moved, nothing due. It costs a model call and a few seconds each time, and on a long run most wakes are of this kind.

## The design

On waking, the agent first answers "has anything I care about changed?" with plain code: new messages in the threads it is waiting on, tickets whose state differs from the last look, waits whose expected-by date has passed. Only when one of those is true does it involve the model, and then with only what changed.

A reference agent filters its waits to those actually stale before any model call, and runs a cheap preflight that returns early when none is.

## What to look for in your own agent

- Count the wakes that end with no write to the world and no change to what the agent is waiting on. `idle_wake` lists them.
- Is there code that runs before the first model call on a wake, and can it end the wake?
- A wake can be right to change nothing; the finding is for review. A run where most wakes are idle is the failure.
