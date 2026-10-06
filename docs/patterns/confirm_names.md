# Confirm names

**Found by:** `near_miss_name`.

## The failure

The agent acts on a name it guessed. On a captured run, given a goal naming one company, it researched a different company whose name is one letter away, then filed two tickets and sent two messages under that name. Every step after the first was internally consistent and wrong.

## The design

A name that matters (a company, a product, a person) is carried exactly as it was given, from the goal through every message and ticket. When the agent is unsure which thing a name refers to, it asks before acting, and an assumption it makes is stated, not buried.

No reference implementation exists yet; the captured run is the evidence that one is needed.

## What to look for in your own agent

- Is the goal's text available verbatim at the step that writes a ticket or a message, or only a summary of it?
- When a search returns something whose name differs from the one asked for, does the agent notice?
- `near_miss_name` fails every write that spells a protected name one letter off.
