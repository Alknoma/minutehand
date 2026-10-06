"""The same person asked about the same thing twice within a short window.

Two messages count as the same ask when they go to the same channel inside the
window, measured on the simulated clock (a simulated fortnight plays in seconds of
real time, so the machine's clock would put every message of a run inside it), one was sent while the other was still unanswered, and they share the
words that make an ask particular. An agent writes much of every message from the
same template ("could you approve, decline, or reply to …", the same footer link),
so each three-word run is weighted by how rare it is among everything the agent
wrote in the run: what every message says names no particular ask. Whether two
differently worded messages mean the same thing is still a judgement, so this
check answers `review`, not `fail`.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import timedelta

from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, RunView, Severity
from minutehand.domain.world import Actor, MessageSnapshot, Operation

WINDOW = timedelta(minutes=5)
SAME_ASK = 0.12
"""Weighted overlap above which two messages are taken to ask the same thing.

On the reference capture the one real repeat scores 0.17; the two template-alike
pairs it used to flag score 0.08 and 0.004."""

_WORD = re.compile(r"\w+")

Run = tuple[str, str, str]


def _runs(text: str) -> set[Run]:
    words = _WORD.findall(text.casefold())
    return {(words[i], words[i + 1], words[i + 2]) for i in range(len(words) - 2)}


def _similarity(a: set[Run], b: set[Run], weight: dict[Run, float]) -> float:
    shared = sum(weight[r] ** 2 for r in a & b)
    whole = math.sqrt(sum(weight[r] ** 2 for r in a) * sum(weight[r] ** 2 for r in b))
    return shared / whole if whole else 0.0


class RepeatedMessage:
    id = "repeated_message"
    needs = frozenset({Needs.WORLD})
    pattern = "one_open_ask_per_person"

    def run(self, view: RunView) -> CheckReport:
        sent = [e for e in view.events if e.operation is Operation.CREATE and isinstance(e.after, MessageSnapshot)]
        runs = {e.seq: _runs(e.after.text) for e in sent if isinstance(e.after, MessageSnapshot)}
        written = [runs[e.seq] for e in sent if e.actor is Actor.AGENT]
        seen_in = Counter(r for message in written for r in message)
        weight = {r: math.log((len(written) + 1) / n) for r, n in seen_in.items()}
        findings: list[Finding] = []
        last_by_channel: dict[str, int] = {}
        for index, event in enumerate(sent):
            assert isinstance(event.after, MessageSnapshot)
            channel = event.after.channel
            if event.actor is not Actor.AGENT:
                last_by_channel.pop(channel, None)
                continue
            earlier = last_by_channel.get(channel)
            last_by_channel[channel] = index
            if earlier is None or event.sim_time - sent[earlier].sim_time > WINDOW:
                continue
            if _similarity(runs[sent[earlier].seq], runs[event.seq], weight) < SAME_ASK:
                continue
            gap = int((event.sim_time - sent[earlier].sim_time).total_seconds())
            findings.append(
                Finding(
                    check=self.id,
                    severity=Severity.WARNING,
                    kind=FindingKind.REVIEW,
                    message=f"two messages to the same person {gap} seconds apart with no reply between",
                    at=event.sim_time,
                    wake=event.wake,
                    evidence=[sent[earlier].seq, event.seq],
                    pattern=self.pattern,
                )
            )
        return CheckReport(findings=findings)
