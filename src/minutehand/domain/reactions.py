"""When the agent learned of each change someone else made in the world, and when it acted on it.

A proactive agent is measured by time: how long a decision, an answer or an ask-back sat unseen, and how long after
seeing it the agent moved. This is that, as facts and nothing more: no lag here is called late, for nothing declares
what late would be (`checks.items` reads windows the scenario declares). For each change by a person, a declared
service's own actor or its timer (`domain.transitions.moves`):

- **seen**: the first moment the agent could know it. A wake that carried it (a reply wakes the agent), or the first
  of the agent's own reads, answered 2xx, of the item it changed. A read that failed is no read: it does not count.
- **acted**: the agent's first move in the world after it saw the change: a message, a write, a transition.

A change the agent never saw, or never acted on after seeing, says so (None)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from pydantic import Field

from minutehand.domain.model import Model
from minutehand.domain.transitions import Transition, moves
from minutehand.domain.world import Actor, EntityKind, RecordedCall, WorldEvent

ANSWERED = range(200, 300)
REPLY_WAKE = "person_replied"


class Reaction(Model):
    change: Transition = Field(description="What someone else changed, and when")
    seen_at: datetime | None = Field(description="When the agent could first know it; None: it never could")
    seen_by: str | None = Field(description="How: `wake` (a reply woke it) or `read` (its own read showed it)")
    seen_call: int | None = Field(default=None, description="The read that showed it (calls.call_id), if a read did")
    acted_at: datetime | None = Field(description="Its first move in the world after it saw it; None: none came")
    acted: Transition | None = Field(default=None, description="That move")

    @property
    def unseen(self) -> timedelta | None:
        return self.seen_at - self.change.at if self.seen_at is not None else None

    @property
    def to_act(self) -> timedelta | None:
        return self.acted_at - self.seen_at if self.acted_at is not None and self.seen_at is not None else None


def _changes(found: Sequence[Transition]) -> list[Transition]:
    """Moves by someone else: a person, or a declared service's own actor or timer. A person's reply is one change,
    its conversation's `reply`, not that and the message it is too."""
    replied = {(m.at, m.provider) for m in found if m.name == "reply"}
    return [
        m
        for m in found
        if m.by not in (Actor.AGENT, Actor.SCENARIO)
        and not (m.item.kind is EntityKind.MESSAGE and m.name == "create" and (m.at, m.provider) in replied)
    ]


def reactions(
    events: Sequence[WorldEvent], calls: Sequence[RecordedCall], wakes: Sequence[tuple[datetime, str | None]]
) -> list[Reaction]:
    """Every change someone else made, with when the agent saw it and acted on it. `wakes` are each wake's moment and
    reason."""
    found = moves(events)
    mine = [m for m in found if m.by is Actor.AGENT]
    reads = [
        (n, c)
        for n, c in enumerate(calls, start=1)
        if c.exchange.method.upper() == "GET" and c.exchange.status in ANSWERED and c.exchange.inbox_call is None
    ]
    out: list[Reaction] = []
    for change in _changes(found):
        seen: list[tuple[datetime, str, int | None]] = []
        if change.name == "reply" or change.item.kind is EntityKind.MESSAGE:
            seen += [(at, "wake", None) for at, reason in wakes if reason == REPLY_WAKE and at >= change.at][:1]
        seen += [
            (c.sim_time, "read", n)
            for n, c in reads
            if c.sim_time >= change.at and change.item.external_id in c.exchange.path
        ][:1]
        first = min(seen, default=None, key=lambda s: s[0])
        acted = (
            next((m for m in mine if m.at >= first[0] and (m.seq or 0) > (change.seq or 0)), None) if first else None
        )
        out.append(
            Reaction(
                change=change,
                seen_at=first[0] if first else None,
                seen_by=first[1] if first else None,
                seen_call=first[2] if first else None,
                acted_at=acted.at if acted else None,
                acted=acted,
            )
        )
    return out
