"""What the run loop raises. Adapters raise `AgentFailed`; the loop turns it into `StopReason.AGENT_FAILED`."""

from __future__ import annotations


class AgentFailed(Exception):
    """The agent could not be reached, exited with an error, or answered with something that is not a report."""


class RunRefused(Exception):
    """The run cannot start or continue as configured: a missing provider, an unsupported person, no state hooks."""
