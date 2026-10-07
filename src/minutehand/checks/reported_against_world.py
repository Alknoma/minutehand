"""What the agent said it was waiting on, held against what the world shows.

An agent may report its commitments as each wake ends (`AgentReport.commitments`); the checkpoints keep them
(`RunView.reported`). The ledger knows every wait the world opened and settled. Where the two disagree about a
person, the agent's picture of its own work is wrong:

- **Met, while the world shows no answer.** A commitment on a person reported MET while a wait on them is open
  and nothing of theirs has settled since the commitment opened. FAIL: the agent is closing on a belief.
- **Still waiting, after the answer landed.** A commitment on a person reported OPEN more than the grace after
  their last wait settled, with none of theirs open. REVIEW: the answer may be one the commitment does not mean.
- **Lost track.** A wait on a person overdue by more than the grace at a wake's end, while the agent reports
  commitments and none of them is an open one on that person. REVIEW: the agent may track it under another name.

Each is said once per commitment or wait. What it cannot see: a commitment on a system, a date or a result, and
one naming nobody in the scenario; a conversation the ledger does not read as a wait. An agent that reports no
commitments is held to none, silently, since reporting them is optional.
"""

from __future__ import annotations

from datetime import datetime

from minutehand.checks._waits import GRACE, span
from minutehand.domain.agent import Commitment, CommitmentStatus, WaitingOn
from minutehand.domain.checks import (
    CheckReport,
    CommitmentsReported,
    Finding,
    FindingKind,
    Needs,
    Obligation,
    ObligationKind,
    RunView,
    Severity,
)

_PERSON_WAITS = frozenset({ObligationKind.ANSWER_FROM_PERSON, ObligationKind.WORK_WITH_PERSON})


def _open_at(o: Obligation, moment: datetime) -> bool:
    return o.opened_at <= moment and (o.settled_at is None or o.settled_at > moment)


class ReportedAgainstWorld:
    id = "reported_against_world"
    needs = frozenset({Needs.WORLD, Needs.OBLIGATIONS})
    pattern = "honest_closure"

    def run(self, view: RunView) -> CheckReport:
        if view.reported is None:
            return CheckReport()  # reporting commitments is optional: an agent that reports none is held to none
        key_of = {p.email.lower(): p.key for p in view.scenario.people}
        waits: dict[str, list[Obligation]] = {}
        for o in view.obligations:
            if o.kind in _PERSON_WAITS and o.person is not None:
                waits.setdefault(o.person, []).append(o)
        findings: list[Finding] = []
        said: set[tuple[str, str]] = set()  # (rule, commitment key or wait key): each disagreement said once
        for report in view.reported:
            for c in report.commitments:
                person = key_of.get((c.person_email or "").lower()) if c.waiting_on is WaitingOn.PERSON else None
                if person is None:
                    continue
                theirs = waits.get(person, [])
                for rule, found in (
                    ("met", self._met_unanswered(c, person, theirs, report)),
                    ("still", self._still_waiting(c, person, theirs, report)),
                ):
                    if found is not None and (rule, c.key) not in said:
                        said.add((rule, c.key))
                        findings.append(found)
            for wait, finding in self._lost(report, waits, key_of):
                if ("lost", wait) not in said:
                    said.add(("lost", wait))
                    findings.append(finding)
        return CheckReport(findings=findings)

    def _met_unanswered(
        self, c: Commitment, person: str, theirs: list[Obligation], report: CommitmentsReported
    ) -> Finding | None:
        if c.status is not CommitmentStatus.MET:
            return None
        open_now = [o for o in theirs if _open_at(o, report.at)]
        settled_since = [o for o in theirs if o.settled_at is not None and c.opened_at <= o.settled_at <= report.at]
        if not open_now or settled_since:
            return None
        return Finding(
            check=self.id,
            severity=Severity.ERROR,
            kind=FindingKind.FAIL,
            message=f"wake {report.wake} reported {c.key!r} met, waiting on {person}, while {person} had not "
            "answered: a wait on them was open and nothing of theirs had settled since the commitment opened",
            at=report.at,
            wake=report.wake,
            evidence=sorted(o.opened_by for o in open_now),
            pattern=self.pattern,
        )

    def _still_waiting(
        self, c: Commitment, person: str, theirs: list[Obligation], report: CommitmentsReported
    ) -> Finding | None:
        if c.status is not CommitmentStatus.OPEN or any(_open_at(o, report.at) for o in theirs):
            return None
        settled = [o for o in theirs if o.settled_at is not None and c.opened_at <= o.settled_at <= report.at]
        if not settled:
            return None
        last = max(settled, key=lambda o: o.settled_at or report.at)
        assert last.settled_at is not None
        if report.at - last.settled_at <= GRACE:
            return None
        return Finding(
            check=self.id,
            severity=Severity.WARNING,
            kind=FindingKind.REVIEW,
            message=f"wake {report.wake} still reported {c.key!r} open, waiting on {person}, "
            f"{span(report.at - last.settled_at)} after {person}'s answer settled their last wait",
            at=report.at,
            wake=report.wake,
            evidence=[last.opened_by],
            pattern=self.pattern,
        )

    def _lost(
        self, report: CommitmentsReported, waits: dict[str, list[Obligation]], key_of: dict[str, str]
    ) -> list[tuple[str, Finding]]:
        """Each overdue wait, by its key, on a person none of the reported open commitments names."""
        tracked = {
            key_of[c.person_email.lower()]
            for c in report.commitments
            if c.status is CommitmentStatus.OPEN and c.person_email and c.person_email.lower() in key_of
        }
        found: list[tuple[str, Finding]] = []
        for person, theirs in waits.items():
            if person in tracked:
                continue
            for o in theirs:
                if not _open_at(o, report.at) or o.expected_by is None or report.at - o.expected_by <= GRACE:
                    continue
                found.append(
                    (
                        o.key,
                        Finding(
                            check=self.id,
                            severity=Severity.WARNING,
                            kind=FindingKind.REVIEW,
                            message=f"a wait on {person} was overdue by {span(report.at - o.expected_by)} as wake "
                            f"{report.wake} ended, and none of the commitments the agent reported was an open one on "
                            f"{person}",
                            at=report.at,
                            wake=report.wake,
                            evidence=[o.opened_by],
                            pattern="expiry_on_every_wait",
                        ),
                    )
                )
                break
        return found
