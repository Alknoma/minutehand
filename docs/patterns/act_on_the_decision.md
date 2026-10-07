# Act on the decision

**Found by:** a rule of the team's own (`docs/assessments.md`), such as `count: {writes: {gated: true}}, at_most: 0`.

## The failure

The agent asks a person to approve an operation in its own product (send the report, book the venue, spend the
money) and then does it anyway: before the person decided, after they rejected it, or after it took the request
back without an answer. The approval was a formality the agent did not wait on.

## The design

A gated operation is held behind its approval item, keyed by the operation's id. The agent goes ahead only when the
item is decided by a decision that permits it. A rejection closes the operation: the agent tells whoever is owed an
answer that it was turned down, with the reason, and does not retry it under another name. A request it withdraws
is a decision it made for the person, and the operation stays held.

## What to look for in your own agent

- Where does the code that performs the operation read the approval's state? If it reads only "a decision exists",
  a rejection lets it through.
- Is the operation's id the same in the approval and in the call that performs it? Without that, nothing can tell
  which approval a call needed.
- The fact `writes` with `gated: true` is the first call of the agent's that carries the gated id while the item was
  pending, rejected or withdrawn; a rule with `at_most: 0` fails it. It is known only when the inbox declares where its
  items name what they gate (`pending.gates`).
