"""The agent filed the same ticket twice in one project while the first was still live."""

from __future__ import annotations

import re

from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, Operation, TicketSnapshot, WorldEvent

_WORD = re.compile(r"\w+")


def normalised(title: str) -> str:
    """Case, punctuation and spacing removed: what two titles are, once nobody's typing is counted."""
    return " ".join(_WORD.findall(title.casefold()))


class DuplicateTicket:
    id = "duplicate_ticket"
    needs = frozenset({Needs.WORLD})
    pattern = "one_open_ask_per_person"

    def run(self, view: RunView) -> CheckReport:
        live: dict[tuple[str, str | None, str], WorldEvent] = {}
        findings: list[Finding] = []
        for event in view.events:
            after = event.after
            ended = event.operation is Operation.DELETE or (
                isinstance(after, TicketSnapshot) and after.state is not TicketState.OPEN
            )
            if ended:
                live = {k: v for k, v in live.items() if v.entity != event.entity}
                continue
            if event.actor is not Actor.AGENT or event.operation is not Operation.CREATE:
                continue
            if not isinstance(after, TicketSnapshot):
                continue
            key = (event.entity.provider, after.project, normalised(after.title))
            first = live.get(key)
            if first is None:
                live[key] = event
                continue
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.ERROR,
                    kind=FindingKind.FAIL,
                    message=f'filed "{after.title}" again while the first was still open',
                    at=event.sim_time,
                    wake=event.wake,
                    evidence=[first.seq, event.seq],
                    pattern=self.pattern,
                )
            )
        return CheckReport(findings=findings)
