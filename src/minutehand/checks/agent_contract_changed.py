"""The agent's own product answered Minutehand outside its own API description.

An inbox declared by an operation of the agent's OpenAPI document (`domain.inboxes.OperationRequest`) is checked
against that document on every answer: a field missing, retyped or renamed means the agent's contract changed under
whoever relies on it, and what Minutehand read from it cannot be trusted. Each such answer is one failure, naming the
operation and the field: a review, or a failure when the agent file or the scenario names it in
`fail_on_integrity`. An inbox declared by a template has no document to hold the agent to, and is never read
here.
"""

from __future__ import annotations

from minutehand.domain.assessments import IntegrityCheck
from minutehand.domain.checks import CheckReport, Finding, Needs, RunView


class AgentContractChanged:
    id = "agent_contract_changed"
    needs = frozenset({Needs.WORLD})
    pattern = None

    def run(self, view: RunView) -> CheckReport:
        kind, severity = view.integrity(IntegrityCheck.AGENT_CONTRACT_CHANGED)
        findings: list[Finding] = []
        seen: set[str] = set()
        for call in view.contract_breaks:
            said = call.inbox_call.contract if call.inbox_call is not None else None
            if said is None or said in seen:
                continue
            seen.add(said)
            findings.append(
                Finding(
                    check=self.id,
                    severity=severity,
                    kind=kind,
                    message=f"the agent's contract changed: {said}",
                    pattern=self.pattern,
                )
            )
        return CheckReport(findings=findings)
