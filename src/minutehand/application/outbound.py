"""What the agent called beyond the providers, read from a run's recorded calls: per host, how many calls, how
each was answered, and, for a host nobody declared, the declaration that would capture it.

A host is summarised under the declaration that matched it (`Captured.declared_as`), else by its own name; a
call refused because nobody claims or declares its host is counted too, so a run that refused a search says
so beside the hosts it captured. A host relayed unopened on tunnels (a model API) is summarised by its bursts,
their connections and the bytes each way, never as refused.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

import yaml

from minutehand.domain.run import EmulatorUse, OperationCount, OutboundUse
from minutehand.domain.world import AnsweredBy, CallOutcome, CaptureMode, RecordedCall

PATHS_SHOWN = 5

WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
"""HTTP's own methods that ask a server to change something: a host called only with these is suggested as
`acknowledge`, since a send that left the machine in a test would reach someone."""


def outbound_uses(calls: Sequence[RecordedCall]) -> list[OutboundUse]:
    """Each host no provider claims that the calls reached, in the order of its first call."""
    uses: dict[tuple[str, CaptureMode | None, bool], OutboundUse] = {}
    connections: dict[tuple[str, CaptureMode | None, bool], set[str]] = {}
    for call in calls:
        if call.provider is not None or call.exchange.inbox_call is not None:
            continue  # a provider's, or Minutehand's own as a person in the agent's product: neither went outbound
        exchange = call.exchange
        captured = exchange.captured
        mode = captured.mode if captured is not None else None
        host = exchange.host.lower()
        tunnelled = exchange.tunnelled
        key = (host, mode, tunnelled is not None)
        found = uses[key] if key in uses else OutboundUse(host=host, mode=mode, calls=0)
        if tunnelled is not None:
            connections.setdefault(key, set()).add(tunnelled.connection)
            uses[key] = found.model_copy(
                update={
                    "calls": found.calls + 1,
                    "tunnelled": found.tunnelled + 1,
                    "connections": len(connections[key]),
                    "bytes_sent": found.bytes_sent + tunnelled.bytes_sent,
                    "bytes_received": found.bytes_received + tunnelled.bytes_received,
                    "methods": list(dict.fromkeys([*found.methods, exchange.method.upper()])),
                }
            )
            continue
        path = urlsplit(exchange.path).path
        unknown = [r.address for r in captured.recipients if r.person is None] if captured is not None else []
        uses[key] = found.model_copy(
            update={
                "declared_as": captured.declared_as if captured is not None else None,
                "calls": found.calls + 1,
                "replayed": found.replayed
                + (1 if captured is not None and captured.answered_by is AnsweredBy.RECORDING else 0),
                "refused": found.refused + (1 if captured is None or captured.answered_by is AnsweredBy.REFUSAL else 0),
                "methods": list(dict.fromkeys([*found.methods, exchange.method.upper()])),
                "paths": list(dict.fromkeys([*found.paths, path]))[:PATHS_SHOWN],
                "unknown_recipients": list(dict.fromkeys([*found.unknown_recipients, *unknown])),
                "not_served": found.not_served + (1 if captured is not None and captured.not_served_by else 0),
                "not_served_by": list(
                    dict.fromkeys(
                        [
                            *found.not_served_by,
                            *([captured.not_served_by] if captured and captured.not_served_by else []),
                        ]
                    )
                ),
            }
        )
    return list(uses.values())


def described(use: OutboundUse) -> str:
    """One line for a host: its calls and how they were answered."""
    if use.tunnelled:
        return (
            f"{use.host}: {use.calls} call{'s' if use.calls != 1 else ''} on {use.connections} "
            f"tunnel{'s' if use.connections != 1 else ''} relayed and never opened, {use.bytes_sent} bytes sent "
            f"and {use.bytes_received} received"
        )
    how = {
        None: "refused: nobody declares it",
        CaptureMode.ACKNOWLEDGE: "acknowledged, never sent",
        CaptureMode.PASS_THROUGH: "passed through to the real host",
        CaptureMode.REPLAY: "replayed from a recording",
        CaptureMode.DISCOVERED: "passed through, undeclared (--capture-unknown)",
        CaptureMode.FORWARD: "forwarded to its external emulator",
        CaptureMode.SERVICE: "answered from the state of its service (`services`, or undeclared under "
        "--capture-unknown model), never sent",
        CaptureMode.STORE: "kept and read back as declared, never sent",
    }[use.mode]
    parts = [f"{use.host}: {use.calls} call{'s' if use.calls != 1 else ''}, {how}"]
    if use.mode is CaptureMode.REPLAY:
        parts.append(f"{use.replayed} answered from the recording")
    if use.not_served:
        parts.append(f"{use.not_served} not served by {', '.join(use.not_served_by)}, answered as declared")
    if use.refused and use.mode is not None:
        parts.append(f"{use.refused} refused")
    if use.unknown_recipients:
        parts.append(f"sent to someone the scenario does not know: {', '.join(use.unknown_recipients)}")
    return "; ".join(parts)


def suggested(uses: Sequence[OutboundUse]) -> str:
    """A declaration for every host the run reached undeclared: discovered, or refused. A host called only to
    change something is suggested as `acknowledge`, since a test must not send a real email; anything else as
    `pass_through`, to be turned into `acknowledge` or `replay` by whoever knows what it is. Empty when every
    host was declared."""
    undeclared = (CaptureMode.DISCOVERED, None)
    hosts = list(
        dict.fromkeys(
            u.host
            for u in uses
            if (u.mode in undeclared or (u.mode is CaptureMode.SERVICE and u.declared_as is None)) and not u.tunnelled
        )
    )
    if not hosts:
        return ""
    entries: list[dict[str, str]] = []
    for host in hosts:
        methods = {m for u in uses if u.host == host for m in u.methods}
        kind = CaptureMode.ACKNOWLEDGE if methods and methods <= WRITES else CaptureMode.PASS_THROUGH
        entries.append({"host": host, "kind": kind.value})
    return yaml.safe_dump({"outbound": entries}, sort_keys=False)


def emulator_uses(calls: Sequence[RecordedCall]) -> list[EmulatorUse]:
    """Each external emulator the calls were forwarded to, in the order of its first call, by what its answers
    were; the operations it had no answer for, each counted."""
    uses: dict[str, EmulatorUse] = {}
    missing: dict[str, dict[str, int]] = {}
    for call in calls:
        captured = call.exchange.captured
        if captured is None or captured.emulator is None:
            continue
        name = captured.emulator
        found = uses[name] if name in uses else EmulatorUse(emulator=name, calls=0)
        outcome = call.exchange.outcome
        said = f"{call.exchange.method} {call.exchange.host}{urlsplit(call.exchange.path).path} (wake {call.wake})"
        if outcome is CallOutcome.NOT_IMPLEMENTED:
            counted = missing.setdefault(name, {})
            what = captured.operation or said
            counted[what] = (counted[what] if what in counted else 0) + 1
        uses[name] = found.model_copy(
            update={
                "calls": found.calls + 1,
                "answered": found.answered + (outcome is CallOutcome.ANSWERED),
                "refused": found.refused + (outcome is CallOutcome.REFUSED),
                "internal_errors": found.internal_errors + (outcome is CallOutcome.INTERNAL_ERROR),
                "unavailable": found.unavailable + (outcome is CallOutcome.UNAVAILABLE),
                "first_unavailable": found.first_unavailable or (said if outcome is CallOutcome.UNAVAILABLE else None),
            }
        )
    return [
        use.model_copy(
            update={
                "not_implemented": [
                    OperationCount(operation=o, calls=n) for o, n in (missing[name] if name in missing else {}).items()
                ]
            }
        )
        for name, use in uses.items()
    ]


def emulator_described(use: EmulatorUse) -> str:
    """One line for an emulator: its calls by what each answer was."""
    parts = [f"{use.emulator}: {use.calls} call{'s' if use.calls != 1 else ''}, {use.answered} answered"]
    if use.refused:
        parts.append(f"{use.refused} refused as the real service would")
    if use.internal_errors:
        parts.append(f"{use.internal_errors} failed inside the emulator")
    if use.unavailable:
        parts.append(f"{use.unavailable} unanswered: the emulator was unavailable, first {use.first_unavailable}")
    if use.not_implemented:
        named = ", ".join(f"{o.operation} ({o.calls})" for o in use.not_implemented)
        parts.append(f"not implemented by the emulator: {named}")
    return "; ".join(parts)
