"""External emulators in a run's record: their health changes, kept in the world's log, and what is said of them
when the run is scored.

An emulator's failure is the environment's, never the agent's: a run whose emulator was unavailable stops
`ENVIRONMENT_FAILED` and exits 2, and its finding names the emulator, the first call it failed and the end of its
log. Its faithful errors are the service's; what it has no implementation of, and its own 5xx answers, are listed
for review, so a gap in the emulator is not read as a fault of the agent's.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from minutehand.domain.checks import Finding, FindingKind, Severity
from minutehand.domain.emulator import EmulatorChange, EmulatorHealth
from minutehand.domain.world import Actor, CallOutcome, Change, EntityKind, EntityRef, Operation, RecordedCall
from minutehand.ports.store import Store

CHECK = "emulator"
"""The name every finding about an external emulator is reported under."""

HEALTH = "health"


def health_entity(emulator: str) -> EntityRef:
    """The one entity an emulator's health changes are versions of. Actor SCENARIO: no check counts it as work."""
    return EntityRef(provider=emulator, kind=EntityKind.RECORD, external_id=HEALTH)


def record_health(store: Store, change: EmulatorChange) -> None:
    entity = health_entity(change.emulator)
    store.apply(
        Change(
            entity=entity,
            operation=Operation.CREATE if store.get(entity) is None else Operation.UPDATE,
            actor=Actor.SCENARIO,
            body=change.model_dump_json(),
        )
    )


def health_changes(store: Store, emulator: str) -> list[EmulatorChange]:
    return [EmulatorChange.model_validate_json(v.body) for v in store.versions(health_entity(emulator))]


def used(calls: Sequence[RecordedCall]) -> list[str]:
    """Every external emulator the calls were forwarded to, in the order of its first call."""
    return list(
        dict.fromkeys(
            c.exchange.captured.emulator for c in calls if c.exchange.captured and c.exchange.captured.emulator
        )
    )


def _said(call: RecordedCall) -> str:
    e = call.exchange
    return f"{e.method} {e.host}{urlsplit(e.path).path} at wake {call.wake}"


def findings(store: Store) -> list[Finding]:
    """What is said of each emulator the run's calls reached: unavailable (a failure of the environment), not
    implemented, or broken inside, each with the call that shows it."""
    calls = store.calls()
    said: list[Finding] = []
    for name in used(calls):
        mine = [c for c in calls if c.exchange.captured is not None and c.exchange.captured.emulator == name]
        down = [c for c in mine if c.exchange.outcome is CallOutcome.UNAVAILABLE]
        if down:
            first = down[0]
            failed = [
                h for h in health_changes(store, name) if h.health in (EmulatorHealth.DIED, EmulatorHealth.UNHEALTHY)
            ]
            why = failed[0].reason if failed else first.exchange.captured.note if first.exchange.captured else None
            tail = failed[0].log_tail if failed else None
            message = (
                f"external emulator {name} was unavailable ({why}); the first call it failed: {_said(first)}, "
                f"answered {first.exchange.status}; {len(down)} call(s) in all. The run's environment failed, not "
                "the agent"
            )
            if tail:
                message += f". Its log ends:\n{tail}"
            said.append(
                Finding(
                    check=CHECK,
                    severity=Severity.ERROR,
                    kind=FindingKind.FAIL,
                    message=message,
                    wake=first.wake,
                    at=first.sim_time,
                )
            )
        missing = [c for c in mine if c.exchange.outcome is CallOutcome.NOT_IMPLEMENTED]
        operations = list(dict.fromkeys(c.exchange.captured.operation for c in missing if c.exchange.captured))
        for operation in operations:
            asked = [c for c in missing if c.exchange.captured and c.exchange.captured.operation == operation]
            said.append(
                Finding(
                    check=CHECK,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=f"external emulator {name} has no implementation of {operation}: {len(asked)} call(s), "
                    f"the first {_said(asked[0])}. What the agent did next was decided on an answer the real "
                    "service would not give",
                    wake=asked[0].wake,
                    at=asked[0].sim_time,
                )
            )
        broken = [c for c in mine if c.exchange.outcome is CallOutcome.INTERNAL_ERROR]
        if broken:
            said.append(
                Finding(
                    check=CHECK,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=f"external emulator {name} failed inside {len(broken)} call(s) with an error nobody "
                    f"declared faithful, the first {_said(broken[0])}, answered {broken[0].exchange.status}",
                    wake=broken[0].wake,
                    at=broken[0].sim_time,
                )
            )
    return said
