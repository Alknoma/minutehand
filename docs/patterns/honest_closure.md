# Honest closure

**Found by:** `expectations`.

## The failure

The agent reports the goal done when the world says otherwise: the ticket the goal depended on was never filed, the approval was declined, the document was never shared. Its last message said "done", and that was taken as the answer.

## The design

Whether the work is finished is decided from the state of the world, by plain checks against what the goal requires, not from the agent's own account of its last step. A goal that cannot be closed is reported as open, with what is missing.

A reference agent evaluates closure against the recorded state of every piece of work the goal depends on before it may mark the goal complete.

## What to look for in your own agent

- What decides that a goal is done? If it is the model's last message, that is the failure.
- Can the agent say, for each thing the goal needs, where in the world it can be seen to be true?
- `expectations` fails each thing the scenario says must be true of the world and is not.
