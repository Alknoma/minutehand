"""When the agent wakes next: a sub-agent proposes it, plain code makes it safe.

    from wake import Open, next_wake
    moment = next_wake(open_items, now)        # a datetime, or None when nothing is open

The decider (`decide.py`) is a small Pydantic AI agent that reads what is open and proposes a moment and why. The
guard (`guard.py`) holds every proposal to the rules a proactive agent cannot break, whatever its model says."""

from wake.decide import Open, Proposal, next_wake

__all__ = ["Open", "Proposal", "next_wake"]
