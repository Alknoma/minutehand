# One open ask per person

**Found by:** a rule of the team's own (`docs/assessments.md`), such as `count: {writes: {repeats_open_ticket: true}}, at_most: 0`, or `each: person`, `count: {messages: {to: [person]}}`, `gap_at_least: PT5M`.

## The failure

The agent sends the same question twice, minutes apart, because two parts of it each decided to ask. Or it files the same piece of work as two tickets. Or it keeps chasing a person about something they already answered or finished.

## The design

Before asking anyone anything, the agent looks at what is already open with that person: questions not yet answered, tickets they hold. A new ask that matches an open one is merged into it, not sent. An answer or a finished ticket closes the matching ask, and nothing more is sent about it.

A reference agent runs every outgoing question past a judge that compares it with the questions already open with the same person, and drops or merges duplicates.

## What to look for in your own agent

- Is there one place that knows every open ask per person, or does each part of the agent keep its own?
- When a reply arrives, what marks the ask as answered?
- A rule with `gap_at_least` on messages to one person flags two minutes apart on the run's clock; whether they asked the same thing is in their words, so such a rule is best `severity: review`. `repeats_open_ticket` is a ticket filed with the title of one still open in the project. `messages` with `in_thread: true` since `answer` is a message under an ask already answered.
