# An expiry on every wait

**Found by:** `late_follow_up`, `no_follow_up`, `slow_to_react`.

## The failure

The agent asks someone for something and then waits on it with no date. Silence is never noticed, or is noticed only when something unrelated wakes the agent. On a captured run a reminder went out 33 hours after the wait it chased had expired: the agent did follow up, so nothing looked broken, and a day and a half was lost.

The mirror image is the answer that lands and sits: the person replied, and the agent came back to it hours later, or never, because nothing woke it on the reply.

## The design

Every wait the agent opens carries an expected-by date, written at the moment it asks: when this person usually answers, or when the work is due. The agent's scheduler wakes it on the earliest expected-by date it holds, not on a fixed rhythm. A reply or a finished ticket is a wake of its own.

A reference agent keeps an expected-by date on every blocker and a single "next stale check" time derived from them; its scheduler books the wake from that time.

## What to look for in your own agent

- Where a wait is recorded, is there a date on it? A list of "pending questions" with no time beside each is the failure.
- What wakes the agent when a date passes? If the answer is "the next poll", the follow-up is as late as the poll interval.
- Does a reply wake the agent, or is it found on the next unrelated wake?
- `late_follow_up` gives the gap in hours; `no_follow_up` lists waits that expired and were never touched again; `slow_to_react` lists answers that sat.
