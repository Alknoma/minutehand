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

import hashlib
import importlib
import importlib.util
import inspect
import pkgutil
from collections.abc import Callable, Collection, Sequence
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Protocol

from minutehand import checks as package
from minutehand.checks import judged as judged_package
from minutehand.checks._waits import chases, ended_at
from minutehand.checks.effectiveness import measure
from minutehand.checks.expectations import Expectations
from minutehand.checks.judged.asked_about import AskedAbout
from minutehand.checks.ledger import build
from minutehand.domain.agent import Commitment, CommitmentStatus
from minutehand.domain.checks import (
    AroundProxy,
    Check,
    CheckReport,
    CommitmentsReported,
    Effectiveness,
    Finding,
    FindingKind,
    Needs,
    ObligationKind,
    RunView,
    Stability,
    WakeModelCalls,
    WakeRecord,
)
from minutehand.domain.clock import DueEntry
from minutehand.domain.people import PersonReply
from minutehand.domain.run import EXIT_CODES, StopReason, Verdict, VerdictKind
from minutehand.domain.scenario import Model, PersonAsked, ProviderKey, Scenario, Silent
from minutehand.domain.world import CallOutcome, EntityRef, Exchange, RecordedCall, WorldEvent
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
        """The verdict's: 0 passed, 1 a check failed, 3 no check failed and the agent did not finish, 4 Minutehand
        broke while answering a call. A blocked check does not pass a run; it is listed in `blocked`."""
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


def discover(own: Sequence[Check] = ()) -> list[Check]:
    """One instance of every check class defined in this package, and `own`, the agent's (`load_checks`), ordered
    by id. An agent's check may not take an id of Minutehand's."""
    found: list[Check] = [cls() for cls in _classes(package, _is_check)]
    return _unique([*found, *own])


class ChecksRefused(ValueError):
    """An agent's check file that could not be loaded, or holds no check."""


def load_checks(paths: Sequence[str]) -> list[Check]:
    """One instance of every check class defined in each of the agent's check files (`AgentUnderTest.checks`),
    loaded as a module of its own. Refused, naming the file, when it cannot be read or run, or defines no check."""
    found: list[Check] = []
    for path in paths:
        source = Path(path)
        name = "minutehand_agent_checks_" + hashlib.sha256(str(source).encode()).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(name, source)
        if spec is None or spec.loader is None or not source.is_file():
            raise ChecksRefused(f"{path}: no such check file")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            raise ChecksRefused(f"{path}: could not be loaded: {type(e).__name__}: {e}") from e
        mine = [cls for _, cls in inspect.getmembers(module, _is_check) if cls.__module__ == name]
        if not mine:
            raise ChecksRefused(f"{path}: defines no check (a class with `id`, `needs` and `run`)")
        found += [cls() for cls in mine]
    return found


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
            verdict=verdict(view, card, stop, self.findings, ended or ended_at(view)),
        )


_STOPPED = {
    StopReason.AGENT_DONE: "the agent reported it was done",
    StopReason.WAKE_LIMIT: "the run stopped at the scenario's wake limit",
    StopReason.DEADLINE_PASSED: "the run stopped at the scenario's deadline",
    StopReason.NOTHING_PENDING: "the run stopped because nothing more was due and the agent asked for no wake",
    StopReason.AGENT_FAILED: "the run stopped because the agent could not be reached or answered with an error",
    StopReason.CLOSED: "the standing world, or the last world of its case, was closed by whoever opened it",
    StopReason.ENVIRONMENT_FAILED: "the run stopped because an external emulator it used was unavailable",
}


def verdict(
    view: RunView, card: Effectiveness, stop: StopReason | None, findings: list[Finding], ended: datetime
) -> Verdict:
    """Tool failed when Minutehand broke answering any call: such a run says nothing about the agent, so it is
    neither passed nor failed. Otherwise failed when a check failed; passed when the agent reported done with
    nothing it asked left abandoned, or nothing was left open; unfinished otherwise.

    Two refinements of "open", both read from the world and neither from the content of any message:

    - **Done, with an ask abandoned.** An agent that reports DONE while a question it asked is unanswered and it
      never followed it up has not finished: it stopped waiting. A "follow-up" sent in the same wake as the ask
      (`Chase.instant`) chased nothing, since the agent never waited, and does not count. Work handed to someone (a
      ticket) is not this. That is unfinished, not passed.
    - **The owner told the result.** Once every expectation of the scenario is met, a message to an owner who
      never answers (`Silent`), sent with or after the last of them, opens a wait nobody will settle; it is the
      result being reported, and neither keeps a run unfinished nor counts as an ask abandoned.
    """
    commitments = (
        None if view.commitments is None else sum(1 for c in view.commitments if c.status is CommitmentStatus.OPEN)
    )
    waits = chases(view, ended)
    still = [c for c in waits if c.obligation.settled_at is None]
    met_by = _all_met_at(view, card, findings)
    owner = next(p for p in view.scenario.people if p.key == view.scenario.owner)
    told_after = [
        c
        for c in still
        if met_by is not None
        and isinstance(owner.reply, Silent)
        and c.obligation.person == owner.key
        and c.obligation.opened_by >= met_by
    ]
    abandoned = [
        c
        for c in still
        if all(s in c.instant for s in c.follow_ups)
        and c.obligation.kind is ObligationKind.ANSWER_FROM_PERSON
        and c not in told_after
    ]
    open_waits = len(still) - len(told_after)
    open_work = open_waits + (commitments or 0)
    how = _STOPPED[stop] if stop is not None else "how the run stopped was not recorded"
    if stop is StopReason.ENVIRONMENT_FAILED:
        kind = VerdictKind.ENVIRONMENT_FAILED
        words = (
            f"Environment failed: {how}, so the agent is not judged on this run"
            f"{f' ({_count(card.failed_checks, "check")} failed after it)' if card.failed_checks else ''}."
        )
    elif view.broken_calls:
        kind = VerdictKind.TOOL_FAILED
        first = view.broken_calls[0]
        said = first.failure.message if first.failure is not None else f"{first.status}"
        words = (
            f"Not scored: Minutehand itself failed while answering {_count(len(view.broken_calls), 'call')} "
            f"(first: {first.method} {first.host}{first.path}: {said}); the agent is not judged on this run."
        )
    elif card.failed_checks:
        kind = VerdictKind.FAILED
        words = f"Failed: {_count(card.failed_checks, 'check')} failed; {how}."
    elif reasons := unjudged(view):
        kind = VerdictKind.NOT_JUDGED
        words = (
            f"Not judged: no check failed, but {_count(len(reasons), 'thing')} kept this run from being judged; "
            f"{how}: " + "; ".join(reasons) + "."
        )
    elif stop is StopReason.AGENT_DONE and abandoned:
        kind = VerdictKind.UNFINISHED
        people = sorted({c.obligation.person or "someone" for c in abandoned})
        waited = " after the wake it asked in" if any(c.instant for c in abandoned) else ""
        words = (
            f"Not finished: no check failed, but the agent reported it was done with {_count(len(abandoned), 'ask')} "
            f"it made still unanswered and never followed up{waited} ({', '.join(people)})."
        )
    elif stop is StopReason.AGENT_DONE or open_work == 0:
        kind = VerdictKind.PASSED
        if stop is StopReason.AGENT_DONE:
            left = ""
        elif told_after:
            left = (
                f", with every expectation met; {_count(len(told_after), 'message')} telling the owner, who never "
                "answers, is not counted as open"
            )
        else:
            left = ", with nothing left open"
        words = f"Passed: no check failed, and {how}{left}."
    else:
        kind = VerdictKind.UNFINISHED
        left_open = [_count(open_waits, "wait")] if open_waits else []
        left_open += [_count(commitments, "commitment")] if commitments else []
        words = (
            f"Not finished: no check failed, but the agent never reported it was done; {how}, "
            f"with {' and '.join(left_open)} still open."
        )
    return Verdict(
        kind=kind,
        stop=stop,
        failed_checks=card.failed_checks,
        open_waits=open_waits,
        open_commitments=commitments,
        words=words,
        unjudged=unjudged(view) if kind is VerdictKind.NOT_JUDGED else [],
    )


def unjudged(view: RunView) -> list[str]:
    """Why a run with no failed check cannot be called passed or unfinished, one reason each; empty when it can.

    A check that could not read its input did not run, and a run whose checks did not run is not one they passed:
    each check that needs the agent's wakes, in a run that recorded none (a standing world nobody marked a step in
    and whose clock never moved). And a run in which nothing was there to judge (no expectation declared, and no wait opened: nobody was asked
    anything the world saw answered, and nothing was handed to anyone) passed nothing. A check blocked because the
    ledger found nothing to wait on is not a reason by itself: an agent that asked nobody anything and met every
    expectation was judged, on its expectations."""
    reasons: list[str] = []
    for check in discover():
        needed: list[str] = []
        if Needs.WAKES in check.needs and not view.wakes:
            needed.append(
                "the agent's wakes, and no step was recorded (mark each step, or move the world's clock forward)"
            )
        if needed:
            reasons.append(f"{check.id} could not run, it needs {' and '.join(needed)}")
    waits = [o for o in view.obligations if o.kind is not ObligationKind.DATE]
    if not view.scenario.expect and not waits and not view.wakes:
        reasons.append(
            "nothing was there to judge: no expectation is declared and no wait was opened (nobody was asked "
            "anything the world saw answered, and no work was handed to anyone)"
        )
    return reasons


def _all_met_at(view: RunView, card: Effectiveness, findings: list[Finding]) -> int | None:
    """The seq of the last event that met an expectation, once every one of the scenario's is met; else None."""
    if not view.scenario.expect or card.expectations_met < card.expectations_total:
        return None
    seqs = [
        seq
        for f in findings
        if f.check == Expectations.id and f.kind is FindingKind.INFORMATIONAL
        for seq in f.evidence
    ]
    return max(seqs) if seqs else None


def _count(n: int, thing: str) -> str:
    return f"{n} {thing}{'' if n == 1 else 's'}"


def _deterministic(view: RunView, own: Sequence[Check] = ()) -> _Tally:
    tally = _Tally(view)
    for check in discover(own):
        report: CheckReport = check.run(view)
        tally.add(check.id, report)
        if isinstance(check, Expectations):
            tally.met -= Expectations.failed(report)
    return tally


def failed_entities(view: RunView, findings: list[Finding]) -> frozenset[EntityRef]:
    """Every entity a failing finding names as its evidence."""
    seqs = {seq for f in findings if f.kind is FindingKind.FAIL for seq in f.evidence}
    return frozenset(e.entity for e in view.events if e.seq in seqs)


def evaluate(
    view: RunView, *, stop: StopReason | None, ended: datetime | None = None, own: Sequence[Check] = ()
) -> RunResult:
    """Every deterministic check over a view that is already built. Judged checks are not run, and an `about`
    expectation, which only `asked_about` can settle, is not counted as met. `stop` is how the run ended, None
    for a run captured elsewhere that does not say."""
    return _deterministic(view, own).result(view, ended, stop)


async def evaluate_judged(
    view: RunView,
    model: LanguageModel | None,
    *,
    stop: StopReason | None,
    ended: datetime | None = None,
    own: Sequence[Check] = (),
) -> RunResult:
    """Every deterministic check, then every judged check on what they did not fail. With no model, each judged
    check is blocked; a model that fails partway blocks the check it failed in."""
    tally = _deterministic(view, own)
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
    withdrawn: Collection[int] = (),
    commitments: list[Commitment] | None = None,
    unmatched_calls: list[Exchange] | None = None,
    ended: datetime | None = None,
) -> RunResult:
    """Build the obligations ledger from the world and the replies, then run every deterministic check."""
    view = view_of(
        scenario,
        events,
        wakes,
        replies,
        withdrawn=withdrawn,
        commitments=commitments,
        unmatched_calls=unmatched_calls,
    )
    return evaluate(view, stop=stop, ended=ended)


def view_of(
    scenario: Scenario,
    events: list[WorldEvent],
    wakes: list[WakeRecord],
    replies: list[PersonReply],
    *,
    withdrawn: Collection[int] = (),
    commitments: list[Commitment] | None = None,
    unmatched_calls: list[Exchange] | None = None,
    model_calls: list[WakeModelCalls] | None = None,
    broken_calls: list[Exchange] | None = None,
    contract_breaks: list[Exchange] | None = None,
    dues: list[DueEntry] | None = None,
    reported: list[CommitmentsReported] | None = None,
    around_proxy: list[AroundProxy] | None = None,
    uncalled_providers: Sequence[ProviderKey] = (),
) -> RunView:
    """What every check reads: the world, the wakes, and the obligations ledger built from the replies, of
    which `withdrawn` (positions) were withdrawn before they landed."""
    return RunView(
        scenario=scenario,
        events=events,
        wakes=wakes,
        obligations=build(scenario, events, replies, withdrawn=withdrawn),
        replies=[r for i, r in enumerate(replies) if i not in withdrawn],
        commitments=commitments,
        unmatched_calls=unmatched_calls,
        model_calls=model_calls,
        broken_calls=broken_calls or [],
        contract_breaks=contract_breaks or [],
        dues=dues,
        reported=reported,
        around_proxy=around_proxy,
        uncalled_providers=list(uncalled_providers),
    )


def broken(calls: list[RecordedCall]) -> list[Exchange]:
    """The calls a provider in this process failed to answer: Minutehand's own error, not the agent's. An external
    emulator's (`captured`) is said by `application.emulators` instead."""
    return [
        c.exchange for c in calls if c.exchange.outcome is CallOutcome.INTERNAL_ERROR and c.exchange.captured is None
    ]


def contract_breaks(calls: list[RecordedCall]) -> list[Exchange]:
    """Minutehand's calls as a person whose answer departed from the agent's own API description."""
    return [c.exchange for c in calls if c.exchange.inbox_call is not None and c.exchange.inbox_call.contract]


def stability(results: list[RunResult]) -> Stability:
    """Several samples of one scenario: how many passed. A sample that did not finish did not pass."""
    if not results:
        raise ValueError("stability needs at least one sample")
    return Stability(samples=len(results), passed=sum(1 for r in results if r.verdict.kind is VerdictKind.PASSED))


def exit_code(results: list[RunResult]) -> int:
    """Over several samples: 2 when any's environment failed, else 4 when Minutehand broke in any, else 1 when any
    failed, else 5 when any could not be judged, else 3 when any did not finish, else 0."""
    kinds = {r.verdict.kind for r in results}
    order = (
        VerdictKind.ENVIRONMENT_FAILED,
        VerdictKind.TOOL_FAILED,
        VerdictKind.FAILED,
        VerdictKind.NOT_JUDGED,
        VerdictKind.UNFINISHED,
    )
    worst = next((k for k in order if k in kinds), VerdictKind.PASSED)
    return EXIT_CODES[worst]
