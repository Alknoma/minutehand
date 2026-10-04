# One instance per period

**Found by:** no deterministic check yet.

## The failure

A recurring task (the weekly report, the Monday check-in) is done twice in one period: a retry after a crash, two schedulers, or a wake that did not know the last one had already done it.

## The design

A recurring task has one instance per period, keyed by the period itself ("week 37"), and the agent checks for that instance before doing the work. A second attempt in the same period finds it and stops.

A reference agent guards each cadence tick with a check for an existing instance in the current period before it creates anything.

## What to look for in your own agent

- Is a recurring task's instance identified by its period, or only by the moment it ran?
- What happens if the agent crashes after doing the work and before recording it?
