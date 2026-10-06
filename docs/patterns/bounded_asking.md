# Bounded asking

**Found by:** no deterministic check yet.

## The failure

The agent keeps asking for input it may never get: the same person, a third and fourth time, or a new person each time the last one did not answer. The work never moves to a version it could deliver.

## The design

Asking has a limit. After a fixed number of attempts on one need, the agent stops asking, delivers the best version it can with what it has, and says plainly what is missing and from whom.

A reference agent counts its attempts on each unmet need and pivots to a best-effort delivery once the count passes a threshold.

## What to look for in your own agent

- Is there a count of attempts per need, and does anything read it?
- What happens on the attempt after the limit: another ask, or a delivery?
- The scorecard's `burden` gives messages per person and how many of them chased an ask still open; a person with many follow-ups and no answer is where to look.
