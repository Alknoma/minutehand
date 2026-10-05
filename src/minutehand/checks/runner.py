"""Run every check in this package over one run, and score it.

There is no registration list: a check is a class defined in a module of this
package that has an `id`, a `needs` and a `run`. A module that defines one is
picked up the moment it exists, so a check cannot sit here and gate nothing.
A judged check is found the same way in `checks.judged`, by its `judge`.

Judged checks run only when asked for (`evaluate_judged`), after every
deterministic check, and never on an entity a deterministic check failed. Asked
for with no model, each one is listed in `blocked`: a judged check that could not
run never reads as one that passed. An `about` expectation counts as met only
when `asked_about` judged it so.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from datetime import datetime
from types import ModuleType
from typing import Protocol

from minutehand import checks as package
from minutehand.checks import judged as judged_package
from minutehand.checks._waits import ended_at
from minutehand.checks.effectiveness import measure
from minutehand.checks.expectations import Expectations
from minutehand.checks.judged.asked_about import AskedAbout
from minutehand.checks.ledger import build
from minutehand.domain.agent import Commitment, CommitmentStatus
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
from minutehand.domain.run import EXIT_CODES, StopReason, Verdict, VerdictKind
from minutehand.domain.scenario import Model, PersonAsked, Scenario
from minutehand.domain.world import EntityRef, Exchange, WorldEvent
from minutehand.ports.model import JudgedCheck, ModelFailed
from minutehand.ports.model import Model as LanguageModel

NO_MODEL = "no model is configured"


class RunResult(Model):
    """Everything the checks said about one run, and the scorecard."""

    findings: list[Finding]
    blocked: list[str]
    notes: list[str]
    effectiveness: Effectiveness
    verdict: Verdict

    @property
    def exit_code(self) -> int:
        """The verdict's: 0 passed, 1 a check failed, 3 no check failed and the agent did not finish. A blocked
        check does not pass a run; it is listed in `blocked`."""
        return self.verdict.exit_code


def _is_check(candidate: object) -> bool:
    if not inspect.isclass(candidate):
        return False
    attributes = vars(candidate)
    return (
        isinstance(attributes["id"] if "id" in attributes else None, str)
        and isinstance(attributes["needs"] if "needs" in attributes else None, frozenset)
        and callable(attributes["run"] if "run" in attributes else None)
    )


def _is_judged(candidate: object) -> bool:
    if not inspect.isclass(candidate):
        return False
    attributes = vars(candidate)
    return (
        isinstance(attributes["id"] if "id" in attributes else None, str)
        and isinstance(attributes["needs"] if "needs" in attributes else None, frozenset)
        and isinstance(attributes["prompt_version"] if "prompt_version" in attributes else None, str)
        and callable(attributes["judge"] if "judge" in attributes else None)
    )


def _classes(where: ModuleType, is_one: Callable[[object], bool]) -> list[type]:
    """Every class `is_one` accepts that is defined in a module of the package `where`."""
    found: list[type] = []
    for module_info in pkgutil.iter_modules(where.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{where.__name__}.{module_info.name}")
        for _, candidate in inspect.getmembers(module, is_one):
            if candidate.__module__ == module.__name__:
                found.append(candidate)
    return found


class _Identified(Protocol):
    id: str


def _unique[C: _Identified](found: list[C]) -> list[C]:
    ids = [c.id for c in found]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise ValueError(f"two checks share an id: {', '.join(duplicated)}")
    return sorted(found, key=lambda c: c.id)


def discover() -> list[Check]:
    """One instance of every check class defined in this package, ordered by id."""
    found: list[Check] = [cls() for cls in _classes(package, _is_check)]
    return _unique(found)


def discover_judged() -> list[JudgedCheck]:
    """One instance of every judged check class defined in `checks.judged`, ordered by id."""
    found: list[JudgedCheck] = [cls() for cls in _classes(judged_package, _is_judged)]
    return _unique(found)


class _Tally:
    """What the checks have said so far, and how many expectations are met."""

    def __init__(self, view: RunView) -> None:
        self.findings: list[Finding] = []
        self.blocked: list[str] = []
        self.notes: list[str] = []
        self.met = len(view.scenario.expect)
        self.unjudged = sum(1 for e in view.scenario.expect if isinstance(e, PersonAsked) and e.about is not None)

    def add(self, check_id: str, report: CheckReport) -> None:
        self.findings += report.findings
        self.blocked += report.blocked
        self.notes += [f"{check_id}: {n}" for n in report.notes]

    def result(self, view: RunView, ended: datetime | None, stop: StopReason | None) -> RunResult:
        met = self.met - self.unjudged
        card = measure(view, self.findings, met=met, ended_at=ended or ended_at(view))
        return RunResult(
            findings=self.findings,
            blocked=self.blocked,
            notes=self.notes,
            effectiveness=card,
            verdict=verdict(view, card, stop),
        )


_STOPPED = {
    StopReason.AGENT_DONE: "the agent reported it was done",
    StopReason.WAKE_LIMIT: "the run stopped at the scenario's wake limit",
    StopReason.DEADLINE_PASSED: "the run stopped at the scenario's deadline",
    StopReason.NOTHING_PENDING: "the run stopped because nothing more was due and the agent asked for no wake",
    StopReason.AGENT_FAILED: "the run stopped because the agent could not be reached or answered with an error",
}


def verdict(view: RunView, card: Effectiveness, stop: StopReason | None) -> Verdict:
    """Failed when a check failed. Otherwise passed when the agent reported done, or nothing was left open;
    unfinished when the run stopped any other way with a wait or a commitment still open."""
    commitments = (
        None if view.commitments is None else sum(1 for c in view.commitments if c.status is CommitmentStatus.OPEN)
    )
    open_work = card.waits_open_at_end + (commitments or 0)
    how = _STOPPED[stop] if stop is not None else "how the run stopped was not recorded"
    if card.failed_checks:
        kind = VerdictKind.FAILED
        words = f"Failed: {_count(card.failed_checks, 'check')} failed; {how}."
    elif stop is StopReason.AGENT_DONE or open_work == 0:
        kind = VerdictKind.PASSED
        left = "" if stop is StopReason.AGENT_DONE else ", with nothing left open"
        words = f"Passed: no check failed, and {how}{left}."
    else:
        kind = VerdictKind.UNFINISHED
        still = [_count(card.waits_open_at_end, "wait")] if card.waits_open_at_end else []
        still += [_count(commitments, "commitment")] if commitments else []
        words = (
            f"Not finished: no check failed, but the agent never reported it was done; {how}, "
            f"with {' and '.join(still)} still open."
        )
    return Verdict(
        kind=kind,
        stop=stop,
        failed_checks=card.failed_checks,
        open_waits=card.waits_open_at_end,
        open_commitments=commitments,
        words=words,
    )


def _count(n: int, thing: str) -> str:
    return f"{n} {thing}{'' if n == 1 else 's'}"


def _deterministic(view: RunView) -> _Tally:
    tally = _Tally(view)
    for check in discover():
        report: CheckReport = check.run(view)
        tally.add(check.id, report)
        if isinstance(check, Expectations):
            tally.met -= len(report.findings)
    return tally


def failed_entities(view: RunView, findings: list[Finding]) -> frozenset[EntityRef]:
    """Every entity a failing finding names as its evidence."""
    seqs = {seq for f in findings if f.kind is FindingKind.FAIL for seq in f.evidence}
    return frozenset(e.entity for e in view.events if e.seq in seqs)


def evaluate(view: RunView, *, stop: StopReason | None, ended: datetime | None = None) -> RunResult:
    """Every deterministic check over a view that is already built. Judged checks are not run, and an `about`
    expectation, which only `asked_about` can settle, is not counted as met. `stop` is how the run ended, None
    for a run captured elsewhere that does not say."""
    return _deterministic(view).result(view, ended, stop)


async def evaluate_judged(
    view: RunView, model: LanguageModel | None, *, stop: StopReason | None, ended: datetime | None = None
) -> RunResult:
    """Every deterministic check, then every judged check on what they did not fail. With no model, each judged
    check is blocked; a model that fails partway blocks the check it failed in."""
    tally = _deterministic(view)
    failed = failed_entities(view, tally.findings)
    for check in discover_judged():
        if model is None:
            tally.blocked.append(f"{check.id}: {NO_MODEL}")
            continue
        try:
            report = await check.judge(view, model, failed=failed)
        except ModelFailed as e:
            tally.blocked.append(f"{check.id}: the model failed: {e}")
            continue
        tally.add(check.id, report)
        if isinstance(check, AskedAbout):
            tally.unjudged = 0
            tally.met -= len(report.findings)
    return tally.result(view, ended, stop)


def evaluate_run(
    scenario: Scenario,
    events: list[WorldEvent],
    wakes: list[WakeRecord],
    replies: list[PersonReply],
    *,
    stop: StopReason | None,
    commitments: list[Commitment] | None = None,
    unmatched_calls: list[Exchange] | None = None,
    ended: datetime | None = None,
) -> RunResult:
    """Build the obligations ledger from the world and the replies, then run every deterministic check."""
    view = view_of(scenario, events, wakes, replies, commitments=commitments, unmatched_calls=unmatched_calls)
    return evaluate(view, stop=stop, ended=ended)


def view_of(
    scenario: Scenario,
    events: list[WorldEvent],
    wakes: list[WakeRecord],
    replies: list[PersonReply],
    *,
    commitments: list[Commitment] | None = None,
    unmatched_calls: list[Exchange] | None = None,
) -> RunView:
    """What every check reads: the world, the wakes, and the obligations ledger built from the replies."""
    return RunView(
        scenario=scenario,
        events=events,
        wakes=wakes,
        obligations=build(scenario, events, replies),
        commitments=commitments,
        unmatched_calls=unmatched_calls,
    )


def stability(results: list[RunResult]) -> Stability:
    """Several samples of one scenario: how many passed. A sample that did not finish did not pass."""
    if not results:
        raise ValueError("stability needs at least one sample")
    return Stability(samples=len(results), passed=sum(1 for r in results if r.verdict.kind is VerdictKind.PASSED))


def exit_code(results: list[RunResult]) -> int:
    """Over several samples: 1 when any failed, else 3 when any did not finish, else 0."""
    kinds = {r.verdict.kind for r in results}
    worst = next((k for k in (VerdictKind.FAILED, VerdictKind.UNFINISHED) if k in kinds), VerdictKind.PASSED)
    return EXIT_CODES[worst]
