"""The same person asked about the same thing twice within a short window.

Two messages count as the same ask when they go to the same channel inside the
window and one was sent while the other was still unanswered. Whether two
differently worded messages mean the same thing is a judgement, so this check
answers `review`, not `fail`.
"""

from __future__ import annotations

from datetime import timedelta

from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.world import Actor, MessageSnapshot, Operation

WINDOW = timedelta(minutes=5)


class RepeatedMessage:
    id = "repeated_message"
    needs = frozenset({Needs.WORLD})

    def run(self, view: RunView) -> CheckReport:
        sent = [
            e
            for e in view.events
            if e.operation is Operation.CREATE and isinstance(e.after, MessageSnapshot)
        ]
        findings: list[Finding] = []
        last_by_channel: dict[str, int] = {}
        for index, event in enumerate(sent):
            assert isinstance(event.after, MessageSnapshot)
            channel = event.after.channel
            if event.actor is not Actor.AGENT:
                last_by_channel.pop(channel, None)
                continue
            earlier = last_by_channel.get(channel)
            if earlier is not None and event.wall_time - sent[earlier].wall_time <= WINDOW:
                gap = int((event.wall_time - sent[earlier].wall_time).total_seconds())
                findings.append(
                    Finding(
                        check=self.id,
                        severity=Severity.WARNING,
                        kind=FindingKind.REVIEW,
                        message=f"two messages to the same person {gap} seconds apart with no reply between",
                        at=event.sim_time,
                        wake=event.wake,
                        evidence=[sent[earlier].seq, event.seq],
                        pattern="one_open_ask_per_person",
                    )
                )
            last_by_channel[channel] = index
        return CheckReport(findings=findings)
