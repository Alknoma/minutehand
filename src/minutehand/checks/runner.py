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
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Protocol

from pydantic import Field

from minutehand import checks as package
from minutehand.checks import judged as judged_package
from minutehand.checks.effectiveness import measure
from minutehand.checks.expectations import Expectations
from minutehand.checks.facts import ended_at
from minutehand.checks.health import CHECK as HEALTH
from minutehand.checks.health import health
from minutehand.checks.items import ItemChecks
from minutehand.checks.judged.asked_about import AskedAbout
from minutehand.checks.judged.review import Review
from minutehand.checks.ledger import build
from minutehand.checks.near_miss_name import NearMissName
from minutehand.domain.agent import Commitment, CommitmentStatus
from minutehand.domain.assessments import IntegrityCheck, Rule, StoppedBy, integrity_fails, merged
from minutehand.domain.checks import (
    AroundProxy,
    Check,
    CheckReport,
    CommitmentsReported,
    DeclaredCollection,
    Effectiveness,
    Finding,
    FindingKind,
    HealthFinding,
    Needs,
    ObligationKind,
    RuleRead,
    RunView,
    Stability,
    WakeModelCalls,
    WakeRecord,
)
from minutehand.domain.clock import DueEntry
from minutehand.domain.conversation import PersonCall, Wrote
from minutehand.domain.items import ItemCheck, ProvidedTypes, TypedItem
from minutehand.domain.people import PersonReply
from minutehand.domain.run import EXIT_CODES, StopReason, Verdict, VerdictKind
from minutehand.domain.scenario import Model, PersonAsked, ProviderKey, Scenario
from minutehand.domain.world import CallOutcome, EntityRef, Exchange, RecordedCall, WorldEvent
from minutehand.ports.model import JudgedCheck, ModelFailed
from minutehand.ports.model import Model as LanguageModel

NO_MODEL = "no model is configured"
NOT_JUDGING = "not assessed: the run was not asked to judge (--judge, with a model configured)"


class RunResult(Model):
    """Everything the checks said about one run, and the scorecard."""

    findings: list[Finding]
    blocked: list[str]
    notes: list[str]
    effectiveness: Effectiveness
    verdict: Verdict
    assessed_by: list[str] = Field(
        default=[],
        description="What judged the run: `items`, the assessment of the agent's effects against the declared world "
        "that every run gets, `review` when a model reviewed them, then each rule of `assess` by its id, "
        "`expectations` and `near_miss_name` when the scenario declares them, each integrity fact its files say fails "
        "the run (`fail_on_integrity`), and each of the agent's own checks",
    )
    rules_read: list[RuleRead] = Field(
        default=[], description="Each rule of `assess`, with how often it was read and how often it could not be"
    )
    simulation: list[HealthFinding] = Field(
        default=[],
        description="The simulated world's health (`checks.health`), kept apart from the agent's findings: what did "
        "not play as the files declare, and how much of what they declare the run reached",
    )

    @property
    def exit_code(self) -> int:
        """The verdict's: 0 passed, 1 a check failed, 3 no check failed and the agent did not finish, 4 Minutehand
        broke while answering a call, 6 the simulated world did not play as declared. A blocked check does not pass a
        run; it is listed in `blocked`."""
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
        and isinstance(attributes["wrote"] if "wrote" in attributes else None, Wrote)
        and callable(attributes["applies"] if "applies" in attributes else None)
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


def finding_names(own: Sequence[Check] = ()) -> set[str]:
    """Every name a finding of Minutehand's or the agent's checks goes by, which a rule may not take: each check's id,
    each judged check's, and each item check's (`domain.items.ItemCheck`)."""
    return {c.id for c in discover(own)} | {c.id for c in discover_judged()} | {c.value for c in ItemCheck}


def discover_judged() -> list[JudgedCheck]:
    """One instance of every judged check class defined in `checks.judged`, ordered by id."""
    found: list[JudgedCheck] = [cls() for cls in _classes(judged_package, _is_judged)]
    return _unique(found)


class _Tally:
    """What the checks have said so far, and how many expectations are met."""

    def __init__(self, view: RunView, own: Sequence[Check] = ()) -> None:
        self.assessed_by = assessed_by(view, own)
        self.findings: list[Finding] = []
        self.blocked: list[str] = []
        self.notes: list[str] = []
        self.rules_read: list[RuleRead] = []
        self.met = len(view.scenario.expect)
        self.unjudged = sum(1 for e in view.scenario.expect if isinstance(e, PersonAsked) and e.about is not None)

    def add(self, check_id: str, report: CheckReport) -> None:
        self.findings += report.findings
        self.blocked += report.blocked
        self.notes += [f"{check_id}: {n}" for n in report.notes]
        self.rules_read += report.rules_read

    def result(self, view: RunView, ended: datetime | None, stop: StopReason | None) -> RunResult:
        met = self.met - self.unjudged
        card = measure(view, self.findings, met=met, ended_at=ended or ended_at(view))
        world = health(view)
        return RunResult(
            findings=self.findings,
            blocked=self.blocked,
            notes=[*self.notes, *(f"{HEALTH}: {f.words}" for f in world)],
            effectiveness=card,
            verdict=verdict(view, card, stop, simulation=world),
            assessed_by=self.assessed_by,
            rules_read=self.rules_read,
            simulation=world,
        )


def assessed_by(view: RunView, own: Sequence[Check] = ()) -> list[str]:
    """What judged the run: the assessment of the agent's effects against the declared world, which every run gets
    (`items`), then what the team wrote besides: its rules, the scenario's expectations and protected names, the
    integrity facts its files say fail the run, and the agent's own checks. An integrity fact nobody named (a call
    around the proxy, a contract the agent broke) is not an assessment: it is stated as a review and says whether the
    run can be trusted, never how the agent should behave."""
    found = [ItemChecks.id, *(r.id for r in view.rules)]
    if view.scenario.expect:
        found.append(Expectations.id)
    if view.scenario.protected_names:
        found.append(NearMissName.id)
    found += [check.value for check in view.fail_on_integrity]
    return found + [c.id for c in own]


_STOPPED = {
    StopReason.AGENT_DONE: "the agent reported it was done",
    StopReason.WAKE_LIMIT: "the run stopped at its wake limit",
    StopReason.DEADLINE_PASSED: "the run stopped at the scenario's deadline",
    StopReason.NOTHING_PENDING: "the run stopped because nothing more was due and the agent asked for no wake",
    StopReason.AGENT_FAILED: "the run stopped because the agent could not be reached or answered with an error",
    StopReason.CLOSED: "the standing world, or the last world of its case, was closed by whoever opened it",
    StopReason.ENVIRONMENT_FAILED: "the run stopped because an external emulator it used was unavailable",
}


def verdict(
    view: RunView,
    card: Effectiveness,
    stop: StopReason | None,
    *,
    simulation: Sequence[HealthFinding] = (),
) -> Verdict:
    """Read from the findings of the run's own rules and checks, and from how the run stopped; nothing else.

    Tool failed when Minutehand broke answering any call: such a run says nothing about the agent. Simulation
    incomplete when the world did not play as its files declare (an incomplete kind in `simulation`): the agent is
    still judged on what did happen, and that verdict is kept in `on_what_happened`, its words after the world's.
    Otherwise failed when a finding failed; not judged when a check could not run; passed when the agent
    reported done, or nothing was left open; unfinished otherwise. Whether the agent should have done anything
    else is the team's to say, in its rules (`domain/assessments.py`).

    "Open" is read from the world: a wait the world had not settled, or a commitment the agent's last report held
    open. Nothing else is read into it: a wait on someone who never answers is open, whatever it said.
    """
    judged = _verdict(view, card, stop)
    problems = [f for f in simulation if f.incomplete]
    if not problems or judged.kind in (VerdictKind.ENVIRONMENT_FAILED, VerdictKind.TOOL_FAILED):
        return judged
    first = problems[0].words
    more = f" and {_count(len(problems) - 1, 'more')}" if len(problems) > 1 else ""
    return judged.model_copy(
        update={
            "kind": VerdictKind.SIMULATION_INCOMPLETE,
            "on_what_happened": judged.kind,
            "words": f"Simulation incomplete: {_count(len(problems), 'thing')} in the simulated world did not play as "
            f"its files declare ({first}{more}); on what did happen: {judged.words}",
        }
    )


def _verdict(view: RunView, card: Effectiveness, stop: StopReason | None) -> Verdict:
    commitments = (
        None if view.commitments is None else sum(1 for c in view.commitments if c.status is CommitmentStatus.OPEN)
    )
    open_waits = sum(1 for o in view.obligations if o.kind is not ObligationKind.DATE and o.settled_at is None)
    open_work = open_waits + (commitments or 0)
    how = _STOPPED[stop] if stop is not None else "how the run stopped was not recorded"
    reasons = unjudged(view)
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
    elif reasons:
        kind = VerdictKind.NOT_JUDGED
        words = (
            f"Not judged: no check failed, but {_count(len(reasons), 'thing')} kept this run from being judged; "
            f"{how}: " + "; ".join(reasons) + "."
        )
    elif stop is StopReason.AGENT_DONE or open_work == 0:
        kind = VerdictKind.PASSED
        left = "" if stop is StopReason.AGENT_DONE else ", with nothing left open"
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
        unjudged=reasons if kind is VerdictKind.NOT_JUDGED else [],
    )


def unjudged(view: RunView) -> list[str]:
    """Why a run with no failed check cannot be called passed or unfinished, one reason each; empty when it can: a
    check that needs the agent's wakes ran over a run that recorded none (a standing world nobody marked a step in
    and whose clock never moved). A check that could not read its input did not run, and a run whose checks did not
    run is not one they passed."""
    reasons: list[str] = []
    for check in discover():
        if Needs.WAKES in check.needs and not view.wakes:
            reasons.append(
                f"{check.id} could not run, it needs the agent's wakes, and no step was recorded (mark each step, or "
                "move the world's clock forward)"
            )
    return reasons


def _count(n: int, thing: str) -> str:
    return f"{n} {thing}{'' if n == 1 else 's'}"


def _deterministic(view: RunView, own: Sequence[Check] = ()) -> _Tally:
    tally = _Tally(view, own)
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
    """Every deterministic check over a view that is already built. Judged checks are not run, each said in a note
    to be not assessed, and an `about` expectation, which only `asked_about` can settle, is not counted as met.
    `stop` is how the run ended, None for a run captured elsewhere that does not say."""
    tally = _deterministic(view, own)
    tally.notes += [f"{check.id}: {NOT_JUDGING}" for check in discover_judged() if check.applies(view)]
    return tally.result(view, ended, stop)


type Keeping = Callable[[JudgedCheck], LanguageModel]
"""The model a judged check is handed: the run's, with every answer kept with the world under the check's `wrote`
(`application.kept.KeptModel`)."""


async def evaluate_judged(
    view: RunView,
    model: LanguageModel | None,
    *,
    stop: StopReason | None,
    ended: datetime | None = None,
    own: Sequence[Check] = (),
    kept: Keeping | None = None,
) -> RunResult:
    """Every deterministic check, then every judged check on what they did not fail. With no model, each judged
    check is blocked; a model that fails partway blocks the check it failed in. With `kept`, each check is handed
    the model through it, so its calls are on the record and replayed when asked again."""
    tally = _deterministic(view, own)
    failed = failed_entities(view, tally.findings)
    for check in discover_judged():
        if not check.applies(view):
            continue
        if model is None:
            tally.blocked.append(f"{check.id}: {NO_MODEL}")
            continue
        try:
            report = await check.judge(view, kept(check) if kept is not None else model, failed=failed)
        except ModelFailed as e:
            tally.blocked.append(f"{check.id}: the model failed: {e}")
            continue
        tally.add(check.id, report)
        if isinstance(check, Review):
            tally.assessed_by.insert(1, check.id)
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
    rules: Sequence[Rule] | None = None,
    fail_on_integrity: Sequence[IntegrityCheck] | None = None,
) -> RunResult:
    """Build the obligations ledger from the world and the replies, then run every deterministic check and the
    team's rules: `rules`, or the scenario's own when none are given."""
    view = view_of(
        scenario,
        events,
        wakes,
        replies,
        commitments=commitments,
        unmatched_calls=unmatched_calls,
        rules=merged([], scenario.assess, scenario.assess_off) if rules is None else rules,
        fail_on_integrity=(
            integrity_fails([], scenario.fail_on_integrity) if fail_on_integrity is None else fail_on_integrity
        ),
        stop=stop,
    )
    return evaluate(view, stop=stop, ended=ended)


def view_of(
    scenario: Scenario,
    events: list[WorldEvent],
    wakes: list[WakeRecord],
    replies: list[PersonReply],
    *,
    commitments: list[Commitment] | None = None,
    unmatched_calls: list[Exchange] | None = None,
    model_calls: list[WakeModelCalls] | None = None,
    broken_calls: list[Exchange] | None = None,
    contract_breaks: list[Exchange] | None = None,
    dues: list[DueEntry] | None = None,
    reported: list[CommitmentsReported] | None = None,
    around_proxy: list[AroundProxy] | None = None,
    uncalled_providers: Sequence[ProviderKey] = (),
    rules: Sequence[Rule] = (),
    fail_on_integrity: Sequence[IntegrityCheck] = (),
    stop: StopReason | None = None,
    calls: list[RecordedCall] | None = None,
    typed: Sequence[TypedItem] = (),
    item_types: Sequence[ProvidedTypes] = (),
    rhythm: timedelta | None = None,
    person_calls: Sequence[PersonCall] = (),
    collections: Sequence[DeclaredCollection] = (),
) -> RunView:
    """What every check reads: the world, the wakes, and the obligations ledger built from the replies that
    landed."""
    return RunView(
        scenario=scenario,
        events=events,
        wakes=wakes,
        obligations=build(scenario, events, replies),
        replies=replies,
        commitments=commitments,
        unmatched_calls=unmatched_calls,
        model_calls=model_calls,
        broken_calls=broken_calls or [],
        contract_breaks=contract_breaks or [],
        dues=dues,
        reported=reported,
        around_proxy=around_proxy,
        uncalled_providers=list(uncalled_providers),
        rules=list(rules),
        fail_on_integrity=list(fail_on_integrity),
        stopped=_STOPPED_BY[stop] if stop is not None else None,
        calls=calls,
        typed=list(typed),
        item_types=list(item_types),
        rhythm=rhythm,
        person_calls=list(person_calls),
        collections=list(collections),
    )


_STOPPED_BY = {
    StopReason.AGENT_DONE: StoppedBy.AGENT_DONE,
    StopReason.WAKE_LIMIT: StoppedBy.WAKE_LIMIT,
    StopReason.DEADLINE_PASSED: StoppedBy.DEADLINE_PASSED,
    StopReason.NOTHING_PENDING: StoppedBy.NOTHING_PENDING,
    StopReason.AGENT_FAILED: StoppedBy.AGENT_FAILED,
    StopReason.CLOSED: StoppedBy.CLOSED,
    StopReason.ENVIRONMENT_FAILED: StoppedBy.ENVIRONMENT_FAILED,
}


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
    """Over several samples: 2 when any's environment failed, else 4 when Minutehand broke in any, else 6 when any's
    simulated world did not play as declared, else 1 when any failed, else 5 when any could not be judged, else 3
    when any did not finish, else 0."""
    kinds = {r.verdict.kind for r in results}
    order = (
        VerdictKind.ENVIRONMENT_FAILED,
        VerdictKind.TOOL_FAILED,
        VerdictKind.SIMULATION_INCOMPLETE,
        VerdictKind.FAILED,
        VerdictKind.NOT_JUDGED,
        VerdictKind.UNFINISHED,
    )
    worst = next((k for k in order if k in kinds), VerdictKind.PASSED)
    return EXIT_CODES[worst]
