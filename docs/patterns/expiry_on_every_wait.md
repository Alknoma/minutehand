# An expiry on every wait

**Found by:** a rule of the team's own (`docs/assessments.md`), such as `each: ask`, `when: {open_at: due}`, `count: {follow_ups: {}, since: due, until: due+PT1H}`, `at_least: 1`; and `when: {answered: true}`, `count: {touches: {}, since: answer, until: answer+PT1H}`, `at_least: 1` for an answer acted on.

## The failure

The agent asks someone for something and then waits on it with no date. Silence is never noticed, or is noticed only when something unrelated wakes the agent. On a captured run a reminder went out 33 hours after the wait it chased had expired: the agent did follow up, so nothing looked broken, and a day and a half was lost.

A single reminder is not a wait handled either. An agent that chases a silent person once, early, and then stops has followed up once and abandoned the wait: the reminder gave the person their usual time to answer again, and when that passed too, nothing came.

The mirror image is the answer that lands and sits: the person replied, and the agent came back to it hours later, or never, because nothing woke it on the reply.

## The design

Every wait the agent opens carries an expected-by date, written at the moment it asks: when this person usually answers, or when the work is due. A follow-up moves the date on by the same allowance, so the wait stays dated until it is answered or closed on purpose. The agent's scheduler wakes it on the earliest expected-by date it holds, not on a fixed rhythm. A reply or a finished ticket is a wake of its own.

A reference agent keeps an expected-by date on every blocker and a single "next stale check" time derived from them; its scheduler books the wake from that time.

## What to look for in your own agent

- Where a wait is recorded, is there a date on it? A list of "pending questions" with no time beside each is the failure.
- What wakes the agent when a date passes? If the answer is "the next poll", the follow-up is as late as the poll interval.
- Does a reply wake the agent, or is it found on the next unrelated wake?
- When a wait is due, and how soon after it a follow-up must come, is the team's to say in its rules: the anchor `due` is when the scenario's person would have answered by, and `ask+P1D` any offset the team keeps. `follow_ups` counts every write the person could see while the wait was open; reading the channel is not one. `planned_wakes` counts the wakes the agent itself asked for, so a rule can say the agent's own plan, not luck, brought it back.
