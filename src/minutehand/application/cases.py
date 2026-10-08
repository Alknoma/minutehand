"""A case: several standing worlds opened under one case label (`CreateWorld.case`) while any of them is open, read
and scored as ONE run.

A harness that drives its own agent opens a world per provider for one case (a messaging world, a documents
world, a tracker world). Each world keeps its own log, as before. The case adds a store of its own beside them
(`<state>/runs/<case_id>/world.db`), which holds what belongs to the case and to none of its worlds: the steps
marked on it, the model traffic its worlds' services made, and the spans that reached the server with no trace
link to any call. `CaseStore` reads all of them as one log:

- **One timeline.** Every world's events and the case's, ordered by simulated time, then by the real moment each
  was written, then by world in the order they opened; numbered again from 1. A call, a span or a version that
  names a seq names the new one.
- **One set of people.** The case's scenario (`merged`) holds every world's people once: a person is the same
  person in every world whose seed names their email, whatever key it gives them, so an ask in the messaging
  world and a ticket moved in the tracker world are one person's.
- **One set of steps.** A step marked on the case is begun in every world under one number, so the merged record
  reads one step wherever its events landed.

Nothing is written through a `CaseStore`; it is a reading.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Sequence

from pydantic import Field

from minutehand.application.refusals import RunRefused
from minutehand.domain.conversation import PersonCall
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Model, Person, ProviderKey, Scenario
from minutehand.domain.storage import Freed, RunUsage
from minutehand.domain.telemetry import ForwardFailure, ReceivedSpan, Signal, SpanSource, StoredSpan
from minutehand.domain.world import (
    CallBegan,
    Change,
    EntityKind,
    EntityRef,
    Exchange,
    RecordedCall,
    Stored,
    WorldEvent,
)
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

CASE = "case.json"
"""In a case's run directory: `CaseKept`, which marks it as a case and names its worlds."""


class CaseKept(Model):
    """What marks a run directory as a case's."""

    case_id: str
    name: str = Field(description="The case label its worlds were opened under")
    worlds: list[str] = Field(description="Its worlds, in the order they opened")


class _Part:
    """One store of the case, and where each of its seqs lands in the merged numbering."""

    def __init__(self, store: Store, events: list[WorldEvent]) -> None:
        self.store = store
        self.events = events
        self.local: list[int] = [e.seq for e in events]
        self.merged: list[int] = []

    def at(self, seq: int) -> int:
        """The merged seq of the last of this part's events at or before its own `seq`; 0 when none is."""
        i = bisect.bisect_right(self.local, seq)
        return self.merged[i - 1] if i else 0


class CaseStore:
    """The worlds of one case and the case's own store, read as one run (`Store`, for reading only)."""

    def __init__(self, run_id: str, parts: Sequence[Store]) -> None:
        self.run_id = run_id
        self._parts = [_Part(store, store.events()) for store in parts]
        tagged = [(e, n) for n, part in enumerate(self._parts) for e in part.events]
        tagged.sort(key=lambda t: (t[0].sim_time, t[0].wall_time, t[1], t[0].seq))
        self._events: list[WorldEvent] = []
        for number, (event, n) in enumerate(tagged, start=1):
            self._parts[n].merged.append(number)
            self._events.append(event.model_copy(update={"seq": number, "run_id": run_id}))
        for part in self._parts:
            # merged numbers were appended in merged order; each part's own events are in its own order, and a
            # world's clock never runs back, so both orders agree
            part.merged.sort()

    # -- reading ------------------------------------------------------------------------------------------------

    def events(self, *, since: int = 0) -> list[WorldEvent]:
        return [e for e in self._events if e.seq > since]

    def head(self) -> int:
        return len(self._events)

    def calls(self) -> list[RecordedCall]:
        found: list[RecordedCall] = []
        for part in self._parts:
            for call in part.store.calls():
                if call.first_seq <= call.last_seq:
                    first, last = part.at(call.first_seq), part.at(call.last_seq)
                else:
                    last = part.at(call.first_seq - 1)
                    first = last + 1
                found.append(call.model_copy(update={"first_seq": first, "last_seq": last}))
        return sorted(found, key=lambda c: (c.sim_time, c.last_seq))

    def spans(self, *, trace_id: str | None = None, wake: int | None = None) -> list[StoredSpan]:
        found: list[StoredSpan] = []
        for part in self._parts:
            for stored in part.store.spans(trace_id=trace_id, wake=wake):
                found.append(stored.model_copy(update={"after_seq": part.at(stored.after_seq)}))
        return sorted(found, key=lambda s: s.span.start)

    def replies(self) -> list[PersonReply]:
        return [r for part in self._parts for r in part.store.replies()]

    def person_calls(self) -> list[PersonCall]:
        return [c for part in self._parts for c in part.store.person_calls()]

    def written(self, key: str) -> str | None:
        return next((w for part in self._parts if (w := part.store.written(key)) is not None), None)

    def versions(self, entity: EntityRef) -> list[Stored]:
        found = [
            v.model_copy(update={"seq": part.at(v.seq)}) for part in self._parts for v in part.store.versions(entity)
        ]
        return sorted(found, key=lambda v: v.seq)

    def get(self, entity: EntityRef) -> Stored | None:
        latest = [
            s.model_copy(update={"seq": part.at(s.seq)})
            for part in self._parts
            if (s := part.store.get(entity)) is not None
        ]
        return max(latest, key=lambda s: s.seq) if latest else None

    def children(
        self, provider: ProviderKey, kind: EntityKind, parent: str | None, *, after: str | None = None, limit: int = 100
    ) -> list[Stored]:
        found = [
            s for part in self._parts for s in part.store.children(provider, kind, parent, after=after, limit=limit)
        ]
        return sorted(found, key=lambda s: s.entity.external_id)[:limit]

    def forward_failures(self) -> list[ForwardFailure]:
        return [f for part in self._parts for f in part.store.forward_failures()]

    def usage(self) -> RunUsage:
        parts = [part.store.usage() for part in self._parts]
        return RunUsage(
            run_id=self.run_id,
            rows=sum(p.rows for p in parts),
            bodies=sum(p.bodies for p in parts),
        )

    # -- what a reading cannot do -------------------------------------------------------------------------------

    def _refused(self, what: str) -> RunRefused:
        return RunRefused(f"case {self.run_id} is read as one run from its worlds, never {what} through")

    def apply(self, change: Change) -> WorldEvent:
        raise self._refused("written")

    def apply_all(self, changes: Sequence[Change]) -> list[WorldEvent]:
        raise self._refused("written")

    def attach(
        self,
        exchange: Exchange,
        *,
        first_seq: int,
        last_seq: int,
        provider: ProviderKey | None = None,
        began: CallBegan | None = None,
    ) -> None:
        raise self._refused("written")

    def remember(self, reply: PersonReply) -> None:
        raise self._refused("written")

    def record_person_call(self, call: PersonCall) -> None:
        raise self._refused("written")

    def wake_began(self, wake: int) -> None:
        raise self._refused("written")

    def wake_ended(self, wake: int) -> None:
        raise self._refused("written")

    def receive(self, spans: Sequence[ReceivedSpan], *, source: SpanSource) -> list[StoredSpan]:
        raise self._refused("written")

    def forward_failed(self, signal: Signal, endpoint: str, reason: str) -> ForwardFailure:
        raise self._refused("written")

    def fork(self, run_id: str, *, at_seq: int, clock: Clock) -> Store:
        raise self._refused("forked")

    def discard(self) -> None:
        raise self._refused("discarded")

    def sweep(self) -> Freed:
        raise self._refused("swept")


def run_name(label: str) -> str:
    """A case label as a scenario name: lower case, words joined by `_`, starting with a letter."""
    name = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return name if name[:1].isalpha() else f"case_{name}"


def merged(name: str, scenarios: Sequence[Scenario]) -> Scenario:
    """One scenario for the case: its worlds' people once each (by email, then by key), every expectation, every
    ticket fate and protected name, the earliest start, the first owner, goal and deadline that are set."""
    if not scenarios:
        raise ValueError("a case holds at least one world")
    people: list[Person] = []
    for scenario in scenarios:
        for person in scenario.people:
            if all(p.email != person.email and p.key != person.key for p in people):
                people.append(person)
    first = scenarios[0]
    starts = min(s.starts_at for s in scenarios)
    goals = list(dict.fromkeys(s.goal for s in scenarios if s.goal))
    deadline = next((s.deadline for s in scenarios if s.deadline is not None), None)
    return Scenario.model_validate(
        {
            "name": run_name(name),
            "goal": "\n".join(goals),
            "owner": first.owner,
            "starts_at": starts,
            "deadline_after": deadline - starts if deadline is not None else None,
            "max_wakes": max((s.max_wakes for s in scenarios if s.max_wakes is not None), default=None),
            "seed": first.seed,
            "protected_names": list(dict.fromkeys(n for s in scenarios for n in s.protected_names)),
            "people": [p.model_dump() for p in people],
            "ticket_fates": [f.model_dump() for s in scenarios for f in s.ticket_fates],
            "expect": [e.model_dump() for s in scenarios for e in s.expect],
            "assess": [r.model_dump() for r in {r.id: r for s in reversed(scenarios) for r in s.assess}.values()],
        }
    )
