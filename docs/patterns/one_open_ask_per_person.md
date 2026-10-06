# One open ask per person

**Found by:** `repeated_message`, `duplicate_ticket`, `kept_chasing_after_done`.

## The failure

The agent sends the same question twice, minutes apart, because two parts of it each decided to ask. Or it files the same piece of work as two tickets. Or it keeps chasing a person about something they already answered or finished.

## The design

Before asking anyone anything, the agent looks at what is already open with that person: questions not yet answered, tickets they hold. A new ask that matches an open one is merged into it, not sent. An answer or a finished ticket closes the matching ask, and nothing more is sent about it.

A reference agent runs every outgoing question past a judge that compares it with the questions already open with the same person, and drops or merges duplicates.

## What to look for in your own agent

- Is there one place that knows every open ask per person, or does each part of the agent keep its own?
- When a reply arrives, what marks the ask as answered?
- `repeated_message` flags, for review, two messages to the same place minutes apart on the run's clock that share the wording particular to an ask (template wording the agent uses everywhere is discounted). `duplicate_ticket` fails the same title filed twice in one project while the first is open. `kept_chasing_after_done` flags a message threaded under an answered ask, or naming a finished ticket.
