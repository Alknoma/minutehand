"""When the agent next wants to be woken.

    from minutehand_agent import wake

    scheduler.schedule(expected_by, follow_up)   # the agent's own scheduler, which does the work in production
    wake.at(expected_by)                         # tells Minutehand; does nothing in production
    wake.clear()                                 # nothing more is due: no next wake

In production (MINUTEHAND_ON unset) both do nothing. Under Minutehand the moment is recorded in the run as the agent's
own plan, in the wake it was said in, and is its next wake: the last one said in a wake replaces any it said before
and the `next_wake` of its report in that wake, and Minutehand wakes it then (through its `wake_url`, as a `marked`
or `reported` agent file declares). A moment must carry its timezone; take it from the wake's `now`, never the
machine's clock, which a run does not move.
"""

from __future__ import annotations

from datetime import datetime

from minutehand_agent import _wire


def at(when: datetime) -> None:
    """Ask to be woken at `when`, an aware moment."""
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError(f"wake.at needs a moment with its timezone, not {when.isoformat()}")
    if _wire.on():
        _wire.call("wake", {"at": when.isoformat()})


def clear() -> None:
    """Nothing more is due: forget the next wake asked for."""
    if _wire.on():
        _wire.call("wake", {"at": None})
