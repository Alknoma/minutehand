"""Calls that wait on the world: a long poll that finds nothing yet is held, out of every lock, until the run's clock
reaches the moment the world can answer it, or the world changes, and is answered then (docs/design.md, "A call that
waits on the world").

A provider's app says a call would wait (`answering.waits`) and is told whether it will be held: only when the run
lends the world a `ports.provider.HeldCalls`, which `minutehand run` does and a standing world does not. The proxy
then drops what the app answered, gives the world's lock back, and holds the call until it is looked at again: by
the run when its clock reaches the call's moment or something the run fired changed the world, or by the proxy
itself after it answers another call in the same world. Each look answers the call through the app afresh, on the
world and the clock as they then stand: what it waited for, or, once the clock has reached the end of its wait,
whatever is there, nothing included.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime

from minutehand.ports.provider import HeldCalls

AGAIN_WITHIN = 2.0
"""Real seconds the run gives the agent, once a call held for it is answered, to make its next call to the same
world (a worker polling in a loop is back within milliseconds) before it moves its clock on, so that call is held
from the moment the last one ended rather than from wherever the clock has jumped to."""


@dataclass
class Waits:
    """The calls held in one world, and the calls being answered in it now."""

    holds: HeldCalls | None = None
    """What the run lends this world to hold a call; None: a call that would wait is refused (a standing world)."""
    held: dict[str, Held] = field(default_factory=lambda: {})
    answering: int = 0
    """Calls inside the world's provider apps now: neither answered nor held."""
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when a call of the agent's reaches the world; cleared when a held call is answered."""
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    """Set while no call is being answered: each that reached the world is answered or held."""

    def __post_init__(self) -> None:
        self.settled.set()

    def began(self, *, fresh: bool) -> None:
        """A call is being answered: `fresh` when the agent just made it, else a held call looked at again."""
        self.answering += 1
        self.settled.clear()
        if fresh:
            self.arrived.set()

    def ended(self) -> None:
        """A call is answered, or held."""
        self.answering -= 1
        if self.answering == 0:
            self.settled.set()

    async def quiet(self) -> None:
        """Until no call is being answered: each that reached the world is answered or held."""
        await self.settled.wait()

    def stir(self) -> None:
        """Another call was answered, and may have changed what a held call waits for: each looks again."""
        for held in self.held.values():
            held.turn.set()


@dataclass
class Held:
    """One call held: the end of its wait, and the two signals it is looked at by."""

    ref: str
    until: datetime
    waits: Waits
    turn: asyncio.Event = field(default_factory=asyncio.Event)
    """Set to have the call look again."""
    looked: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when a look is over: the call answered, or held again."""
    over: bool = False
    """Answer it as the world stands at the next look: the run is ending."""
    answered: bool = False

    async def look(self, over: bool) -> bool:
        """`HeldCalls.hold`'s `look`: look at the call once on the world as it stands and answer whether that answered
        it. Once answered, wait for the agent's next call to the world (up to `AGAIN_WITHIN`), and for each call that
        reached it to be answered or held, so the run moves its clock on only once the agent is waiting again."""
        self.over = self.over or over
        if not self.answered:
            self.looked.clear()
            self.turn.set()
            await self.looked.wait()
        if self.answered and not over:
            with suppress(TimeoutError):
                await asyncio.wait_for(self.waits.arrived.wait(), AGAIN_WITHIN)
        await self.waits.settled.wait()
        return self.answered

    def ended_by(self, now: datetime) -> bool:
        """Whether the call is to be answered as the world stands, without waiting again."""
        return self.over or now >= self.until
