# Follow-ups budgeted against the deadline

**Found by:** `acted_after_deadline`.

## The failure

The agent follows up on a fixed cadence that ignores the date the work is due: too often early on, too rarely near the end, and still working after the date has passed as if nothing had happened.

## The design

The agent plans its reminders across the time left before the deadline: fewer and gentler while there is room, closer together as the date approaches, and a clear report to the owner when the date is missed instead of carrying on.

A reference agent computes the next reminder from the time remaining and the number already sent, and escalates to the owner when the remaining time is shorter than the person usually takes to answer.

## What to look for in your own agent

- Is the deadline an input to when the agent next follows up, or only to what it writes?
- What does the agent do on the first wake after the deadline? A ticket filed or a document written then is work done too late; a message telling the owner the date was missed is the honest one.
- `acted_after_deadline` fails a wake that wrote tickets or documents after the deadline and asks for review of one that only sent messages.
