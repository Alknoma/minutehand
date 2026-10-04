"""How long the agent took on each wait: the one reading of an obligation that the
scorecard and the time checks share, so they cannot disagree about a number."""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import AwareDatetime

from minutehand.domain.checks import Needs, Obligation, ObligationKind, RunView
from minutehand.domain.scenario import Model

GRACE = timedelta(hours=1)
"""How long after the moment it should act the agent may take before the time counts against it."""


class FollowUp(Model):
    """A wait that passed its expected date while still open, and what the agent did about it."""

    obligation: Obligation
    expired: AwareDatetime
    closes: AwareDatetime
    touch: int | None
    touched_at: AwareDatetime | None

    @property
    def gap(self) -> timedelta:
        """From the expiry to the follow-up, or to the close when there was none."""
        return (self.touched_at or self.closes) - self.expired

    @property
    def late(self) -> bool:
        return self.touch is None or self.gap > GRACE


class Reaction(Model):
    """A wait the world settled, and how long the agent took to come back to it."""

    obligation: Obligation
    settled: AwareDatetime
    touch: int | None
    touched_at: AwareDatetime | None
    ended: AwareDatetime

    @property
    def gap(self) -> timedelta:
        return (self.touched_at or self.ended) - self.settled

    @property
    def slow(self) -> bool:
        return self.gap > GRACE


def ended_at(view: RunView) -> datetime:
    """The last moment the run reached: its last event or its last wake, whichever is later."""
    moments = [e.sim_time for e in view.events] + [w.sim_time for w in view.wakes]
    return max(moments, default=view.scenario.starts_at)


def follow_up(o: Obligation, when: dict[int, datetime], ended: datetime) -> FollowUp | None:
    """None when the wait settled, or the run ended, before it was due."""
    closes = o.settled_at or ended
    if o.expected_by is None or o.expected_by >= closes:
        return None
    expired = max(o.expected_by, o.opened_at)
    after = sorted((when[s], s) for s in o.agent_touches if s in when and when[s] >= expired)
    first = after[0] if after else None
    return FollowUp(
        obligation=o, expired=expired, closes=closes,
        touch=first[1] if first else None, touched_at=first[0] if first else None,
    )


def reaction(o: Obligation, when: dict[int, datetime], ended: datetime) -> Reaction | None:
    """None when there is nothing to react to, or nothing the agent could touch to react.

    An obligation naming neither a person nor an entity (one read from an agent's own
    records rather than built by the ledger) has no reaction that can be timed.
    """
    if o.kind is ObligationKind.DATE or o.settled_at is None or (o.person is None and o.entity is None):
        return None
    touch = o.first_touch_after_settled
    return Reaction(
        obligation=o, settled=o.settled_at, touch=touch,
        touched_at=when[touch] if touch is not None and touch in when else None, ended=max(ended, o.settled_at),
    )


def blocked(view: RunView, needs: frozenset[Needs], check: str) -> list[str]:
    """What this check needs and the view does not carry. Non-empty means the check did not run."""
    missing: list[str] = []
    if Needs.OBLIGATIONS in needs and not view.obligations:
        missing.append(f"{check}: no obligations in the view; the ledger did not run or found nothing to wait on")
    if Needs.COMMITMENTS in needs and view.commitments is None:
        missing.append(f"{check}: the agent reported no commitments")
    if Needs.WAKES in needs and not view.wakes:
        missing.append(f"{check}: no wakes recorded")
    if Needs.CALLS in needs and view.unmatched_calls is None:
        missing.append(f"{check}: nobody recorded which calls reached no provider")
    return missing
