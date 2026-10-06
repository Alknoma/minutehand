# Know who is away

**Found by:** `chased_absent_person`.

## The failure

The agent chases someone who is on leave while a colleague covers for them. The reminders land in an inbox nobody reads, the wait runs on, and the person who could answer is never asked.

## The design

The agent knows who is away and until when: from an out-of-office reply, a calendar, or a directory. Before asking or chasing someone, it checks. If they are away it either extends the wait to their return or goes to their delegate, and says which it chose.

A reference agent runs an absence filter over every follow-up before it is sent, and reroutes to the named cover when one exists.

## What to look for in your own agent

- Does anything between "decide to chase" and "send" look at whether the person is available?
- When an auto-reply says "I am away until …, contact …", is that read and kept, or treated as an answer?
- `chased_absent_person` lists every message sent inside an absence that had a delegate.
