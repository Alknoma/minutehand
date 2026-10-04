"""Run every check in this package over one run, and score it.

There is no registration list: a check is a class defined in a module of this
package that has an `id`, a `needs` and a `run`. A module that defines one is
picked up the moment it exists, so a check cannot sit here and gate nothing.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from datetime import datetime

from minutehand import checks as package
from minutehand.checks._waits import ended_at
from minutehand.checks.effectiveness import measure
from minutehand.checks.expectations import Expectations
from minutehand.checks.ledger import build
from minutehand.domain.agent import Commitment
from minutehand.domain.checks import (
    Check,
    CheckReport,
    Effectiveness,
    Finding,
    FindingKind,
    RunView,
    Stability,
    WakeRecord,
)
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Model, Scenario
from minutehand.domain.world import Exchange, WorldEvent


class RunResult(Model):
    """Everything the checks said about one run, and the scorecard."""

    findings: list[Finding]
    blocked: list[str]
    notes: list[str]
    effectiveness: Effectiveness

    @property
    def exit_code(self) -> int:
        """1 when any finding is a failure. A blocked check does not pass a run; it is listed in `blocked`."""
        return 1 if any(f.kind is FindingKind.FAIL for f in self.findings) else 0


def _is_check(candidate: object) -> bool:
    if not inspect.isclass(candidate):
        return False
    attributes = vars(candidate)
    return (
        isinstance(attributes["id"] if "id" in attributes else None, str)
        and isinstance(attributes["needs"] if "needs" in attributes else None, frozenset)
        and callable(attributes["run"] if "run" in attributes else None)
    )


def discover() -> list[Check]:
    """One instance of every check class defined in this package, ordered by id."""
    found: list[Check] = []
    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        for _, candidate in inspect.getmembers(module, _is_check):
            if candidate.__module__ == module.__name__:
                found.append(candidate())
    ids = [c.id for c in found]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise ValueError(f"two checks share an id: {', '.join(duplicated)}")
    return sorted(found, key=lambda c: c.id)


def evaluate(view: RunView, *, ended: datetime | None = None) -> RunResult:
    """Every discovered check over a view that is already built."""
    findings: list[Finding] = []
    blocked: list[str] = []
    notes: list[str] = []
    met = len(view.scenario.expect)
    for check in discover():
        report: CheckReport = check.run(view)
        findings += report.findings
        blocked += report.blocked
        notes += [f"{check.id}: {n}" for n in report.notes]
        if isinstance(check, Expectations):
            met -= len(report.findings)
    card = measure(view, findings, met=met, ended_at=ended or ended_at(view))
    return RunResult(findings=findings, blocked=blocked, notes=notes, effectiveness=card)


def evaluate_run(
    scenario: Scenario,
    events: list[WorldEvent],
    wakes: list[WakeRecord],
    replies: list[PersonReply],
    *,
    commitments: list[Commitment] | None = None,
    unmatched_calls: list[Exchange] | None = None,
    ended: datetime | None = None,
) -> RunResult:
    """Build the obligations ledger from the world and the replies, then run every check."""
    view = RunView(
        scenario=scenario,
        events=events,
        wakes=wakes,
        obligations=build(scenario, events, replies),
        commitments=commitments,
        unmatched_calls=unmatched_calls,
    )
    return evaluate(view, ended=ended)


def stability(results: list[RunResult]) -> Stability:
    """Several samples of one scenario: how many passed."""
    if not results:
        raise ValueError("stability needs at least one sample")
    return Stability(samples=len(results), passed=sum(1 for r in results if r.exit_code == 0))
